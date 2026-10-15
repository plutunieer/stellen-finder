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
import re
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130 Safari/537.36")
ZEITLIMIT = 25
PRO_ABFRAGE = 20


def _holen(url, daten=None):
    kopf = {"User-Agent": UA, "Accept-Language": "de-CH,de;q=0.9"}
    if daten is not None:
        kopf["Content-Type"] = "application/json"
        daten = json.dumps(daten).encode()
    anfrage = urllib.request.Request(url, data=daten, headers=kopf)
    with urllib.request.urlopen(anfrage, timeout=ZEITLIMIT) as antwort:
        return antwort.read().decode("utf-8", "replace")


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

def _jobcloud(portal, domain, stichwort, ort):
    q = {"query": stichwort, "rows": PRO_ABFRAGE}
    if ort:
        q["location"] = _jobcloud_ort(ort)
    daten = json.loads(_holen(f"https://www.{domain}/api/v1/public/search?"
                              + urllib.parse.urlencode(q)))
    stellen = []
    for d in daten.get("documents", []):
        grade = d.get("employment_grades") or []
        link = (d.get("_links", {}).get("detail_de")
                or d.get("_links", {}).get("detail_fr")
                or d.get("_links", {}).get("detail_en") or {}).get("href", "")
        stellen.append({
            "key": f"{portal}:{d['job_id']}", "portal": portal,
            "titel": d.get("title", ""), "firma": d.get("company_name", ""),
            "ort": d.get("place", ""),
            "pensum": _pensum(min(grade), max(grade)) if grade else None,
            "datum": (d.get("publication_date") or "")[:10],
            "link": _sicher(link), "vorschau": (d.get("preview") or "")[:400],
        })
    return stellen


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


def _kanton_code(ort):
    for code, name in KANTON_NAMEN.items():
        if ort.strip().lower() in (name.lower(), code.lower()):
            return code
    return None


# Wo jobs.ch/jobup.ch mit dem deutschen Namen fast nichts finden (gemessen
# 15.10.2026, Stichwort „Verkauf“: „Sankt Gallen“ 1 Seite, „St. Gallen“ 36).
JOBCLOUD_ORT = {"SG": "St. Gallen", "GE": "Genève", "FR": "Fribourg",
                "NE": "Neuchâtel", "AI": "Appenzell"}


def _jobcloud_ort(ort):
    return JOBCLOUD_ORT.get(_kanton_code(ort or ""), ort)


def _linkedin(stichwort, ort):
    try:
        kanton = KANTON_NAMEN.get(_kanton(ort)) if ort else None
    except Exception:
        kanton = None     # Ort bei Job-Room unbekannt: LinkedIn ungefiltert
    if not kanton:
        return _linkedin_roh(stichwort, ort)
    code = _kanton_code(kanton)
    namen = [kanton] + KANTON_ANDERE.get(code, [])
    muster = re.compile(r"\b(" + "|".join(re.escape(n.lower()) for n in namen) + r")\b(?! ausserrhoden)")
    # „Kanton Genf“ kennt LinkedIn nicht, „Genève, Schweiz“ schon: der Reihe nach probieren.
    for anfrage in [f"Kanton {kanton}"] + namen:
        stellen = [s for s in _linkedin_roh(stichwort, anfrage) if muster.search(s["ort"].lower())]
        if stellen:
            return stellen
    return []


def _linkedin_roh(stichwort, ort):
    q = {"keywords": stichwort, "location": f"{ort}, Schweiz" if ort else "Schweiz",
         "start": 0}
    roh = _holen("https://www.linkedin.com/jobs-guest/jobs/api/"
                 "seeMoreJobPostings/search?" + urllib.parse.urlencode(q))
    stellen = []
    for karte in roh.split("<li")[1:]:
        nr = re.search(r"urn:li:jobPosting:(\d+)", karte)
        titel = re.search(r'base-search-card__title">\s*([^<]+)', karte)
        if not nr or not titel:
            continue
        firma = re.search(r'base-search-card__subtitle">.*?>\s*([^<]+?)\s*<', karte, re.S)
        ort_ = re.search(r'job-search-card__location">\s*([^<]+)', karte)
        datum = re.search(r'datetime="([\d-]+)"', karte)
        stellen.append({
            "key": f"linkedin:{nr.group(1)}", "portal": "linkedin",
            "titel": html.unescape(titel.group(1).strip().splitlines()[0].strip()),
            "firma": html.unescape(firma.group(1).strip()) if firma else "",
            "ort": html.unescape(ort_.group(1).strip()) if ort_ else "",
            "pensum": None, "datum": datum.group(1) if datum else "",
            "link": f"https://www.linkedin.com/jobs/view/{nr.group(1)}/",
            "vorschau": "",
        })
    return stellen[:PRO_ABFRAGE]


