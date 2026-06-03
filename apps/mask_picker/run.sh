#!/usr/bin/env bash
# =========================================================================
#  Mask Picker — запуск на macOS / Linux.
#  Використання: ./run.sh  (або bash run.sh)
# =========================================================================

set -e
cd "$(dirname "$0")"

if ! command -v python3 &>/dev/null; then
  echo "[ERROR] python3 не знайдено. Встанови Python 3.10+"
  exit 1
fi

# -- швидка перевірка залежностей --
if ! python3 -c "import flask, yaml" 2>/dev/null; then
  echo "[i] Ставлю Flask і PyYAML..."
  python3 -m pip install --user -r requirements.txt
fi

python3 app.py "$@"
