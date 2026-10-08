#!/bin/bash
# Full pipeline: build → audio → images → compress → export → sync.
# Equivalent to `flashcards run`; defaults come from settings.toml [run] and
# any flags are passed through (e.g. ./run.sh --no-sync).
#
# Orphan deletion is never forced: if sync refuses because too many notes
# would go, check `./fc.sh sync --dry-run`, then re-run with
# `./run.sh --allow-orphan-delete` if it's intended.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/scripts/_venv.sh"
ensure_venv "$HERE"

# Fail fast on a malformed sources.json before any AI calls are made.
echo "[run] validating sources.json"
if ! discover_out="$(python -m flashcards discover 2>&1)"; then
    echo "$discover_out"
    exit 1
fi

python -m flashcards run "$@"
