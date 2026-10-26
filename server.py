# -*- coding: utf-8 -*-
"""Stellen-Finder — http://localhost:8813

Knopf „Stellen suchen“: Portale abfragen, Claude bewertet die Passung.
Klick auf eine Stelle: Claude passt den Lebenslauf an und schreibt das Anschreiben.
Nichts wird verschickt.
"""

import base64
import hashlib
import json
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

import dokumente
import ki
import portale

PORT = 8813
HIER = Path(__file__).parent
DATEN = HIER / "daten"
SUCHE_DATEI = DATEN / "suche.json"
MAX_BEWERTEN = 400    # mit Lebenslauf: so viele Stellen prueft Claude hoechstens
KI_GLEICHZEITIG = 5   # Claude-Laeufe parallel
PRO_KI_LAUF = 40
MINDESTENS = 50       # ab dieser Punktzahl gilt eine Stelle als passend
MAX_ANFRAGE = 64_000
MAX_LEBENSLAUF = 10_000_000   # Upload als Base64 im JSON
CV_ORDNER = DATEN / "lebenslauf"   # genau EIN hochgeladener Lebenslauf
# Nur Aufrufe von der eigenen Seite annehmen (Schutz gegen fremde Webseiten,
# die per DNS-Rebinding oder CSRF auf den lokalen Server zugreifen wollen).
EIGENE_HOSTS = (f"localhost:{PORT}", f"127.0.0.1:{PORT}")
EIGENE_HERKUNFT = tuple(f"http://{h}" for h in EIGENE_HOSTS)
# Je Dokument: Textdatei (zuletzt geschrieben = fertig) und PDF-Datei.
ARTEN = {"lebenslauf": ("lebenslauf.md", "Lebenslauf.pdf"),
         "anschreiben": ("anschreiben.txt", "Anschreiben.pdf")}
MAX_DOKUMENT = 30_000   # Zeichen, die man im Fenster bearbeiten kann

zustand = {"laeuft": False, "meldung": "", "fehler": None}
suche = {}            # letzte Suche (auch nach Neustart aus der Datei)
bewerbungen = {}      # "art|key" -> {"status", "meldung", "fehler", "kennung", "text"}
sperre = threading.Lock()


def _lebenslauf():
    """Pfad des hochgeladenen Lebenslaufs oder None. Nur echte .docx/.pdf —
    versteckte Dateien (.DS_Store, laufende Uploads) zaehlen nicht."""
    try:
        dateien = [p for p in CV_ORDNER.iterdir()
                   if p.is_file() and not p.name.startswith(".")
                   and p.suffix.lower() in (".docx", ".pdf")]
        return str(max(dateien, key=lambda p: p.stat().st_mtime_ns)) if dateien else None
    except OSError:
        return None


def _cv_kennung(pfad):
    """Erkennt einen neu hochgeladenen Lebenslauf auch bei gleichem Dateinamen."""
    try:
        st = Path(pfad).stat()
    except (OSError, TypeError):
        return ""
    return f"{Path(pfad).name}|{st.st_size}|{st.st_mtime_ns}"


def _aktueller_cv(text=False):
    """(pfad, kennung[, text]) des aktuellen Lebenslaufs, unter der Sperre gelesen,
    damit ein gleichzeitiger Upload nichts halb Ausgetauschtes liefert."""
    with sperre:
        pfad = _lebenslauf()
        kennung = _cv_kennung(pfad)
        if not text:
            return pfad, kennung
        return pfad, kennung, (dokumente.lebenslauf_text(pfad) if pfad else None)


def _ordner(key, kennung):
    """Eine Bewerbung gehoert zu Stelle UND Lebenslauf: anderer Lebenslauf,
    anderer Ordner. Der Hash macht den Namen ausserdem pfadsicher."""
    return DATEN / "bewerbungen" / hashlib.sha1(f"{key}|{kennung}".encode()).hexdigest()[:20]


def _speichern():
    DATEN.mkdir(exist_ok=True)
    SUCHE_DATEI.write_text(json.dumps(suche, ensure_ascii=False, indent=1), "utf-8")


