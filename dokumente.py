# -*- coding: utf-8 -*-
"""dokumente.py — hochgeladenen Lebenslauf lesen (Word/PDF) und Ergebnisse als PDF schreiben."""

import io
import os
import re
import zipfile
from pathlib import Path

MIN_FOTO = 5_000          # kleinere Bilder sind Symbole oder Logos, kein Foto
MAX_BILD_BYTES = 15_000_000
MAX_PIXEL = 25_000_000    # Schutz gegen praeparierte Riesenbilder
FOTO_PX = 800             # das Foto wird hoechstens so gross neu gespeichert


def _bildart(daten):
    if daten[:3] == b"\xff\xd8\xff":
        return "jpg"
    if daten[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    return None


def _sauber(daten):
    """Bild pruefen, verkleinern und neu als JPEG speichern — ohne EXIF-Angaben
    (z. B. GPS-Ort vom Handyfoto). None, wenn es kein brauchbares Bild ist."""
    try:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = MAX_PIXEL
        with Image.open(io.BytesIO(daten)) as bild:
            if bild.width * bild.height > MAX_PIXEL:
                return None
            # Ein Passfoto ist etwa so hoch wie breit; Banner und Hintergruende nicht.
            if not 0.5 <= bild.width / max(1, bild.height) <= 1.6 or min(bild.size) < 80:
                return None
            bild.thumbnail((FOTO_PX, FOTO_PX))
            if bild.mode in ("RGBA", "LA", "P"):
                bild = bild.convert("RGBA")
                weiss = Image.new("RGB", bild.size, "white")
                weiss.paste(bild, mask=bild.split()[-1])
                bild = weiss
            puffer = io.BytesIO()
            bild.convert("RGB").save(puffer, "JPEG", quality=88)
            return puffer.getvalue()
    except Exception as e:
        print("Foto unbrauchbar:", repr(e))
        return None


def foto(pfad):
    """Das Foto aus dem hochgeladenen Lebenslauf als JPEG-bytes oder None.
    Genommen wird das groesste Bild (Logos und Symbole sind kleiner)."""
    p = Path(pfad)
    kandidaten = []
    try:
        if p.suffix.lower() == ".pdf":
            from pypdf import PdfReader
            for seite in PdfReader(p).pages[:2]:
                for bild in seite.images:
                    try:                               # ein kaputtes Bild kostet nur sich
                        daten = bild.data
                        if len(daten) > MAX_BILD_BYTES:
                            continue
                        if not _bildart(daten):        # rohe PDF-Bilddaten
                            roh = bild.image
                            if roh.width * roh.height > MAX_PIXEL:
                                continue
                            puffer = io.BytesIO()
                            roh.convert("RGB").save(puffer, "JPEG", quality=90)
                            daten = puffer.getvalue()
                        kandidaten.append(daten)
                    except Exception as e:
                        print("PDF-Bild übersprungen:", repr(e))
        else:
            with zipfile.ZipFile(p) as z:
                kandidaten = [z.read(i) for i in z.infolist()
                              if i.filename.startswith("word/media/")
                              and MIN_FOTO <= i.file_size <= MAX_BILD_BYTES]
    except Exception as e:
        print("Foto nicht lesbar:", repr(e))
        return None
    kandidaten = sorted((k for k in kandidaten if _bildart(k) and len(k) >= MIN_FOTO),
                        key=len, reverse=True)
    for k in kandidaten[:3]:
        sauber = _sauber(k)
        if sauber:
            return sauber
    return None


def lebenslauf_text(pfad):
    p = Path(pfad)
    if p.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        return "\n".join((s.extract_text() or "") for s in PdfReader(p).pages).strip()
    # Direkt aus dem XML: python-docx scheitert an manchen Word-Dateien
    # (z. B. Bilder ohne Dateityp).
    xml = zipfile.ZipFile(p).read("word/document.xml").decode("utf-8")
    absaetze = []
    for absatz in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S):
        teile = re.findall(r"<w:t[^>]*>([^<]*)</w:t>|(<w:tab/>)", absatz)
        text = "".join(t or "\t" for t, _ in teile)
        if text.strip():
            absaetze.append(text)
    import html
    return html.unescape("\n".join(absaetze))


