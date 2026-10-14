# -*- coding: utf-8 -*-
"""Stellen-Finder — http://localhost:8813

Knopf „Stellen suchen“: Portale abfragen, Claude bewertet die Passung.
Klick auf eine Stelle: Claude passt den Lebenslauf an und schreibt das Anschreiben.
Nichts wird verschickt.
"""

import hashlib
import json
import re
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
MAX_BEWERTEN = 160    # mehr Stellen machen die Bewertung langsam und ungenau
PRO_KI_LAUF = 40
MINDESTENS = 50       # ab dieser Punktzahl gilt eine Stelle als passend
MAX_ANFRAGE = 64_000
# Nur Aufrufe von der eigenen Seite annehmen (Schutz gegen fremde Webseiten,
# die per DNS-Rebinding oder CSRF auf den lokalen Server zugreifen wollen).
EIGENE_HOSTS = (f"localhost:{PORT}", f"127.0.0.1:{PORT}")
EIGENE_HERKUNFT = tuple(f"http://{h}" for h in EIGENE_HOSTS)
DATEIEN = ("Lebenslauf.docx", "Anschreiben.docx", "anschreiben.txt", "lebenslauf.md")

zustand = {"laeuft": False, "meldung": "", "fehler": None}
suche = {}            # letzte Suche (auch nach Neustart aus der Datei)
bewerbungen = {}      # key -> {"status", "fehler", "lebenslauf", "anschreiben"}
sperre = threading.Lock()


def _ordner(key, cv=None):
    """Eine Bewerbung gehoert zu Stelle UND Lebenslauf: anderer Lebenslauf,
    anderer Ordner. Der Hash macht den Namen ausserdem pfadsicher."""
    cv = cv or suche.get("lebenslauf", "")
    return DATEN / "bewerbungen" / hashlib.sha1(f"{key}|{cv}".encode()).hexdigest()[:20]


def _speichern():
    DATEN.mkdir(exist_ok=True)
    SUCHE_DATEI.write_text(json.dumps(suche, ensure_ascii=False, indent=1), "utf-8")


def _bewerbungen_laden():
    """Fertige Bewerbungen zur aktuellen Suche (gleicher Lebenslauf) von der Platte."""
    neu = {}
    for s in suche.get("stellen", []):
        o = _ordner(s["key"])
        if all((o / d).exists() for d in DATEIEN):
            neu[s["key"]] = {
                "status": "fertig",
                "lebenslauf": (o / "lebenslauf.md").read_text("utf-8"),
                "anschreiben": (o / "anschreiben.txt").read_text("utf-8")}
    with sperre:
        laufend = {k: v for k, v in bewerbungen.items() if v.get("status") == "laeuft"}
        bewerbungen.clear()
        bewerbungen.update(neu)
        bewerbungen.update(laufend)


def _laden():
    global suche
    if SUCHE_DATEI.exists():
        suche = json.loads(SUCHE_DATEI.read_text("utf-8"))
    _bewerbungen_laden()


def _melden(text):
    zustand["meldung"] = text


def _bewerten(cv, paket):
    """Faellt ein Claude-Lauf aus, bleiben die Stellen trotzdem in der Liste."""
    try:
        return ki.bewerten(cv, paket)
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
    return auswahl[:MAX_BEWERTEN]


def suche_starten(eingabe):
    global suche
    try:
        cv_pfad = eingabe.get("lebenslauf", "")
        if cv_pfad not in dokumente.lebenslaeufe():
            raise RuntimeError("Lebenslauf-Datei nicht gefunden.")
        _melden("Lese Lebenslauf …")
        cv = dokumente.lebenslauf_text(cv_pfad)
        stichworte = [s.strip() for s in re.split(r"[,;\n]", str(eingabe.get("stichworte", ""))) if s.strip()]
        if not stichworte:
            _melden("Claude leitet Suchbegriffe aus dem Lebenslauf ab …")
            stichworte = ki.suchbegriffe(cv)
        stichworte = list({w.lower(): w for w in stichworte}.values())
        ort = str(eingabe.get("ort", "")).strip()
        von, bis = int(eingabe.get("pensum_von") or 0), int(eingabe.get("pensum_bis") or 100)
        stellen, fehler = portale.suchen(stichworte, ort, von, bis, _melden)
        if not stellen:
            raise RuntimeError("Keine Stellen gefunden. " + "; ".join(fehler))
        stellen = _auswahl(stellen, stichworte)
        _melden(f"Claude prüft {len(stellen)} Stellen gegen deinen Lebenslauf (1–3 Min.) …")
        pakete = [stellen[i:i + PRO_KI_LAUF] for i in range(0, len(stellen), PRO_KI_LAUF)]
        punkte = {}
        with ThreadPoolExecutor(len(pakete)) as pool:
            for teil in pool.map(lambda p: _bewerten(cv, p), pakete):
                punkte.update(teil)
        for s in stellen:
            s["punkte"], s["grund"] = punkte.get(s["key"], (0, "nicht bewertet"))
        stellen.sort(key=lambda s: s["punkte"], reverse=True)
        suche = {"zeit": time.strftime("%d.%m.%Y %H:%M"), "lebenslauf": cv_pfad,
                 "stichworte": stichworte, "ort": ort, "pensum": [von, bis],
                 "stellen": stellen, "portal_fehler": fehler}
        _speichern()
        _bewerbungen_laden()
        zustand["fehler"] = None
    except Exception as e:
        zustand["fehler"] = str(e)
        print("Suche fehlgeschlagen:", repr(e))
    finally:
        zustand["laeuft"] = False
        zustand["meldung"] = ""


