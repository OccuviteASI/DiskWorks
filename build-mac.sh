#!/bin/sh
# One-shot macOS build: venv + Python deps + PyInstaller .app   (see MACOS.md)
# Usage: ./build-mac.sh [--console]
set -e
cd "$(dirname "$0")"

if ! xcode-select -p >/dev/null 2>&1; then
  echo "The Xcode Command Line Tools are missing. Run:  xcode-select --install   then re-run this script."
  exit 1
fi

PY="${PYTHON:-python3}"
if [ ! -x .venv/bin/python ]; then
  echo "Creating .venv with $PY ..."
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt

python build.py "$@"
