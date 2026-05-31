#!/bin/bash
# Run the AI card auditor (scripts/04_audit_cards.py).
#
# Bootstraps .venv, then sends every generated card to the AI model for review
# and writes a timestamped audit_report_*.csv to the project root.
#
# Defaults: 100 cards per run, 200 concurrent workers. Override by passing
# --limit / --workers (later flags win over these defaults).
#
# Usage:
#   ./scripts/audit_cards.sh                          # audit 100 cards, 200 workers
#   ./scripts/audit_cards.sh --limit 500              # override the limit
#   ./scripts/audit_cards.sh --deck 'Italian - CILS A1'
#   ./scripts/audit_cards.sh --only-problems
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

# shellcheck disable=SC1091
source "$HERE/_venv.sh"
ensure_venv "$ROOT"

python "$HERE/04_audit_cards.py" --limit 100 --workers 200 "$@"