def _bewerbungen_laden():
    """Fertige Dokumente zur aktuellen Suche und zum aktuellen Lebenslauf."""
    _, kennung = _aktueller_cv()
    neu = {}
    for s in suche.get("stellen", []):
        o = _ordner(s["key"], kennung)
        for art, (textdatei, _) in ARTEN.items():
            if kennung and (o / textdatei).exists():
                neu[f"{art}|{s['key']}"] = {"status": "fertig", "kennung": kennung,
                                            "text": (o / textdatei).read_text("utf-8")}
    with sperre:
        # Laufende behalten; fertige nur, wenn sie zum aktuellen Lebenslauf gehoeren
        # (koennen zwischen Lesen und Sperre fertig geworden sein).
        behalten = {k: v for k, v in bewerbungen.items() if k not in neu and (
            v.get("status") == "laeuft"
            or (v.get("status") == "fertig" and v.get("kennung") == kennung))}
        bewerbungen.clear()
        bewerbungen.update(neu)
        bewerbungen.update(behalten)


def _laden():
    global suche
    if SUCHE_DATEI.exists():
        suche = json.loads(SUCHE_DATEI.read_text("utf-8"))
    _bewerbungen_laden()


def _hochladen(eingabe):
    """Unter der Sperre aufrufen. Der neue Lebenslauf wird erst geprueft, dann per
    os.replace in einem Schritt an seinen Platz gelegt; erst danach verschwindet der
    alte. So gibt es nie einen Moment ohne gueltigen Lebenslauf."""
    name = Path(str(eingabe.get("name", ""))).name
    endung = Path(name).suffix.lower()
    if endung not in (".docx", ".pdf"):
        raise RuntimeError("Bitte eine Word- (.docx) oder PDF-Datei hochladen.")
    inhalt = base64.b64decode(str(eingabe.get("inhalt", "")), validate=True)
    CV_ORDNER.mkdir(parents=True, exist_ok=True)
    for rest in CV_ORDNER.glob(".upload-*"):      # Reste eines Absturzes
        rest.unlink(missing_ok=True)
    tmp = CV_ORDNER / f".upload-{time.time_ns()}{endung}"
    tmp.write_bytes(inhalt)
    try:
        lesbar = len(dokumente.lebenslauf_text(tmp)) >= 200
    except Exception:
        lesbar = False
    if not lesbar:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("Die Datei lässt sich nicht lesen (beschädigt oder eingescannt?).")
    stamm = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(name).stem).strip(" .")[:80] or "lebenslauf"
    ziel = CV_ORDNER / (stamm + endung)
    os.replace(tmp, ziel)
    os.utime(ziel)                                  # gilt sicher als neueste Datei
    for alt in CV_ORDNER.iterdir():
        if alt != ziel:
            if alt.is_dir():
                shutil.rmtree(alt, ignore_errors=True)
            else:
                alt.unlink(missing_ok=True)
    return ziel.name


def _melden(text):
    zustand["meldung"] = text


def _bewerten(cv, paket, erfahrung="egal"):
    """Faellt ein Claude-Lauf aus, bleiben die Stellen trotzdem in der Liste."""
    try:
        return ki.bewerten(cv, paket, erfahrung)
    except Exception as e:
        print("Bewertung fehlgeschlagen:", repr(e))
        return {s["key"]: (0, "nicht bewertet (Claude-Fehler)") for s in paket}


def _auswahl(stellen, stichworte):
    """Jeder Suchbegriff bekommt gleich viele Plaetze (neueste zuerst), damit ein
    breiter Begriff mit vielen Treffern die genauen nicht verdraengt."""
    gruppen = [sorted((s for s in stellen if s.get("wort") == w),
                      key=lambda s: s.get("datum") or "", reverse=True) for w in stichworte]
    auswahl = []
    for runde in range(max(map(len, gruppen), default=0)):
        auswahl += [g[runde] for g in gruppen if runde < len(g)]
    return auswahl


UMKREIS_KM = (10, 20, 30, 50)
ERFAHRUNG = {"egal": "", "einsteiger": "Einsteiger / Quereinsteiger",
             "junior": "Junior", "erfahren": "Erfahren"}
# Titel, die klar Erfahrung oder Fuehrung verlangen („Begleiter“ ist keine Leitung).
FUER_ERFAHRENE = re.compile(
    r"\b(senior|head|director|direktor(in)?|principal|kader|vice president|lead|team ?lead)\b"
    r"|\bsr\.(?=\s)|\bchef(in)?\b(?! de (rang|partie|cuisine))"
    r"|\b\w*(?<!beg)(?<!halb)leiter(in|innen)?\b|\b\w*leitung\b"
    r"|key[ -]?account|geschäftsführer", re.I)
