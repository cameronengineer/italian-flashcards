#!/bin/bash
# Shared venv bootstrap, sourced by run.sh.
#
#   source "$HERE/scripts/_venv.sh"
#   ensure_venv "$HERE"
#
# Creates $1/.venv if missing, installs the project (pyproject.toml) into it
# in editable mode, and activates it. Re-installs only when pyproject.toml
# changes.
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
