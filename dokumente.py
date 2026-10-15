# -*- coding: utf-8 -*-
"""dokumente.py — hochgeladenen Lebenslauf lesen (Word/PDF) und Ergebnisse als Word schreiben."""

import re
import zipfile
from pathlib import Path

from docx import Document
from docx.shared import Pt, RGBColor


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


def _fett(absatz, text):
    for i, stueck in enumerate(re.split(r"\*\*(.+?)\*\*", text)):
        if stueck:
            absatz.add_run(stueck).bold = (i % 2 == 1)


def lebenslauf_docx(markdown, ziel):
    doc = Document()
    stil = doc.styles["Normal"]
    stil.font.name, stil.font.size = "Calibri", Pt(10.5)
    for zeile in markdown.splitlines():
        z = zeile.rstrip()
        if not z.strip():
            continue
        if z.startswith("# "):
            r = doc.add_paragraph().add_run(z[2:].strip())
            r.bold, r.font.size = True, Pt(20)
        elif z.startswith("## "):
            a = doc.add_paragraph()
            a.paragraph_format.space_before = Pt(10)
            r = a.add_run(z[3:].strip().upper())
            r.bold, r.font.size = True, Pt(11)
            r.font.color.rgb = RGBColor(0x1F, 0x4E, 0x8C)
        elif re.match(r"^\s*[-•*] ", z):
            _fett(doc.add_paragraph(style="List Bullet"), re.sub(r"^\s*[-•*] ", "", z))
        else:
            _fett(doc.add_paragraph(), z.lstrip("# "))
    doc.save(ziel)


def anschreiben_docx(text, ziel):
    doc = Document()
    stil = doc.styles["Normal"]
    stil.font.name, stil.font.size = "Calibri", Pt(11)
    for block in re.split(r"\n\s*\n", text.strip()):
        a = doc.add_paragraph()
        zeilen = block.splitlines()
        for i, z in enumerate(zeilen):
            _fett(a, z.strip())
            if i < len(zeilen) - 1:
                a.add_run().add_break()
    doc.save(ziel)
