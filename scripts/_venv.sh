#!/bin/bash
# Shared venv bootstrap. Sourced by run.sh and scripts/*.sh.
#
# Usage:
#   source "$HERE/scripts/_venv.sh"
#   ensure_venv "$HERE"
#
# ensure_venv creates a fresh .venv at $1/.venv if missing, installs
# requirements.txt into it, and activates it. Idempotent: if .venv exists and
# requirements.txt hasn't changed since last install, it just activates.

ensure_venv() {
    local root="${1:?ensure_venv: project root required}"
    local venv="$root/.venv"
    local reqs="$root/requirements.txt"
    local stamp="$venv/.requirements.sha"

    if [ ! -d "$venv" ]; then
        echo "[venv] creating $venv"
        python3 -m venv "$venv"
    fi

    # shellcheck disable=SC1091
    source "$venv/bin/activate"

    if [ -f "$reqs" ]; then
        local current
        current="$(shasum -a 256 "$reqs" | awk '{print $1}')"
        local previous=""
        [ -f "$stamp" ] && previous="$(cat "$stamp")"
        if [ "$current" != "$previous" ]; then
            echo "[venv] installing requirements (changed since last run)"
            pip install --quiet --upgrade pip
            pip install --quiet -r "$reqs"
            echo "$current" > "$stamp"
        fi
    fi
}
