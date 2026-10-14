#!/bin/bash
# Startet den Stellen-Finder: http://localhost:8813
cd "$(dirname "$0")"
if [ ! -f venv/.installiert ]; then
  python3 -m venv venv
  ./venv/bin/pip install -q -r requirements.txt && touch venv/.installiert
fi
exec ./venv/bin/python server.py
