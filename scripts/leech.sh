#!/bin/bash
# Suspend Anki cards that have failed too many times in a row.
# Default threshold lives in scripts/01_leech_cards.py; override with --threshold N.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
source "$HERE/scripts/_venv.sh"
ensure_venv "$HERE"

python "$HERE/scripts/01_leech_cards.py" "$@"
