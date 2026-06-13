#!/usr/bin/env bash
# Bootstrap + run DropHound: venv, deps, seed, serve. Idempotent.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

if [ ! -d .venv ]; then
  echo "→ creating virtualenv"
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "→ installing dependencies"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

echo "→ seeding demo data + running one engine cycle"
python -m drophound demo

echo "→ starting web app on http://localhost:8000  (Ctrl-C to stop)"
exec python -m drophound serve --host 0.0.0.0 --port 8000