def bewerbung_erstellen(key, stelle, cv_pfad):
    eintrag = bewerbungen[key]
    try:
        eintrag["meldung"] = "Hole den vollen Inseratetext …"
        inserat = portale.inseratetext(stelle)
        cv = dokumente.lebenslauf_text(cv_pfad)
        eintrag["meldung"] = "Claude schreibt Lebenslauf und Anschreiben (1–3 Min.) …"
        lebenslauf, anschreiben = ki.bewerbung(cv, stelle, inserat, time.strftime("%d.%m.%Y"))
        o = _ordner(key, cv_pfad)
        o.mkdir(parents=True, exist_ok=True)
        (o / "inserat.txt").write_text(inserat, "utf-8")
        # Reihenfolge: lebenslauf.md zuletzt — erst dann gilt die Bewerbung als fertig.
        dokumente.lebenslauf_docx(lebenslauf, o / "Lebenslauf.docx")
        dokumente.anschreiben_docx(anschreiben, o / "Anschreiben.docx")
        (o / "anschreiben.txt").write_text(anschreiben, "utf-8")
        (o / "lebenslauf.md").write_text(lebenslauf, "utf-8")
        if suche.get("lebenslauf") == cv_pfad:
            eintrag.update(status="fertig", lebenslauf=lebenslauf, anschreiben=anschreiben)
        elif bewerbungen.get(key) is eintrag:
            bewerbungen.pop(key, None)   # inzwischen anderer Lebenslauf gewählt
    except Exception as e:
        print("Bewerbung fehlgeschlagen:", repr(e))
        eintrag.update(status="fehler", fehler=str(e))


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
        herkunft = self.headers.get("Origin")
        return bool(herkunft) and herkunft not in EIGENE_HERKUNFT

    def do_GET(self):
        if self._fremd():
            return self._senden(403, {"fehler": "verboten"})
        pfad = self.path.split("?")[0]
        if pfad == "/":
            return self._senden(200, (HIER / "index.html").read_bytes(), "text/html; charset=utf-8")
        if pfad == "/api/lebenslaeufe":
            return self._senden(200, dokumente.lebenslaeufe())
        if pfad == "/api/stand":
            with sperre:
                status = {k: v.get("status") for k, v in bewerbungen.items()}
            return self._senden(200, {"zustand": zustand, "suche": suche,
                                      "mindestens": MINDESTENS, "bewerbungen": status})
        m = re.fullmatch(r"/api/bewerbung/(.+)", pfad)
        if m:
            return self._senden(200, bewerbungen.get(unquote(m.group(1)), {"status": "keine"}))
        m = re.fullmatch(r"/download/(.+)/(Lebenslauf|Anschreiben)\.docx", pfad)
        if m and _stelle(unquote(m.group(1))):
            datei = _ordner(unquote(m.group(1))) / f"{m.group(2)}.docx"
            if datei.exists():
                return self._senden(200, datei.read_bytes(),
                                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                    {"Content-Disposition": f'attachment; filename="{m.group(2)}.docx"'})
        self._senden(404, {"fehler": "nicht gefunden"})

    def do_POST(self):
        if self._fremd() or not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._senden(403, {"fehler": "verboten"})
        laenge = int(self.headers.get("Content-Length") or 0)
        if laenge > MAX_ANFRAGE:
            return self._senden(413, {"fehler": "zu gross"})
        try:
            eingabe = json.loads(self.rfile.read(laenge) or b"{}")
        except ValueError:
            return self._senden(400, {"fehler": "ungültige Eingabe"})
        if self.path == "/api/suchen":
            with sperre:
                if zustand["laeuft"]:
                    return self._senden(409, {"fehler": "Suche läuft schon."})
                zustand.update(laeuft=True, fehler=None, meldung="Starte …")
            threading.Thread(target=suche_starten, args=(eingabe,), daemon=True).start()
            return self._senden(200, {"ok": True})
        if self.path == "/api/bewerbung":
            key = str(eingabe.get("key", ""))
            stelle = _stelle(key)
            if not stelle:
                return self._senden(404, {"fehler": "Stelle gehört nicht zur aktuellen Suche."})
            with sperre:
                if bewerbungen.get(key, {}).get("status") == "laeuft":
                    return self._senden(200, {"ok": True})
                bewerbungen[key] = {"status": "laeuft", "meldung": "Starte …"}
            threading.Thread(target=bewerbung_erstellen, args=(key, stelle, suche["lebenslauf"]),
                             daemon=True).start()
            return self._senden(200, {"ok": True})
        self._senden(404, {"fehler": "nicht gefunden"})


if __name__ == "__main__":
    _laden()
    print(f"Stellen-Finder läuft: http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
