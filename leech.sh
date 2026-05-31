#!/bin/bash
set -e

source .venv/bin/activate

python scripts/92_leech_cards.py "$@" --threshold 5
