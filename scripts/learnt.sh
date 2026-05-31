#!/bin/bash
# Export the list of learnt Italian words from Anki to the system temp dir.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
source "$HERE/scripts/_venv.sh"
ensure_venv "$HERE"

python "$HERE/scripts/02_learnt_words.py" "$@"
