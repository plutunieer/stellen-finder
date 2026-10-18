# -*- coding: utf-8 -*-
"""portale.py — Stellen aus Schweizer Portalen holen.

Angebunden: jobs.ch, jobup.ch (beide JobCloud, gleiche Schnittstelle),
LinkedIn (oeffentliche Gast-Suche) und Job-Room (Stellenportal des Bundes/RAV,
enthaelt auch viele Inserate anderer Portale).
Indeed sperrt automatische Abfragen (Antwort 403) und fehlt deshalb.

Jede Stelle ist ein dict mit: key, portal, titel, firma, ort, pensum, datum,
link, vorschau (kurzer Text) und optional text (voller Inseratetext).
"""

import html
import json
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130 Safari/537.36")
ZEITLIMIT = 25
PRO_ABFRAGE = 20        # jobs.ch/jobup.ch geben hoechstens 20 je Seite heraus
MAX_PRO_PORTAL = 300    # Treffer je Portal, Stichwort und Kanton
SEITEN_GESAMT = 30       # jobs.ch/jobup.ch: Seiten je Stichwort ueber alle Kantone
LINKEDIN_SEITEN = 5     # je 10 Treffer; LinkedIn sperrt bei vielen Abfragen (429)


def _holen(url, daten=None):
    kopf = {"User-Agent": UA, "Accept-Language": "de-CH,de;q=0.9"}
    if daten is not None:
        kopf["Content-Type"] = "application/json"
        daten = json.dumps(daten).encode()
    anfrage = urllib.request.Request(url, data=daten, headers=kopf)
    with urllib.request.urlopen(anfrage, timeout=ZEITLIMIT) as antwort:
        return antwort.read(8_000_000).decode("utf-8", "replace")   # Grenze gegen Riesenantworten


def text_aus_html(roh):
    roh = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", roh or "")
    roh = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>|</div>", "\n", roh)
    roh = re.sub(r"(?i)<li[^>]*>", "\n• ", roh)
    roh = html.unescape(re.sub(r"<[^>]+>", " ", roh))
    roh = re.sub(r"[ \t\xa0]+", " ", roh)
    return re.sub(r"\n\s*\n+", "\n\n", roh).strip()


def _sicher(link):
    """Nur echte https-Links auf die Seite lassen."""
    return link if str(link or "").startswith("https://") else ""


def _pensum(von, bis):
    try:
        von, bis = int(von or 0), int(bis or 0)
    except (TypeError, ValueError):
        return None
    if not bis:
        return None
    return (von, bis)


# --- jobs.ch / jobup.ch -------------------------------------------------------

