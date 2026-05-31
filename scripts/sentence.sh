#!/bin/bash
# Generate short Italian sentences for active recall, written to system temp dir.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
source "$HERE/scripts/_venv.sh"
ensure_venv "$HERE"

python "$HERE/scripts/03_sentence_practice.py" "$@"