# „Assistenz der Geschäftsleitung“ ist keine Fuehrungsstelle.
KEINE_FUEHRUNG = re.compile(r"assist|sekretär|sekretar|sachbearbeit", re.I)
FUER_EINSTEIGER = re.compile(
    r"junior|einsteiger|quereinsteig|trainee|\bprakti(kum|kant)(in)?\b|"
    r"ohne (berufs)?erfahrung|keine (berufs)?erfahrung|"
    r"erfahrung (ist )?nicht (zwingend|nötig|notwendig)", re.I)
# Verlangte Berufserfahrung im Inseratetext.
_ZAHL = {"ein": 1, "eins": 1, "einem": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5,
         "sechs": 6, "sieben": 7, "acht": 8, "neun": 9, "zehn": 10, "one": 1, "two": 2,
         "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "ten": 10,
         "un": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5}
_N = r"(?<![\d'’.,])\b(\d{1,2}|" + "|".join(_ZAHL) + r")\b"
# „erfahrung“ deckt Berufs-, Verkaufs-, Praxiserfahrung ab; „Praxis“ allein waere
# auch die Arztpraxis.
_ERF = r"(erfahrung|experience|expérience)"
_JAHRE = r"\s*\+?\s*(?:(?:-|–|bis|to|à)\s*\d{1,2}\s*)?\+?\s*(?:jahre?n?|years?|ans)\b"
JAHRE_VOR = re.compile(_N + _JAHRE + r"[^.\n•;:]{0,25}?" + _ERF, re.I)
JAHRE_NACH = re.compile(_ERF + r"[^.\n•;]{0,40}?" + _N + _JAHRE, re.I)
# Davor stehend meint die Zahl ein Alter oder das Firmenalter, keine Erfahrung.
KEINE_ERFAHRUNGSZAHL = re.compile(r"(seit|über|alter|kinder\w*|zwischen|und|ab|jugendliche\w*|"
                                  r"schüler\w*|personen|menschen)\s*$", re.I)
_BEZUG = r"[^.\n•;]{0,40}?" + _ERF          # Wort muss sich auf Erfahrung beziehen
MEHRJAEHRIG = re.compile(r"(mehrjährig\w*|mehrere jahre|several years|plusieurs années)" + _BEZUG
                         + r"|expérience confirmée|ausgewiesene \w*erfahrung"
                         r"|fundierte \w*erfahrung|solide \w*erfahrung", re.I)
LANGJAEHRIG = re.compile(r"(langjährig\w*|many years)" + _BEZUG
                         + r"|extensive experience|longue expérience", re.I)
EINIGE = re.compile(r"(einige jahre|some years|quelques années)" + _BEZUG, re.I)


def verlangte_jahre(text):
    """Grob: wie viele Jahre Erfahrung verlangt das Inserat? None = nichts erkannt."""
    text = re.sub(r"\\", "", text or "")
    jahre = []
    for m in list(JAHRE_VOR.finditer(text)) + list(JAHRE_NACH.finditer(text)):
        gruppe = 1 if m.re is JAHRE_VOR else 2
        wort = m.group(gruppe)
        if KEINE_ERFAHRUNGSZAHL.search(text[max(0, m.start(gruppe) - 25):m.start(gruppe)]):
            continue
        n = int(wort) if wort.isdigit() else _ZAHL.get(wort.lower())
        if n and n <= 15:          # „seit 50 Jahren Erfahrung“ meint die Firma
            jahre.append(n)
    if LANGJAEHRIG.search(text):
        jahre.append(5)
    if MEHRJAEHRIG.search(text):
        jahre.append(4)            # „mehrjährig“ ist mehr als Junior (1–3 Jahre)
    if EINIGE.search(text):
        jahre.append(2)
    return max(jahre) if jahre else None


# Ab so vielen verlangten Jahren faellt eine Stelle weg.
ZU_VIEL_JAHRE = {"einsteiger": 2, "junior": 4}
MAX_STICHWORTE = 8
MAX_AUSGEBLENDET = 300


