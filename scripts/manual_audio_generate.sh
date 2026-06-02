#!/bin/bash
# Generate missing audio for specific decks.
#
# Usage:
#   ./manual_audio_generate.sh              # generate all missing audio for listed decks
#   ./manual_audio_generate.sh --limit 200  # cap this run at 200 files
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

# shellcheck disable=SC1091
source "$HERE/_venv.sh"
ensure_venv "$ROOT"

# Generate ALL missing audio for the core decks. Args from the command line
# (e.g. --limit N) are forwarded to this call only.
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
    --deck 'Italian - Italki Verbs Presente Progressivo' \
    --deck 'Italian - Oral Exam Prep' \
    "$@"

# Cap each Condizionale deck at 300 files per run. The audio command sorts
# texts alphabetically and skips ones already on disk, so this picks up the
# next batch of 300 alphabetically each time it's run until both decks are
# fully covered.
python -m flashcards audio \
    --workers 5 \
    --limit 300 \
    --deck 'Italian - Italki Verbs Condizionale Presente'

python -m flashcards audio \
    --workers 5 \
    --limit 300 \
    --deck 'Italian - Italki Verbs Condizionale Passato'
