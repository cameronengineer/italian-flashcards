#!/bin/bash
set -e

source .venv/bin/activate

python scripts/93_learnt_words.py "$@"