def _gebiet(eingabe):
    """Eingabe der Seite -> geprueftes Gebiet und Text fuer die Anzeige."""
    art = str(eingabe.get("region") or "schweiz")
    if art == "kantone":
        codes = []
        liste = eingabe.get("kantone")
        for name in liste if isinstance(liste, list) else []:
            code = portale._kanton_code(str(name))
            if code and code not in codes:
                codes.append(code)
        if not codes:
            raise RuntimeError("Bitte mindestens einen Kanton anhaken.")
        return {"art": "kantone", "codes": codes}, ", ".join(portale.KANTON_NAMEN[c] for c in codes)
    if art == "umkreis":
        ort = str(eingabe.get("umkreis_ort") or "").strip()[:80]
        try:
            km = int(eingabe.get("umkreis_km") or 0)
        except (TypeError, ValueError):
            km = 0
        if not ort:
            raise RuntimeError("Bitte für den Umkreis einen Ort oder eine PLZ eingeben.")
        if km not in UMKREIS_KM:
            raise RuntimeError("Ungültiger Umkreis.")
        try:
            treffer = portale.ort_suchen(ort)
        except Exception:
            raise RuntimeError("Das Ortsverzeichnis (Job-Room) ist gerade nicht erreichbar.") from None
        t = treffer and ((treffer["genau"] or [None])[0] or treffer["erster"])
        if not t:
            raise RuntimeError(f"Ort „{ort}“ nicht gefunden. Bitte anders schreiben oder die PLZ nehmen.")
        g = t["geoPoint"]
        return ({"art": "umkreis", "ort": t["city"], "km": km, "lat": float(g["lat"]),
                 "lon": float(g["lon"]), "kanton": t.get("cantonCode")},
                f"{km} km um {t['city']}")
    return {"art": "schweiz"}, "ganze Schweiz"


def _woerter(text):
    return [w.strip().lower() for w in re.split(r"[,;\n]", str(text or "")) if w.strip()][:20]


def _filtern(stellen, erfahrung, ohne):
    """Teilt in (bleiben, ausgeblendet). Jede ausgeblendete Stelle sagt, warum."""
    bleiben, weg = [], []
    # Ab 4 Buchstaben auch mitten im Wort („techniker“ trifft „Handytechniker“),
    # kuerzere nur am Wortanfang (sonst traefe „it“ fast jede Stelle).
    muster = [(w, re.compile(("" if len(w) >= 4 else r"(?<!\w)") + re.escape(w), re.I))
              for w in ohne]
    for s in stellen:
        s["titel"] = s.get("titel") or ""
        inhalt = f'{s["titel"]} {s.get("vorschau") or ""}'
        wort = next((w for w, m in muster if m.search(inhalt)), None)
        titel = s["titel"]
        if wort:
            s["warum"] = f"„{wort}“"
        elif (erfahrung in ("einsteiger", "junior") and FUER_ERFAHRENE.search(titel)
              and not FUER_EINSTEIGER.search(titel) and not KEINE_FUEHRUNG.search(titel)):
            s["warum"] = "für Erfahrene"
        else:
            s["einsteiger"] = (erfahrung in ("einsteiger", "junior")
                               and bool(FUER_EINSTEIGER.search(inhalt)))
            bleiben.append(s)
            continue
        weg.append(_schlank(s, s["warum"]))
    return bleiben, weg


def _schlank(s, warum):
    return {**{k: s.get(k) for k in ("key", "titel", "firma", "ort", "portal", "km", "link")},
            "warum": warum}


def _gemischt(weg):
    """Hoechstens MAX_AUSGEBLENDET, abwechselnd aus jedem Grund, damit kein Grund
    die anderen verdraengt."""
    gruppen = {}
    for w in weg:
        gruppen.setdefault(w["warum"], []).append(w)
    ergebnis, nr = [], 0
    while len(ergebnis) < MAX_AUSGEBLENDET and any(nr < len(g) for g in gruppen.values()):
        ergebnis += [g[nr] for g in gruppen.values() if nr < len(g)]
        nr += 1
    return ergebnis[:MAX_AUSGEBLENDET]


def _titel_passt(stellen, stichworte):
    """Portale suchen im ganzen Text: „Sales“ bringt sonst auch eine Radiologie-
    Fachfrau, weil die Firma „Sales“ im Text hat. Behalten wird nur, wessen Titel
    den Suchbegriff oder ein gleichbedeutendes Wort enthaelt."""
    if not stellen:
        return stellen, []
    _melden("Claude sucht gleichbedeutende Berufswörter …")
    try:
        woerter = ki.titelwoerter(stichworte)
    except Exception as e:
        # Ohne Claude wuerde der woertliche Begriff zu viel wegwerfen
        # („Sozialberatung“ trifft „Sozialberater“ nicht): dann nicht filtern.
        print("Titelwörter fehlgeschlagen:", repr(e))
        return stellen, []
    gleich = lambda x: re.sub(r"[-/_:*]+", " ", (x or "").lower())
    teile = set()
    for w in stichworte:
        teile.add(gleich(w))
        teile.update(t for t in gleich(w).split() if len(t) >= 4)   # „Sales Manager“
    for liste in woerter.values():
        teile.update(gleich(x) for x in liste)
    teile = [t.strip() for t in teile if len(t.strip()) >= 4]
    bleiben, weg = [], []
    for s in stellen:
        titel = gleich(s.get("titel"))
        (bleiben if any(t in titel for t in teile) else weg).append(s)
    if not bleiben:          # lieber alles zeigen als nichts (z. B. Tippfehler)
        return stellen, []
    return bleiben, [_schlank(s, "Titel passt nicht zum Suchbegriff") for s in weg]


