#!/usr/bin/env sh
# sERP launcher for macOS / Linux. First start creates .venv and installs requirements.
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "Preparing sERP for the first start (about a minute)..."
  python3 -m venv .venv && .venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt \
    || { echo "Could not set up Python packages (needs Python 3.10+ and internet)."; rm -rf .venv; exit 1; }
fi
echo "Starting sERP... press Ctrl+C to stop."
exec .venv/bin/python serp_server.py "$@"
