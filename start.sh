#!/bin/bash
# Startet den Stellen-Finder: http://localhost:8813
cd "$(dirname "$0")"
# Neu installieren, sobald sich requirements.txt aendert (z. B. nach git pull).
if ! cmp -s requirements.txt venv/.installiert 2>/dev/null; then
  [ -d venv ] || python3 -m venv venv
  ./venv/bin/pip install -q -r requirements.txt && cp requirements.txt venv/.installiert
fi
exec ./venv/bin/python server.py
