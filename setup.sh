#!/bin/sh
set -eu
cd "$(dirname "$0")"
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install --no-deps -e .
npm --prefix frontend ci
npm --prefix frontend run build
echo 'Ready. Run .venv/bin/python start.py'

