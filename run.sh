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
# Create and activate the environment; reinstall only when dependencies change.
ensure_venv() {
    local root="${1:?ensure_venv: project root required}"
    local venv="$root/.venv"
    local project="$root/pyproject.toml"
    local stamp="$venv/.pyproject.sha"

    if [ ! -d "$venv" ]; then
        echo "[venv] creating $venv"
        python3 -m venv "$venv"
    fi

    # shellcheck disable=SC1091
    source "$venv/bin/activate"

    local current previous=""
    current="$(shasum -a 256 "$project" | awk '{print $1}')"
    [ -f "$stamp" ] && previous="$(cat "$stamp")"
    if [ "$current" != "$previous" ]; then
        echo "[venv] installing project (pyproject.toml changed since last run)"
        pip install --quiet --upgrade pip
        pip install --quiet -e "$root"
        echo "$current" > "$stamp"
    fi
}

ensure_venv "$HERE" >&2

if [ $# -eq 0 ] || [[ "$1" == -* ]]; then
    exec python -m flashcards run "$@"
fi
if [ "$1" = "help" ]; then
    exec python -m flashcards --help
fi
exec python -m flashcards "$@"