def _erfahrung_im_text(stellen, erfahrung):
    """Liest die Inseratetexte und blendet aus, was zu viel Erfahrung verlangt."""
    texte = portale.texte_holen(stellen, _melden)
    grenze = ZU_VIEL_JAHRE[erfahrung]
    bleiben, weg = [], []
    for s in stellen:
        text = texte.get(s["key"])
        s["ungeprueft"] = text is None
        jahre = verlangte_jahre(text or s.get("vorschau") or "")
        # „Quereinsteiger willkommen“ wiegt mehr als „idealerweise 2 Jahre“.
        if jahre is not None and jahre >= grenze and not s.get("einsteiger"):
            weg.append(_schlank(s, "verlangt mehrjährige Erfahrung"))
        else:
            bleiben.append(s)
    return bleiben, weg


def suche_starten(eingabe):
    global suche
    try:
        # Der Lebenslauf ist fuer die Suche freiwillig: ohne ihn gibt es alle
        # Treffer (neueste zuerst), mit ihm bewertet Claude die Passung.
        gebiet, ort = _gebiet(eingabe)        # Eingabefehler sofort, nicht erst nach Claude
        _, _, cv = _aktueller_cv(text=True)
        stichworte = [s.strip() for s in re.split(r"[,;\n]", str(eingabe.get("stichworte", ""))) if s.strip()]
        eigene_stichworte = bool(stichworte)
        if not stichworte:
            if not cv:
                raise RuntimeError("Bitte ein Stichwort eingeben (z. B. „Sozialberatung“). "
                                   "Mit hochgeladenem Lebenslauf wählt Claude die Stichworte selbst.")
            _melden("Claude leitet Suchbegriffe aus dem Lebenslauf ab …")
            stichworte = ki.suchbegriffe(cv)
        stichworte = list({w.lower(): w for w in stichworte}.values())[:MAX_STICHWORTE]
        erfahrung = str(eingabe.get("erfahrung") or "egal")
        if erfahrung not in ERFAHRUNG:
            erfahrung = "egal"
        ohne = _woerter(eingabe.get("ohne"))
        von, bis = int(eingabe.get("pensum_von") or 0), int(eingabe.get("pensum_bis") or 100)
        stellen, fehler = portale.suchen(stichworte, gebiet, von, bis, _melden)
        if not stellen:
            raise RuntimeError("Keine Stellen gefunden. " + "; ".join(fehler))
        stellen, ausgeblendet = _filtern(_auswahl(stellen, stichworte), erfahrung, ohne)
        if eigene_stichworte:      # Claudes Begriffe aus dem Lebenslauf sind breiter gemeint
            stellen, weg = _titel_passt(stellen, stichworte)
            ausgeblendet += weg
        if erfahrung in ZU_VIEL_JAHRE and stellen:
            stellen, weg = _erfahrung_im_text(stellen, erfahrung)
            ausgeblendet += weg
        anzahl_weg = {}
        for w in ausgeblendet:
            anzahl_weg[w["warum"]] = anzahl_weg.get(w["warum"], 0) + 1
        ausgeblendet = _gemischt(ausgeblendet)
        # Einsteiger-Stellen zuerst: sie kommen sicher in die Bewertung und stehen
        # ohne Lebenslauf oben (sort ist stabil, die Reihenfolge sonst bleibt).
        stellen.sort(key=lambda s: not s.get("einsteiger"))
        if cv and stellen:
            stellen = stellen[:MAX_BEWERTEN]
            _melden(f"Claude prüft {len(stellen)} Stellen gegen deinen Lebenslauf (1–3 Min.) …")
            pakete = [stellen[i:i + PRO_KI_LAUF] for i in range(0, len(stellen), PRO_KI_LAUF)]
            punkte = {}
            with ThreadPoolExecutor(min(len(pakete), KI_GLEICHZEITIG)) as pool:
                for teil in pool.map(lambda p: _bewerten(cv, p, erfahrung), pakete):
                    punkte.update(teil)
            for s in stellen:
                s["punkte"], s["grund"] = punkte.get(s["key"], (0, "nicht bewertet"))
            stellen.sort(key=lambda s: s["punkte"], reverse=True)
        else:
            for s in stellen:
                s["punkte"], s["grund"] = None, ""
            stellen.sort(key=lambda s: (bool(s.get("einsteiger")), s.get("datum") or ""), reverse=True)
        suche = {"zeit": time.strftime("%d.%m.%Y %H:%M"), "bewertet": bool(cv and stellen),
                 "stichworte": stichworte, "ort": ort, "pensum": [von, bis],
                 "erfahrung": ERFAHRUNG[erfahrung], "ohne": ohne,
                 "stellen": stellen, "ausgeblendet": ausgeblendet, "ausgeblendet_anzahl": anzahl_weg, "portal_fehler": fehler}
        _speichern()
        _bewerbungen_laden()
        zustand["fehler"] = None
    except Exception as e:
        zustand["fehler"] = str(e)
        print("Suche fehlgeschlagen:", repr(e))
    finally:
        zustand["laeuft"] = False
        zustand["meldung"] = ""