def _jobcloud_seiten(domain, stichwort, ort, max_seiten=MAX_PRO_PORTAL // PRO_ABFRAGE):
    """Alle Ergebnisseiten (je 20) bis MAX_PRO_PORTAL, gleichzeitig abgerufen."""
    q = {"query": stichwort, "rows": PRO_ABFRAGE}
    if ort:
        q["location"] = ort
    seite = lambda nr: json.loads(_holen(f"https://www.{domain}/api/v1/public/search?"
                                         + urllib.parse.urlencode({**q, "page": nr})))
    def seite_sicher(nr):          # eine kaputte Seite kostet nur ihre 20 Treffer
        try:
            return seite(nr)
        except Exception:
            return {}

    erste = seite(1)
    letzte = min(int(erste.get("num_pages") or 1), max_seiten)
    dokumente = list(erste.get("documents", []))
    if letzte > 1:
        with ThreadPoolExecutor(6) as pool:
            for daten in pool.map(seite_sicher, range(2, letzte + 1)):
                dokumente += daten.get("documents", [])
    return dokumente


def _jobcloud(portal, domain, stichwort, gebiet):
    if gebiet["art"] == "schweiz":
        orte = [None]
    else:
        # Umkreis: alle Kantone, die den Kreis beruehren koennen; genau gefiltert
        # wird danach ueber die Koordinaten jeder Stelle.
        codes = gebiet["codes"] if gebiet["art"] == "kantone" else _kantone_im_umkreis(gebiet)
        orte = [JOBCLOUD_ORT.get(c, KANTON_NAMEN[c]) for c in codes]
    # Viele Kantone: weniger Seiten je Kanton, damit eine Suche nicht Hunderte
    # Abfragen ausloest. Beim Umkreis bekommt der Kanton des Orts alle Seiten,
    # die Nachbarkantone teilen sich den Rest.
    voll = MAX_PRO_PORTAL // PRO_ABFRAGE
    if gebiet["art"] == "umkreis":
        nachbarn = max(1, len(orte) - 1)
        seiten = [voll if c == gebiet["kanton"] else max(2, SEITEN_GESAMT // nachbarn) for c in codes]
    else:
        seiten = [max(1, min(voll, SEITEN_GESAMT // len(orte)))] * len(orte)
    dokumente, fehler = [], None
    for ort, je_kanton in zip(orte, seiten):
        try:
            dokumente += _jobcloud_seiten(domain, stichwort, ort, je_kanton)
        except Exception as e:     # ein Kanton faellt aus, die anderen bleiben
            fehler = e
    if fehler and not dokumente:
        raise fehler
    if gebiet["art"] == "kantone":
        dokumente = _nur_kantone(dokumente, gebiet["codes"])
    stellen = []
    for d in dokumente:
        grade = d.get("employment_grades") or []
        link = (d.get("_links", {}).get("detail_de")
                or d.get("_links", {}).get("detail_fr")
                or d.get("_links", {}).get("detail_en") or {}).get("href", "")
        k = d.get("coordinates") or {}
        stellen.append({
            "key": f"{portal}:{d['job_id']}", "portal": portal,
            "titel": d.get("title", ""), "firma": d.get("company_name", ""),
            "ort": d.get("place", ""),
            "pensum": _pensum(min(grade), max(grade)) if grade else None,
            "datum": (d.get("publication_date") or "")[:10],
            "link": _sicher(link), "vorschau": (d.get("preview") or "")[:400],
            "punkt": _punkt(k.get("lat"), k.get("lon")),
        })
    return stellen


def _nur_kantone(dokumente, codes):
    """jobs.ch nimmt den Kanton ungenau (bei „Luzern“ auch Zuerich oder Basel).
    Weg faellt nur, was eindeutig woanders liegt: Ortsname genau bekannt, in
    einem anderen Kanton, und kein gewaehlter Kanton im Ortstext genannt."""
    namen = [n.lower() for c in codes for n in [KANTON_NAMEN[c]] + KANTON_ANDERE.get(c, [])]
    erste = lambda d: re.split(r"[,/(]| - ", d.get("place") or "")[0].strip()
    orte = {erste(d) for d in dokumente}
    def kanton(ort):
        try:
            e = ort_suchen(ort)
        except Exception:
            return None
        genau = (e or {}).get("genau") or []
        return {g.get("cantonCode") for g in genau} if genau else None
    with ThreadPoolExecutor(6) as pool:
        kantone = dict(zip(orte, pool.map(kanton, orte)))
    def drin(d):
        k = kantone.get(erste(d))
        if not k or k & set(codes):
            return True
        return any(n in (d.get("place") or "").lower() for n in namen)
    return [d for d in dokumente if drin(d)]


def _jobcloud_text(domain, job_id):
    d = json.loads(_holen(f"https://www.{domain}/api/v1/public/search/job/"
                          + urllib.parse.quote(job_id, safe="")))
    return text_aus_html(d.get("template_text") or d.get("template") or "")


# --- LinkedIn -----------------------------------------------------------------

# LinkedIn nimmt den Ort nur ungefaehr (liefert auch Bern/Zuerich fuer „Luzern“).
# Deshalb werden die Treffer auf den Kanton des Orts gefiltert. Namen wie LinkedIn
# sie schreibt („Emmen, Luzern, Schweiz“).
KANTON_NAMEN = {
    "AG": "Aargau", "AI": "Appenzell Innerrhoden", "AR": "Appenzell Ausserrhoden",
    "BE": "Bern", "BL": "Basel-Landschaft", "BS": "Basel-Stadt", "FR": "Freiburg",
    "GE": "Genf", "GL": "Glarus", "GR": "Graubünden", "JU": "Jura", "LU": "Luzern",
    "NE": "Neuenburg", "NW": "Nidwalden", "OW": "Obwalden", "SG": "Sankt Gallen",
    "SH": "Schaffhausen", "SO": "Solothurn", "SZ": "Schwyz", "TG": "Thurgau",
    "TI": "Tessin", "UR": "Uri", "VD": "Waadt", "VS": "Wallis", "ZG": "Zug", "ZH": "Zürich"}


# Andere Schreibweisen derselben Kantone (fr/it/kurz), fuer den LinkedIn-Filter.
KANTON_ANDERE = {
    "SG": ["St. Gallen"], "GE": ["Genève", "Geneva"], "FR": ["Fribourg"],
    "NE": ["Neuchâtel"], "TI": ["Ticino"], "VD": ["Vaud"], "VS": ["Valais"],
    "AI": ["Appenzell"], "BL": ["Basel-Land"], "GR": ["Grisons", "Graubunden"]}


KANTON_ENGLISCH = {"LU": ["Lucerne"], "ZH": ["Zurich"], "BE": ["Berne"],
                   "GR": ["Graubuenden"], "ZG": ["Zoug"], "SG": ["Saint Gallen"]}


def _kanton_code(ort):
    ort = ort.strip().lower()
    for code, name in KANTON_NAMEN.items():
        namen = [name, code] + KANTON_ANDERE.get(code, []) + KANTON_ENGLISCH.get(code, [])
        if ort in (n.lower() for n in namen):
            return code
    return None


# Wo jobs.ch/jobup.ch mit dem deutschen Namen fast nichts finden (gemessen
# 15.10.2026, Stichwort „Verkauf“: „Sankt Gallen“ 1 Seite, „St. Gallen“ 36).
JOBCLOUD_ORT = {"SG": "St. Gallen", "GE": "Genève", "FR": "Fribourg",
                "NE": "Neuchâtel", "AI": "Appenzell"}


def _linkedin(stichwort, gebiet):
    if gebiet["art"] == "schweiz":
        return _linkedin_roh(stichwort, None, LINKEDIN_SEITEN)
    if gebiet["art"] == "umkreis":
        # LinkedIn kennt keinen genauen Umkreis: Ort abfragen, dann jede Stelle
        # ueber ihren Ortsnamen verorten. Unbekannter Ort = Stelle faellt weg.
        stellen = _linkedin_roh(stichwort, gebiet["ort"], LINKEDIN_SEITEN,
                                distanz=round(gebiet["km"] / 1.6) + 5)
        orte = {s["ort"] for s in stellen}
        with ThreadPoolExecutor(6) as pool:
            punkte = dict(zip(orte, pool.map(_linkedin_punkt, orte)))
        for s in stellen:
            s["punkt"] = punkte.get(s["ort"])
        return stellen
    stellen, fehler = [], None
    # LinkedIn sperrt bei vielen Abfragen: je mehr Kantone, desto weniger Seiten.
    seiten = max(1, LINKEDIN_SEITEN // len(gebiet["codes"]))
    for code in gebiet["codes"]:
        try:
            stellen += _linkedin_kanton(stichwort, code, seiten)
        except Exception as e:
            fehler = e
    if fehler and not stellen:
        raise fehler
    return stellen


def _linkedin_kanton(stichwort, code, seiten=LINKEDIN_SEITEN):
    kanton = KANTON_NAMEN[code]
    namen = [kanton] + KANTON_ANDERE.get(code, [])
    muster = re.compile(r"\b(" + "|".join(re.escape(n.lower()) for n in namen) + r")\b(?! ausserrhoden)")
    # „Kanton Genf“ kennt LinkedIn nicht, „Genève, Schweiz“ schon: der Reihe nach probieren.
    for anfrage in [f"Kanton {kanton}"] + namen:
        stellen = [s for s in _linkedin_roh(stichwort, anfrage) if muster.search(s["ort"].lower())]
        if stellen:
            # Diese Schreibweise funktioniert: die weiteren Seiten dazuholen.
            # Faellt dabei etwas aus, bleiben die Treffer von Seite 1.
            try:
                mehr = _linkedin_roh(stichwort, anfrage, seiten, ab=1) if seiten > 1 else []
            except Exception:
                mehr = []
            bekannt = {s["key"] for s in stellen}
            return stellen + [s for s in mehr if s["key"] not in bekannt
                              and muster.search(s["ort"].lower())]
    return []


def _linkedin_roh(stichwort, ort, seiten=1, ab=0, distanz=None):
    q = {"keywords": stichwort, "location": f"{ort}, Schweiz" if ort else "Schweiz"}
    if distanz:
        q["distance"] = distanz     # Meilen
    karten = []
    for nr in range(ab, seiten):
        try:
            roh = _holen("https://www.linkedin.com/jobs-guest/jobs/api/"
                         "seeMoreJobPostings/search?"
                         + urllib.parse.urlencode({**q, "start": nr * 10}))
        except Exception:
            if nr == ab:
                raise
            break             # spaetere Seite gesperrt: mit dem bisher Gefundenen weiter
        neu = roh.split("<li")[1:]
        if not neu:
            break
        karten += neu
    stellen, gesehen = [], set()
    for karte in karten:
        nr = re.search(r"urn:li:jobPosting:(\d+)", karte)
        titel = re.search(r'base-search-card__title">\s*([^<]+)', karte)
        if not nr or not titel:
            continue
        firma = re.search(r'base-search-card__subtitle">.*?>\s*([^<]+?)\s*<', karte, re.S)
        ort_ = re.search(r'job-search-card__location">\s*([^<]+)', karte)
        datum = re.search(r'datetime="([\d-]+)"', karte)
        if nr.group(1) in gesehen:
            continue
        gesehen.add(nr.group(1))
        stellen.append({
            "key": f"linkedin:{nr.group(1)}", "portal": "linkedin",
            "titel": html.unescape(titel.group(1).strip().splitlines()[0].strip()),
            "firma": html.unescape(firma.group(1).strip()) if firma else "",
            "ort": html.unescape(ort_.group(1).strip()) if ort_ else "",
            "pensum": None, "datum": datum.group(1) if datum else "",
            "link": f"https://www.linkedin.com/jobs/view/{nr.group(1)}/",
            "vorschau": "",
        })
    return stellen


def _linkedin_text(job_id):
    roh = _holen("https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/"
                 + urllib.parse.quote(job_id, safe=""))
    teil = re.search(r'show-more-less-html__markup[^>]*>(.*?)</div>', roh, re.S)
    return text_aus_html(teil.group(1) if teil else roh)


# --- Job-Room (Bund/RAV) ------------------------------------------------------

# --- Orte und Umkreis ---------------------------------------------------------

def _punkt(lat, lon):
    try:
        return (float(lat), float(lon))
    except (TypeError, ValueError):
        return None


def entfernung(a, b):
    """Luftlinie in km (genau genug fuer die Schweiz)."""
    lat = math.radians((a[0] + b[0]) / 2)
    return math.hypot((a[0] - b[0]) * 111.2, (a[1] - b[1]) * 111.2 * math.cos(lat))


_orte = {}
_orte_sperre = threading.Lock()
# Englische Namen, wie LinkedIn sie schreibt.
ORT_ENGLISCH = {"geneva": "Genève", "lucerne": "Luzern", "zurich": "Zürich",
                "berne": "Bern", "basle": "Basel", "fribourg": "Fribourg"}


def _norm(t):
    t = (t or "").lower().strip()
    for a, b in (("ä", "a"), ("ö", "o"), ("ü", "u"), ("é", "e"), ("è", "e"), ("â", "a")):
        t = t.replace(a, b)
    return re.sub(r"\s*\(.*?\)|[^a-z0-9. ]", "", t).strip()


def ort_suchen(name):
    """Ort oder PLZ ueber das Ortsverzeichnis von Job-Room. Liefert
    {"genau": [Treffer mit gleichem Namen], "erster": erster Treffer} — oder None."""
    name = (name or "").strip()[:80]
    schluessel = _norm(name)
    if not schluessel:
        return None
    with _orte_sperre:
        if schluessel in _orte:
            return _orte[schluessel]
    d = json.loads(_holen(
        "https://www.job-room.ch/referenceservice/api/_search/localities?"
        + urllib.parse.urlencode({"prefix": name, "resultSize": 10,
                                  "distinctLocalities": "true"})))
    treffer = [t for t in d.get("localities") or []
               if _punkt(*[(t.get("geoPoint") or {}).get(x) for x in ("lat", "lon")]) and t.get("city")]
    # „Buchs AG“ zaehlt als genauer Treffer fuer „Buchs“.
    genau = [t for t in treffer if re.fullmatch(re.escape(schluessel) + r"( [a-z]{2})?", _norm(t.get("city")))]
    ergebnis = {"genau": genau, "erster": treffer[0] if treffer else None}
    with _orte_sperre:
        if len(_orte) > 2000:
            _orte.clear()
        _orte[schluessel] = ergebnis
    return ergebnis


def _linkedin_punkt(ort):
    """„Emmen, Luzern, Schweiz“ -> Koordinaten. Gleichnamige Orte (Buchs AG/SG)
    werden ueber den Kanton im zweiten Teil unterschieden. Unklar = None."""
    teile = [x.strip() for x in ort.split(",")]
    name = ORT_ENGLISCH.get(_norm(teile[0]), teile[0])
    try:
        e = ort_suchen(name)
    except Exception:
        return None
    kandidaten = (e or {}).get("genau") or []
    kanton = _kanton_code(teile[1]) if len(teile) > 2 else None
    if kanton:
        kandidaten = [k for k in kandidaten if k.get("cantonCode") == kanton]
    if not kandidaten:
        return None
    g = kandidaten[0]["geoPoint"]
    return _punkt(g["lat"], g["lon"])


# Ungefaehre Kantonsmitte und groesste Entfernung von dort bis zur Grenze (km).
# Nur fuer die Frage „kann der Umkreis diesen Kanton beruehren?“ — grosszuegig.
KANTON_MITTE = {
    "AG": (47.41, 8.15, 35), "AI": (47.32, 9.42, 15), "AR": (47.37, 9.30, 25),
    "BE": (46.82, 7.63, 80), "BL": (47.45, 7.70, 30), "BS": (47.56, 7.60, 8),
    "FR": (46.72, 7.08, 40), "GE": (46.20, 6.14, 20), "GL": (46.98, 9.07, 30),
    "GR": (46.66, 9.63, 95), "JU": (47.35, 7.15, 35), "LU": (47.07, 8.11, 40),
    "NE": (47.00, 6.78, 35), "NW": (46.93, 8.39, 18), "OW": (46.85, 8.20, 25),
    "SG": (47.23, 9.27, 55), "SH": (47.71, 8.59, 25), "SO": (47.30, 7.64, 45),
    "SZ": (47.06, 8.75, 35), "TG": (47.57, 9.10, 35), "TI": (46.30, 8.80, 65),
    "UR": (46.77, 8.63, 35), "VD": (46.57, 6.55, 65), "VS": (46.21, 7.61, 90),
    "ZG": (47.16, 8.52, 15), "ZH": (47.42, 8.65, 40)}


def _kantone_im_umkreis(gebiet):
    mitte = (gebiet["lat"], gebiet["lon"])
    return [c for c, (lat, lon, weite) in KANTON_MITTE.items()
            if c == gebiet["kanton"] or entfernung(mitte, (lat, lon)) <= gebiet["km"] + weite]


def _jobroom(stichwort, gebiet):
    umkreis = None
    if gebiet["art"] == "umkreis":
        umkreis = {"geoPoint": {"lat": gebiet["lat"], "lon": gebiet["lon"]}, "distance": gebiet["km"]}
    anfrage = {"keywords": [stichwort],
               "cantonCodes": gebiet["codes"] if gebiet["art"] == "kantone" else [],
               "workloadPercentageMin": 0, "workloadPercentageMax": 100,
               "permanent": None, "companyName": None, "onlineSince": 60,
               "displayRestricted": False, "professionCodes": [],
               "communalCodes": [], "radiusSearchRequest": umkreis}
    daten = []
    for seite in range(MAX_PRO_PORTAL // 100):
        teil = json.loads(_holen(
            "https://www.job-room.ch/jobadservice/api/jobAdvertisements/_search?"
            f"page={seite}&size=100&sort=date_desc&language=de", anfrage))
        daten += teil
        if len(teil) < 100:
            break
    stellen = []
    for eintrag in daten:
        a = eintrag.get("jobAdvertisement", {})
        c = a.get("jobContent", {})
        beschr = (c.get("jobDescriptions") or [{}])[0]
        text = re.sub(r"</?em>", "", beschr.get("description") or "")
        text = re.sub(r"\\([*_#\[\]()-])", r"\1", text)
        titel = html.unescape(re.sub(r"</?em>", "", beschr.get("title") or ""))
        link = c.get("externalUrl") or f"https://www.job-room.ch/job-search/{a.get('id')}"
        if link and not link.startswith("http"):
            link = "https://" + link
        e = c.get("employment") or {}
        link = _sicher(link)
        ort = c.get("location") or {}
        stellen.append({
            "key": f"jobroom:{a.get('id')}", "portal": "jobroom",
            "titel": titel, "firma": (c.get("company") or {}).get("name", ""),
            "ort": ort.get("city", ""),
            "pensum": _pensum(e.get("workloadPercentageMin"), e.get("workloadPercentageMax")),
            "datum": (a.get("createdTime") or "")[:10], "link": link,
            "vorschau": text[:400], "text": text,
            "punkt": _punkt(*[(ort.get("coordinates") or {}).get(x) for x in ("lat", "lon")]),
        })
    return stellen


PORTALE = {
    "jobs.ch": lambda s, o: _jobcloud("jobs.ch", "jobs.ch", s, o),
    "jobup.ch": lambda s, o: _jobcloud("jobup.ch", "jobup.ch", s, o),
    "linkedin": _linkedin,
    "jobroom": _jobroom,
}


def inseratetext(stelle):
    """Voller Inseratetext — wird erst beim Anklicken einer Stelle geholt."""
    if stelle.get("text"):
        return stelle["text"]
    portal, job_id = stelle["key"].split(":", 1)
    if portal in ("jobs.ch", "jobup.ch"):
        return _jobcloud_text(portal, job_id)
    if portal == "linkedin":
        return _linkedin_text(job_id)
    return stelle.get("vorschau", "")


class _Takt:
    """Hoechstens ein Abruf je `abstand` Sekunden — jobs.ch sperrt nach etwa 300
    Abrufen in wenigen Minuten (gemessen 18.10.2026), LinkedIn noch frueher."""
    def __init__(self, abstand):
        self.abstand, self.naechster, self.sperre = abstand, 0.0, threading.Lock()

    def warten(self):
        with self.sperre:
            jetzt = time.time()
            start = max(jetzt, self.naechster)
            self.naechster = start + self.abstand
        time.sleep(max(0.0, start - jetzt))


def texte_holen(stellen, melden=lambda t: None, frist=150):
    """Volle Inseratetexte fuer die Erfahrungs-Pruefung, in Reihenfolge der Liste.
    Gibt {key: text} zurueck; was nicht in der Frist geladen wird, fehlt.
    Je Portal ein eigener Takt; sperrt ein Portal, hoeren seine Abrufe sofort auf."""
    ende = time.time() + frist
    takte = {"jobs.ch": _Takt(0.6), "jobup.ch": _Takt(0.6), "linkedin": _Takt(2.0)}
    gesperrt = {name: threading.Event() for name in takte}

    def holen(s):
        portal = s["key"].split(":", 1)[0]
        if portal not in takte or gesperrt[portal].is_set() or time.time() > ende:
            return s["key"], None
        takte[portal].warten()
        if time.time() > ende or gesperrt[portal].is_set():
            return s["key"], None
        try:
            return s["key"], inseratetext(s)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):        # gesperrt: dieses Portal nicht weiter belasten
                gesperrt[portal].set()
        except Exception:
            pass                            # z. B. geloeschtes Inserat: nur diese Stelle
        return s["key"], None

    fertig = {s["key"]: s["text"] for s in stellen if s.get("text")}
    offen = [s for s in stellen if not s.get("text")]
    melden(f"Prüfe in {len(offen)} Inseraten die verlangte Erfahrung (bis {frist // 60} Min.) …")
    with ThreadPoolExecutor(6) as pool:
        fertig.update((k, v) for k, v in pool.map(holen, offen) if v)
    return fertig


def _schluessel(stelle):
    """Gleiche Stelle auf mehreren Portalen (Job-Room spiegelt z. B. jobs.ch)."""
    norm = lambda t: re.sub(r"[^a-z0-9]", "", (t or "").lower())
    titel = re.sub(r"\(.*?\)|m/w/d|w/m/d|m/w|w/m|\d+\s*-?\s*\d*\s*%", "", stelle["titel"].lower())
    if not norm(stelle["firma"]):
        return stelle["key"]
    ort = norm(re.split(r"[,(]", stelle.get("ort") or "")[0])
    return norm(titel)[:40] + "|" + norm(stelle["firma"])[:20] + "|" + ort[:15]


def suchen(stichworte, gebiet, pensum_von, pensum_bis, melden=lambda t: None):
    """Alle Portale x alle Stichworte. Liefert (stellen, fehler).
    gebiet: {"art": "schweiz"} | {"art": "kantone", "codes": [...]}
          | {"art": "umkreis", "ort", "km", "lat", "lon", "kanton"}"""
    def holen(name, stichwort):
        try:
            return PORTALE[name](stichwort, gebiet), None
        except urllib.error.HTTPError as e:
            grund = ("kurz gesperrt (zu viele Abfragen), in 2 Minuten nochmal versuchen"
                     if e.code in (403, 429) else f"Fehler {e.code}")
        except Exception as e:
            grund = str(e) if isinstance(e, RuntimeError) else type(e).__name__
        return [], f"{name} ({stichwort}): {grund}"

    gesehen, stellen, fehler = set(), [], []
    for stichwort in stichworte:
        melden(f"Suche „{stichwort}“ auf allen Portalen …")
        # Alle Portale gleichzeitig; zusammengefuehrt in fester Reihenfolge, damit
        # bei Doppelten immer dasselbe Portal gewinnt.
        with ThreadPoolExecutor(len(PORTALE)) as pool:
            ergebnisse = list(pool.map(lambda n: holen(n, stichwort), PORTALE))
        for gefunden, fehlertext in ergebnisse:
            if fehlertext:
                fehler.append(fehlertext)
            for s in gefunden:
                if gebiet["art"] == "umkreis":
                    if not s.get("punkt"):
                        continue          # Ort unbekannt: lieber weglassen
                    s["km"] = round(entfernung((gebiet["lat"], gebiet["lon"]), s["punkt"]))
                    if s["km"] > gebiet["km"]:
                        continue
                s.pop("punkt", None)
                p = s.get("pensum")
                if p and (p[1] < pensum_von or p[0] > pensum_bis):
                    continue
                k = _schluessel(s)
                if k in gesehen:
                    continue
                gesehen.add(k)
                s["wort"] = stichwort
                stellen.append(s)
    return stellen, fehler
