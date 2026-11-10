#!/usr/bin/env bash
# Trascrittore .brAIan - avvio su macOS / Linux
set -e
cd "$(dirname "$0")"
if [ ! -x venv/bin/python ]; then
  echo "Prima volta: preparo l'ambiente Python, ci vuole qualche minuto..."
  python3 -m venv venv
  venv/bin/python -m pip install --upgrade pip
  venv/bin/python -m pip install -r requirements.txt
fi
exec venv/bin/python trascrittore.py
