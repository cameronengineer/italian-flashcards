#!/bin/bash
# Full pipeline: lexicon → AI queue (Claude + Codex) → notes → Anki → audio.
# Equivalent to `flashcards run`; defaults come from settings.toml [run] and
# any flags are passed through (e.g. ./run.sh --no-sync).
#
# Retiring old notes is never forced: if apply refuses because many
# unstudied notes would go, check `./fc.sh plan`, then re-run with
# `./run.sh --allow-retire` if it's intended. Studied notes are never deleted.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/scripts/_venv.sh"
ensure_venv "$HERE"

# Fail fast on a malformed lists.toml / plan.toml before any AI calls are made.
echo "[run] validating lists.toml + plan.toml"
if ! check_out="$(python -m flashcards check 2>&1)"; then
    echo "$check_out"
    exit 1
fi

python -m flashcards run "$@"
