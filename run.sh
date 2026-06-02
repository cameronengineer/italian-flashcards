#!/bin/bash
# Full pipeline. Equivalent to `python -m flashcards run`.
#
# To add a new source: append an entry to sources.json (path is relative to
# inputs/) and re-run this script. No code changes needed.
#
# This script bootstraps .venv on first run, validates sources.json, then
# delegates to `python -m flashcards run`.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

# shellcheck disable=SC1091
source "$HERE/scripts/_venv.sh"
ensure_venv "$HERE"

# Gate the pipeline on sources.json being well-formed. `discover` exits non-zero
# (and prints what's wrong) if the manifest is missing, malformed, or fails
# validation — that catches typos before any AI calls are made.
echo "[run] validating sources.json"
python -m flashcards discover > /dev/null

# Per-phase concurrency. Pass --build-workers / --audio-workers / etc. to
# override individual phases, or --workers N to override all at once.
# Audio is capped at 5 to match ElevenLabs' concurrent-request limit.
# Per-phase limits cap how many new media files each stage generates per run.
python -m flashcards run \
    --build-workers 200 \
    --audio-workers 5 \
    --image-workers 200 \
    --compress-workers 8 \
    --audio-limit 1 \
    --image-limit 300 \
    --allow-orphan-delete \
    "$@"
