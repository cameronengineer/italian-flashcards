#!/bin/bash
# Run any flashcards command inside the project venv, e.g.
#   ./fc.sh practice --style reflexive
#   ./fc.sh sync --dry-run
#   ./fc.sh --help
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/scripts/_venv.sh"
ensure_venv "$HERE" >&2

exec python -m flashcards "$@"