def _foto(o):
    return next((o / f"foto.{e}" for e in ("png", "jpg") if (o / f"foto.{e}").exists()), None)


def _inserat(o, stelle):
    """Inseratetext einmal holen und ablegen. Schreiben ueber eine Zwischendatei,
    damit ein paralleler Lauf (Lebenslauf + Anschreiben) nie einen halben Text liest."""
    datei = o / "inserat.txt"
    if datei.exists():
        return datei.read_text("utf-8")
    inserat = portale.inseratetext(stelle)
    tmp = o / f".inserat-{threading.get_ident()}.txt"
    tmp.write_text(inserat, "utf-8")
    os.replace(tmp, datei)
    return inserat


pdf_sperre = threading.Lock()   # Neuschreiben und Download nie gleichzeitig an einer PDF


def _pdf(art, text, o):
    """PDF schreiben; ein unbrauchbares Foto darf den Lebenslauf nie verhindern."""
    pdfdatei = o / ARTEN[art][1]
    if art == "anschreiben":
        return dokumente.anschreiben_pdf(text, pdfdatei)
    foto = _foto(o)
    try:
        dokumente.lebenslauf_pdf(text, pdfdatei, foto.read_bytes() if foto else None)
    except Exception as e:
        print("PDF mit Foto fehlgeschlagen:", repr(e))
        dokumente.lebenslauf_pdf(text, pdfdatei)


