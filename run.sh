#!/bin/bash
# The one command for this project.
#
#   ./run.sh                 do everything: lists → cards → Anki → Claude → audio → Anki
#                            (safe to run any time; it only does what's left)
#   ./run.sh --images 20     same, making up to 20 Codex images this run
#   ./run.sh help            the occasional extras (practice, movie report, share, …)
#   ./run.sh <extra> …       run one of them, e.g. ./run.sh practice
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/scripts/_venv.sh"
ensure_venv "$HERE" >&2

if [ $# -eq 0 ] || [[ "$1" == -* ]]; then
    exec python -m flashcards run "$@"
fi
if [ "$1" = "help" ]; then
    exec python -m flashcards --help
fi
exec python -m flashcards "$@"
