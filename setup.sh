#!/usr/bin/env bash
# Narrator Studio - one-time setup for macOS / Linux.  Run:  bash setup.sh
set -e
cd "$(dirname "$0")"

PY=""
for v in 3.11 3.10 3.9; do
  if command -v "python$v" >/dev/null 2>&1; then PY="python$v"; break; fi
done
if [ -z "$PY" ]; then
  echo "Python 3.9-3.11 is required (Coqui TTS does not support 3.12+)."
  echo "  macOS:  brew install python@3.11"
  echo "  Ubuntu: sudo apt install python3.11 python3.11-venv"
  exit 1
fi

missing=""
command -v ffmpeg >/dev/null 2>&1 || missing="$missing ffmpeg"
command -v espeak-ng >/dev/null 2>&1 || command -v espeak >/dev/null 2>&1 || missing="$missing espeak-ng"
if [ -n "$missing" ]; then
  echo "Missing:$missing"
  echo "  macOS:  brew install$missing"
  echo "  Ubuntu: sudo apt install$missing"
  exit 1
fi

[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
echo
echo "Setup complete. Start the app with:  .venv/bin/python app.py"