def dokument_erstellen(art, key, stelle, cv, cv_pfad, kennung, geaendert=None):
    """Erstellt den Lebenslauf ODER das Anschreiben (getrennt). Mit `geaendert`
    (Text aus dem Fenster) ueberarbeitet Claude die Fassung der Person — die geht
    in keinem Fehlerfall verloren. cv, cv_pfad und kennung wurden beim Klick
    festgehalten."""
    eintrag = bewerbungen[f"{art}|{key}"]
    textdatei = ARTEN[art][0]
    o = _ordner(key, kennung)
    name = "Lebenslauf" if art == "lebenslauf" else "Anschreiben"
    vorher = (o / textdatei).read_text("utf-8") if (o / textdatei).exists() else None
    ergebnis, fehler = None, None
    try:
        o.mkdir(parents=True, exist_ok=True)
        eintrag["meldung"] = "Hole den vollen Inseratetext …"
        inserat = _inserat(o, stelle)
        if art == "lebenslauf" and not _foto(o) and cv_pfad:
            try:                                  # Foto aus dem Original uebernehmen;
                bild = dokumente.foto(cv_pfad)    # ein Fehler hier kostet nur das Foto
                if bild:
                    (o / "foto.jpg").write_bytes(bild)
            except Exception as e:
                print("Foto übersprungen:", repr(e))
        datum = time.strftime("%d.%m.%Y")
        if geaendert is None:
            eintrag["meldung"] = f"Claude schreibt den {name} (1–2 Min.) …"
            ergebnis = (ki.lebenslauf(cv, stelle, inserat) if art == "lebenslauf"
                        else ki.anschreiben(cv, stelle, inserat, datum))
        else:
            eintrag["meldung"] = f"Claude baut deine Änderungen in den {name} ein (1–2 Min.) …"
            ergebnis = ki.ueberarbeiten(art, cv, stelle, inserat, vorher or "", geaendert, datum)
    except Exception as e:
        print("Dokument fehlgeschlagen:", repr(e))
        fehler = str(e)
    if ergebnis is None and geaendert is not None:
        ergebnis = geaendert                      # deine Fassung zaehlt, auch ohne Claude
        fehler = f"Überarbeitung fehlgeschlagen ({fehler}). Deine Fassung ist unverändert gespeichert."
    try:
        if ergebnis is not None:
            o.mkdir(parents=True, exist_ok=True)
            with pdf_sperre:
                (o / ARTEN[art][1]).unlink(missing_ok=True)   # nie eine veraltete PDF-Datei
                try:
                    _pdf(art, ergebnis, o)
                except Exception as e:
                    print("PDF fehlgeschlagen:", repr(e))
                    fehler = "Die PDF-Datei konnte nicht erstellt werden: " + str(e)
                (o / textdatei).write_text(ergebnis, "utf-8")   # zuletzt: erst jetzt fertig
    except OSError as e:
        print("Speichern fehlgeschlagen:", repr(e))
        fehler, ergebnis = "Speichern fehlgeschlagen: " + str(e), ergebnis
    text = ergebnis if ergebnis is not None else vorher
    with sperre:   # Pruefen und Setzen in einem Schritt (Upload dazwischen)
        if _cv_kennung(_lebenslauf()) != kennung:
            if bewerbungen.get(f"{art}|{key}") is eintrag:
                bewerbungen.pop(f"{art}|{key}", None)   # inzwischen anderer Lebenslauf
        elif text is not None:
            eintrag.update(status="fertig", kennung=kennung, text=text, fehler=fehler)
        else:
            eintrag.update(status="fehler", fehler=fehler or "Unbekannter Fehler")


def _stelle(key):
    return next((s for s in suche.get("stellen", []) if s["key"] == key), None)


