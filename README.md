# Stellen-Finder

Findet passende Stellen in der Schweiz und erstellt auf Klick den angepassten Lebenslauf und das Anschreiben.

**Starten:** `./start.sh` → http://localhost:8813

## Bedienung
1. Lebenslauf hochladen (Word oder PDF). Die Seite merkt sich genau diese eine Datei, bis du eine neue hochlädst.
2. Stichworte, Erfahrung, Region und Pensum wählen. Ohne Stichworte leitet Claude die Suchbegriffe aus dem Lebenslauf ab.
   - **Erfahrung** „Einsteiger“ oder „Junior“: Stellen mit Senior, Leiter, Head usw. im Titel werden ausgeblendet, Einsteiger-Stellen kommen nach oben.
   - **Region**: ganze Schweiz, mehrere Kantone oder Umkreis (10–50 km) um einen Ort oder eine PLZ.
   - **Ausschliessen**: Wörter wie „Aussendienst, Provision“ blenden Stellen aus. Ausgeblendetes lässt sich mit „Trotzdem zeigen“ ansehen.
3. **Stellen suchen** (ca. 2 Min.): Die Seite zeigt die Stellen ab 50 Punkten, die beste steht oben.
4. **Bewerbung erstellen** (ca. 1–3 Min.): Lebenslauf und Anschreiben erscheinen auf der Seite und lassen sich als Word-Datei herunterladen.

Es wird nichts verschickt.

## Portale
jobs.ch, jobup.ch, LinkedIn und Job-Room (das Stellenportal des Bundes/RAV, es enthält auch Inserate vieler anderer Portale). **Indeed fehlt**, weil es automatische Abfragen sperrt.

## Dateien
- `server.py`: die Webseite und der Ablauf (Suche, Bewertung, Bewerbung)
- `portale.py`: holt die Stellen und die Inseratetexte von den Portalen
- `ki.py`: alle KI-Aufrufe über die lokale Claude-CLI (Abo, kein API-Schlüssel, ohne Werkzeuge)
- `dokumente.py`: liest den Lebenslauf (Word/PDF) und schreibt die Word-Dateien
- `index.html`: die Oberfläche
- `daten/`: hochgeladener Lebenslauf, letzte Suche und erstellte Bewerbungen (persönliche Daten, nicht in Git)