# Schrift mit Umlauten und Sonderzeichen; ohne sie bleibt Helvetica (nur Latin-1).
SCHRIFTEN = [("/System/Library/Fonts/Supplemental/Arial.ttf",
              "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
             ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
             ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")]
BLAU = (0x1F, 0x4E, 0x8C)
MM_BILD = 35              # Fotobreite im Lebenslauf


def _pdf():
    from fpdf import FPDF
    pdf = FPDF(format="A4")
    pdf.set_margins(20, 18, 20)
    pdf.set_auto_page_break(True, 18)
    for normal, fett in SCHRIFTEN:
        if Path(normal).exists() and Path(fett).exists():
            pdf.add_font("Text", "", normal)
            pdf.add_font("Text", "B", fett)
            pdf.schrift = "Text"
            break
    else:
        pdf.schrift = "Helvetica"
    pdf.add_page()
    return pdf


def _zeichen(pdf, text):
    """Helvetica kennt nur Latin-1: typografische Zeichen ersetzen statt abbrechen."""
    if pdf.schrift != "Helvetica":
        return text
    for alt, neu in {"–": "-", "—": "-", "“": '"', "”": '"', "„": '"', "‘": "'",
                     "’": "'", "•": "-", "…": "...", "€": "EUR"}.items():
        text = text.replace(alt, neu)
    return text.encode("latin-1", "replace").decode("latin-1")


def _markdown(text):
    """fpdf versteht nur **fett**; __, -- und ~~ wuerden sonst als Kursiv,
    Unterstrich oder Durchstrich gelesen."""
    return re.sub(r"([-_~])(?=\1)", r"\1 ", text)


def _absatz(pdf, text, groesse, fett=False, farbe=(0, 0, 0), breite=0, zeile=1.35):
    pdf.set_font(pdf.schrift, "B" if fett else "", groesse)
    pdf.set_text_color(*farbe)
    pdf.multi_cell(breite, groesse * 0.3528 * zeile, _markdown(_zeichen(pdf, text)),
                   markdown=True, align="L", new_x="LMARGIN", new_y="NEXT")


def _speichern(pdf, ziel):
    """Erst Zwischendatei, dann ersetzen: nie eine halb geschriebene PDF."""
    tmp = Path(ziel).with_suffix(".tmp")
    pdf.output(tmp)
    os.replace(tmp, ziel)


def lebenslauf_pdf(markdown, ziel, bild=None):
    """bild: bytes des Fotos (jpg/png) oder None. Mit Foto stehen Name und
    Berufstitel links, das Foto rechts daneben."""
    pdf = _pdf()
    zeilen = [z.rstrip() for z in markdown.splitlines() if z.strip()]
    if bild:
        oben = pdf.get_y()
        pdf.image(io.BytesIO(bild), x=pdf.w - pdf.r_margin - MM_BILD, y=oben, w=MM_BILD)
        from PIL import Image                          # Bildhoehe selbst rechnen
        with Image.open(io.BytesIO(bild)) as b:
            bild_unten = oben + MM_BILD * b.height / b.width
        links = pdf.epw - MM_BILD - 5
        if zeilen and zeilen[0].startswith("# "):
            _absatz(pdf, zeilen[0][2:].strip(), 20, fett=True, breite=links)
            zeilen = zeilen[1:]
            if zeilen and not zeilen[0].startswith("#") and not re.match(r"^\s*[-•*] ", zeilen[0]):
                _absatz(pdf, zeilen[0], 10.5, breite=links)
                zeilen = zeilen[1:]
        pdf.set_y(max(pdf.get_y(), bild_unten) + 2)
    for z in zeilen:
        if z.startswith("# "):
            _absatz(pdf, z[2:].strip(), 20, fett=True)
        elif z.startswith("## "):
            pdf.ln(3)
            _absatz(pdf, z[3:].strip().upper(), 11, fett=True, farbe=BLAU)
            pdf.ln(1)
        elif re.match(r"^\s*[-•*] ", z):
            pdf.set_font(pdf.schrift, "", 10.5)
            if pdf.get_y() + 10.5 * 0.3528 * 1.35 > pdf.page_break_trigger:
                pdf.add_page()                         # Punkt nie allein am Seitenende
            pdf.set_text_color(0, 0, 0)
            pdf.cell(5, 10.5 * 0.3528 * 1.35, _zeichen(pdf, "•"))
            _absatz(pdf, re.sub(r"^\s*[-•*] ", "", z), 10.5, breite=pdf.epw - 5)
        else:
            _absatz(pdf, z.lstrip("# "), 10.5)
    _speichern(pdf, ziel)


def anschreiben_pdf(text, ziel):
    """Immer genau eine Seite: ist der Text zu lang, wird die Schrift kleiner."""
    bloecke = re.split(r"\n\s*\n", text.strip())
    for groesse in (11, 10.5, 10, 9.5, 9, 8.5, 8):
        pdf = _pdf()
        for i, block in enumerate(bloecke):
            if i:
                pdf.ln(groesse * 0.3528 * 0.9)
            _absatz(pdf, "\n".join(z.strip() for z in block.splitlines()), groesse)
        if pdf.page_no() == 1:
            break
    else:
        raise RuntimeError("Das Anschreiben ist zu lang für eine Seite. Bitte kürzen.")
    _speichern(pdf, ziel)