def _linkedin_text(job_id):
    roh = _holen("https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/"
                 + urllib.parse.quote(job_id, safe=""))
    teil = re.search(r'show-more-less-html__markup[^>]*>(.*?)</div>', roh, re.S)
    return text_aus_html(teil.group(1) if teil else roh)


# --- Job-Room (Bund/RAV) ------------------------------------------------------

_kantone = {}


def _kanton(ort):
    if not ort:
        return None
    code = _kanton_code(ort)                  # Auswahl auf der Seite = Kantonsname
    if code:
        return code
    if ort not in _kantone:
        d = json.loads(_holen(
            "https://www.job-room.ch/referenceservice/api/_search/localities?"
            + urllib.parse.urlencode({"prefix": ort, "resultSize": 1,
                                      "distinctLocalities": "true"})))
        treffer = d.get("localities") or []
        if not treffer:
            # Lieber auslassen als still die ganze Schweiz liefern.
            raise RuntimeError(f"Ort „{ort}“ bei Job-Room unbekannt")
        _kantone[ort] = treffer[0]["cantonCode"]
    return _kantone[ort]


def _jobroom(stichwort, ort):
    kanton = _kanton(ort)
    anfrage = {"keywords": [stichwort], "cantonCodes": [kanton] if kanton else [],
               "workloadPercentageMin": 0, "workloadPercentageMax": 100,
               "permanent": None, "companyName": None, "onlineSince": 60,
               "displayRestricted": False, "professionCodes": [],
               "communalCodes": [], "radiusSearchRequest": None}
    daten = json.loads(_holen(
        "https://www.job-room.ch/jobadservice/api/jobAdvertisements/_search?"
        f"page=0&size={PRO_ABFRAGE}&sort=date_desc&language=de", anfrage))
    stellen = []
    for eintrag in daten:
        a = eintrag.get("jobAdvertisement", {})
        c = a.get("jobContent", {})
        beschr = (c.get("jobDescriptions") or [{}])[0]
        text = re.sub(r"</?em>", "", beschr.get("description") or "")
        text = re.sub(r"\\([*_#\[\]()-])", r"\1", text)
        titel = re.sub(r"</?em>", "", beschr.get("title") or "")
        link = c.get("externalUrl") or f"https://www.job-room.ch/job-search/{a.get('id')}"
        if link and not link.startswith("http"):
            link = "https://" + link
        e = c.get("employment") or {}
        link = _sicher(link)
        stellen.append({
            "key": f"jobroom:{a.get('id')}", "portal": "jobroom",
            "titel": titel, "firma": (c.get("company") or {}).get("name", ""),
            "ort": (c.get("location") or {}).get("city", ""),
            "pensum": _pensum(e.get("workloadPercentageMin"), e.get("workloadPercentageMax")),
            "datum": (a.get("createdTime") or "")[:10], "link": link,
            "vorschau": text[:400], "text": text,
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


def _schluessel(stelle):
    """Gleiche Stelle auf mehreren Portalen (Job-Room spiegelt z. B. jobs.ch)."""
    norm = lambda t: re.sub(r"[^a-z0-9]", "", (t or "").lower())
    titel = re.sub(r"\(.*?\)|m/w/d|w/m/d|m/w|w/m|\d+\s*-?\s*\d*\s*%", "", stelle["titel"].lower())
    if not norm(stelle["firma"]):
        return stelle["key"]
    ort = norm(re.split(r"[,(]", stelle.get("ort") or "")[0])
    return norm(titel)[:40] + "|" + norm(stelle["firma"])[:20] + "|" + ort[:15]


def suchen(stichworte, ort, pensum_von, pensum_bis, melden=lambda t: None):
    """Alle Portale x alle Stichworte. Liefert (stellen, fehler)."""
    gesehen, stellen, fehler = set(), [], []
    for stichwort in stichworte:
        for name, abfrage in PORTALE.items():
            melden(f"Suche „{stichwort}“ auf {name} …")
            try:
                gefunden = abfrage(stichwort, ort)
            except Exception as e:
                grund = str(e) if isinstance(e, RuntimeError) else type(e).__name__
                fehler.append(f"{name} ({stichwort}): {grund}")
                continue
            for s in gefunden:
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