class Handler(BaseHTTPRequestHandler):
    def _senden(self, code, inhalt, typ="application/json; charset=utf-8", extra=None):
        if not isinstance(inhalt, bytes):
            inhalt = (json.dumps(inhalt, ensure_ascii=False) if "json" in typ else inhalt).encode()
        self.send_response(code)
        self.send_header("Content-Type", typ)
        self.send_header("Content-Length", str(len(inhalt)))
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(inhalt)

    def log_message(self, *a):
        pass

    def _fremd(self):
        if self.headers.get("Host") not in EIGENE_HOSTS:
            return True
        # Browser melden, ob eine fremde Seite die Anfrage ausloest (z. B. <img> auf
        # das Foto). Die Startseite selbst darf man von ueberall aufrufen.
        if (self.path.split("?")[0] != "/"
                and self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none")):
            return True
        herkunft = self.headers.get("Origin")
        return bool(herkunft) and herkunft not in EIGENE_HERKUNFT

    def do_GET(self):
        if self._fremd():
            return self._senden(403, {"fehler": "verboten"})
        pfad = self.path.split("?")[0]
        if pfad == "/":
            return self._senden(200, (HIER / "index.html").read_bytes(), "text/html; charset=utf-8")
        if pfad == "/api/lebenslauf":
            cv = _lebenslauf()
            return self._senden(200, {"name": Path(cv).name if cv else None})
        if pfad == "/api/stand":
            with sperre:
                status = {k: v.get("status") for k, v in bewerbungen.items()}   # "art|key"
            return self._senden(200, {"zustand": zustand, "suche": suche,
                                      "mindestens": MINDESTENS, "bewerbungen": status})
        m = re.fullmatch(r"/api/bewerbung/(lebenslauf|anschreiben)/(.+)", pfad)
        if m:
            key = unquote(m.group(2))
            with sperre:
                e = dict(bewerbungen.get(f"{m.group(1)}|{key}", {"status": "keine"}))
            if m.group(1) == "lebenslauf" and _stelle(key):
                e["foto"] = bool(_foto(_ordner(key, _aktueller_cv()[1])))
            return self._senden(200, e)
        m = re.fullmatch(r"/foto/(.+)", pfad)
        if m and _stelle(unquote(m.group(1))):
            foto = _foto(_ordner(unquote(m.group(1)), _aktueller_cv()[1]))
            if foto:
                return self._senden(200, foto.read_bytes(),
                                    "image/png" if foto.suffix == ".png" else "image/jpeg",
                                    {"Cache-Control": "no-store"})
        m = re.fullmatch(r"/download/(.+)/(Lebenslauf|Anschreiben)\.pdf", pfad)
        if m and _stelle(unquote(m.group(1))):
            o = _ordner(unquote(m.group(1)), _aktueller_cv()[1])
            art = m.group(2).lower()
            datei = o / ARTEN[art][1]
            with pdf_sperre:
                if not datei.exists() and (o / ARTEN[art][0]).exists():
                    try:                  # aeltere Bewerbung: es gab nur Word
                        _pdf(art, (o / ARTEN[art][0]).read_text("utf-8"), o)
                    except Exception as e:
                        print("PDF fehlgeschlagen:", repr(e))
                        return self._senden(500, {"fehler": "Die PDF-Datei konnte nicht erstellt werden: " + str(e)})
                inhalt = datei.read_bytes() if datei.exists() else None
            if inhalt:
                return self._senden(200, inhalt, "application/pdf",
                                    {"Content-Disposition": f'attachment; filename="{m.group(2)}.pdf"'})
        self._senden(404, {"fehler": "nicht gefunden"})

    def do_POST(self):
        if self._fremd() or not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._senden(403, {"fehler": "verboten"})
        try:
            laenge = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            laenge = -1
        if laenge < 0:
            return self._senden(400, {"fehler": "ungültige Länge"})
        if laenge > (MAX_LEBENSLAUF if self.path == "/api/lebenslauf" else MAX_ANFRAGE):
            return self._senden(413, {"fehler": "zu gross"})
        try:
            eingabe = json.loads(self.rfile.read(laenge) or b"{}")
        except ValueError:
            return self._senden(400, {"fehler": "ungültige Eingabe"})
        if self.path == "/api/lebenslauf":
            with sperre:
                try:
                    name = _hochladen(eingabe)
                except (RuntimeError, ValueError) as e:
                    return self._senden(400, {"fehler": str(e) if isinstance(e, RuntimeError)
                                              else "Datei beschädigt."})
                except OSError as e:
                    print("Upload fehlgeschlagen:", repr(e))
                    return self._senden(500, {"fehler": "Speichern fehlgeschlagen."})
            _bewerbungen_laden()   # Bewerbungen zum alten Lebenslauf ausblenden
            return self._senden(200, {"name": name})
        if self.path == "/api/suchen":
            with sperre:
                if zustand["laeuft"]:
                    return self._senden(409, {"fehler": "Suche läuft schon."})
                zustand.update(laeuft=True, fehler=None, meldung="Starte …")
            threading.Thread(target=suche_starten, args=(eingabe,), daemon=True).start()
            return self._senden(200, {"ok": True})
        if self.path == "/api/bewerbung":
            # {key, art} erstellt; {key, art, text} ueberarbeitet die Fassung aus dem Fenster.
            key, art = str(eingabe.get("key", "")), str(eingabe.get("art", ""))
            geaendert = eingabe.get("text")
            stelle = _stelle(key)
            if art not in ARTEN:
                return self._senden(400, {"fehler": "Unbekanntes Dokument."})
            if not stelle:
                return self._senden(404, {"fehler": "Stelle gehört nicht zur aktuellen Suche."})
            if geaendert is not None:
                geaendert = str(geaendert).strip()[:MAX_DOKUMENT]
                if len(geaendert) < 20:
                    return self._senden(400, {"fehler": "Der Text ist (fast) leer."})
            try:
                cv_pfad, kennung, cv = _aktueller_cv(text=True)
            except Exception as e:
                print("Lebenslauf nicht lesbar:", repr(e))
                return self._senden(400, {"fehler": "Der Lebenslauf lässt sich nicht lesen. Bitte neu hochladen."})
            if not cv_pfad:
                return self._senden(400, {"fehler": "Bitte zuerst oben deinen Lebenslauf hochladen."})
            with sperre:
                if bewerbungen.get(f"{art}|{key}", {}).get("status") == "laeuft":
                    return self._senden(200, {"ok": True})
                bewerbungen[f"{art}|{key}"] = {"status": "laeuft", "meldung": "Starte …"}
            threading.Thread(target=dokument_erstellen,
                             args=(art, key, stelle, cv, cv_pfad, kennung, geaendert),
                             daemon=True).start()
            return self._senden(200, {"ok": True})
        self._senden(404, {"fehler": "nicht gefunden"})


if __name__ == "__main__":
    _laden()
    print(f"Stellen-Finder läuft: http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
