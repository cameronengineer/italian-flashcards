#!/bin/bash
# Generate missing audio for specific decks.
#
# Usage:
#   ./manual_audio_generate.sh              # generate all missing audio for listed decks
#   ./manual_audio_generate.sh --limit 200  # cap this run at 200 files
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

# shellcheck disable=SC1091
source "$HERE/scripts/_venv.sh"
ensure_venv "$HERE"

python -m flashcards audio \
    --workers 5 \
    --deck 'Italian - CILS A1' \
    --deck 'Italian - CILS A2' \
    --deck 'Italian - CILS B1' \
    --deck 'Italian - CILS B2' \
    --deck 'Italian - Espressioni con Avere' \
    --deck 'Italian - Italki' \
    --deck 'Italian - Italki Verbs Imperativo' \
    --deck 'Italian - Italki Verbs Imperfetto' \
    --deck 'Italian - Italki Verbs Infinitive' \
    --deck 'Italian - Italki Verbs Passato Prossimo' \
    --deck 'Italian - Italki Verbs Presente' \
    "$@"
