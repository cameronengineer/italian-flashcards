#!/usr/bin/env python3
"""AI audit of the final generated flashcards.

Walks every logical card in the ``cards`` table (one row per natural_key —
the reverse en↔it direction is skipped since it carries the same content) and
sends it to an AI model for review. The model checks each card for:

  * Correctness   — is the Italian↔English translation accurate?
  * Grammar       — is the Italian grammatically correct (gender, agreement,
                    conjugation, articles)?
  * Naturalness   — does the phrasing sound natural to a native speaker?
  * Consistency   — do front_text, back_highlight, back_text, front_labels and
                    audio_text agree with one another?

Results are written to a timestamped CSV in the ``audit_reports/`` folder
(``audit_reports/audit_report_YYYYMMDD_HHMMSS.csv``) with one row per audited
card.

The model verdict is structured JSON (strict schema) so the report is easy to
filter — sort by ``severity`` or grep ``verdict == "fail"`` to find the cards
worth fixing by hand.

Requires:
  * .openrouter API key file in the project root.

Usage:
  python scripts/04_audit_cards.py
  python scripts/04_audit_cards.py --deck "Italian - CILS A1"
  python scripts/04_audit_cards.py --deck "Italian - CILS A1" --deck "Italian - CILS A2"
  python scripts/04_audit_cards.py --limit 100
  python scripts/04_audit_cards.py --workers 8
  python scripts/04_audit_cards.py --only-problems   # CSV holds only warn/fail rows
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime
from pathlib import Path

# Make the `flashcards` package importable when run as a standalone script.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from flashcards.db import connect  # noqa: E402
from flashcards.openrouter import OpenRouterError, request_structured  # noqa: E402
from flashcards.pool import run_pool  # noqa: E402
from flashcards.util import load_key_file, print_banner  # noqa: E402
from flashcards.paths import OPENROUTER_KEY_FILE  # noqa: E402

MODEL = "deepseek/deepseek-v4-flash"

# Strict structured-output schema for one card verdict.
AUDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "warn", "fail"]},
        "severity": {"type": "integer", "minimum": 0, "maximum": 5},
        "categories": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["correctness", "grammar", "naturalness", "consistency"],
            },
        },
        "issues": {"type": "string"},
        "suggestion": {"type": "string"},
    },
    "required": ["verdict", "severity", "categories", "issues", "suggestion"],
    "additionalProperties": False,
}

SYSTEM_GUIDANCE = (
    "You are a meticulous Italian-language reviewer auditing Anki flashcards "
    "produced by an automated pipeline. Each card teaches an Italian word or "
    "phrase to an English speaker. Judge the card on four axes:\n"
    "  1. correctness  — the Italian and English mean the same thing.\n"
    "  2. grammar      — the Italian is grammatically correct: gender, "
    "number agreement, conjugation, articles, accents.\n"
    "  3. naturalness  — the Italian and English both read naturally to a "
    "native speaker; not stilted or word-for-word.\n"
    "  4. consistency  — the front, back, highlight, labels and audio text "
    "all agree and describe the same item.\n\n"
    "Be strict but fair. A perfectly fine card is verdict 'pass', severity 0, "
    "empty issues and suggestion. Use 'warn' (severity 1-2) for minor stylistic "
    "nits and 'fail' (severity 3-5) for genuine errors that would mislead a "
    "learner. Keep 'issues' concise (one or two sentences). 'suggestion' is the "
    "corrected Italian/English when you propose a fix, otherwise an empty string."
)


def fetch_cards(conn, decks: list[str] | None, limit: int | None) -> list[dict]:
    """Every card row — both en_to_it and it_to_en directions are audited."""
    where = ""
    params: list = []
    if decks:
        placeholders = ",".join("?" * len(decks))
        where = f"WHERE deck IN ({placeholders})"
        params = list(decks)
    sql = f"""
        SELECT
            id,
            natural_key,
            direction,
            deck,
            front_text,
            front_labels,
            back_highlight,
            back_text,
            audio_text,
            image_text
        FROM cards
        {where}
        ORDER BY deck, sort_order, direction
    """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


DIRECTION_DESC = {
    "en_to_it": "English prompt → Italian answer (learner recalls the Italian)",
    "it_to_en": "Italian prompt → English answer (learner recalls the English)",
}


def build_prompt(card: dict) -> str:
    direction = card.get("direction") or ""
    direction_note = DIRECTION_DESC.get(direction, direction)
    return (
        f"{SYSTEM_GUIDANCE}\n\n"
        "Audit this flashcard and respond with the structured verdict.\n\n"
        f"Card direction: {direction_note}\n\n"
        "Card fields:\n"
        f"- deck:           {card.get('deck') or ''}\n"
        f"- front_text:     {card.get('front_text') or ''}\n"
        f"- front_labels:   {card.get('front_labels') or ''}\n"
        f"- back_highlight: {card.get('back_highlight') or ''}\n"
        f"- back_text:      {card.get('back_text') or ''}\n"
        f"- audio_text:     {card.get('audio_text') or ''}\n"
    )


def audit_card(card: dict, api_key: str) -> dict:
    result = request_structured(
        prompt=build_prompt(card),
        schema_name="card_audit",
        schema=AUDIT_SCHEMA,
        api_key=api_key,
        model=MODEL,
        timeout=90,
    )
    return result


def main() -> int:
    print_banner("04 Audit Cards — AI review of generated flashcards")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--deck", action="append", metavar="DECK",
        help="Only audit this deck (repeatable). Default: all decks.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Audit at most this many cards (after deck filtering).",
    )
    parser.add_argument(
        "--workers", type=int, default=6,
        help="Concurrent AI requests (default: 6).",
    )
    parser.add_argument(
        "--only-problems", action="store_true",
        help="Write only warn/fail rows to the CSV (skip clean passes).",
    )
    parser.add_argument(
        "--out", type=Path, default=None,
        help="Output CSV path (default: audit_reports/audit_report_<timestamp>.csv).",
    )
    args = parser.parse_args()

    api_key = load_key_file(OPENROUTER_KEY_FILE)

    with connect() as conn:
        cards = fetch_cards(conn, decks=args.deck or None, limit=args.limit)

    if not cards:
        print("  No cards matched. Nothing to audit.")
        return 0

    print(f"  Auditing {len(cards)} cards with model '{MODEL}'.")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    reports_dir = PROJECT_ROOT / "audit_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out or (reports_dir / f"audit_report_{ts}.csv")

    fieldnames = [
        "id", "deck", "natural_key", "direction", "verdict", "severity",
        "categories", "issues", "suggestion",
        "front_text", "front_labels", "back_highlight", "back_text", "audio_text",
    ]

    counts = {"pass": 0, "warn": 0, "fail": 0, "error": 0}

    def work(card: dict) -> dict:
        return audit_card(card, api_key)

    written = 0
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()

        for card, res in run_pool(
            cards, work, workers=args.workers, label="cards",
            describe=lambda c: (c.get("back_highlight") or c.get("front_text") or "")[:50],
        ):
            if isinstance(res, Exception):
                counts["error"] += 1
                verdict = "error"
                row = {
                    "id": card["id"], "deck": card["deck"],
                    "natural_key": card["natural_key"],
                    "direction": card.get("direction") or "",
                    "verdict": "error", "severity": "",
                    "categories": "", "issues": str(res), "suggestion": "",
                    "front_text": card.get("front_text") or "",
                    "front_labels": card.get("front_labels") or "",
                    "back_highlight": card.get("back_highlight") or "",
                    "back_text": card.get("back_text") or "",
                    "audio_text": card.get("audio_text") or "",
                }
            else:
                verdict = res.get("verdict", "pass")
                counts[verdict] = counts.get(verdict, 0) + 1
                row = {
                    "id": card["id"], "deck": card["deck"],
                    "natural_key": card["natural_key"],
                    "direction": card.get("direction") or "",
                    "verdict": verdict,
                    "severity": res.get("severity", 0),
                    "categories": ", ".join(res.get("categories", []) or []),
                    "issues": res.get("issues", "") or "",
                    "suggestion": res.get("suggestion", "") or "",
                    "front_text": card.get("front_text") or "",
                    "front_labels": card.get("front_labels") or "",
                    "back_highlight": card.get("back_highlight") or "",
                    "back_text": card.get("back_text") or "",
                    "audio_text": card.get("audio_text") or "",
                }

            if args.only_problems and verdict == "pass":
                continue
            # Persist each row to disk as its AI call completes, so a long run
            # that is interrupted (Ctrl-C, crash) keeps every result gathered
            # so far rather than losing buffered rows.
            writer.writerow(row)
            fh.flush()
            os.fsync(fh.fileno())
            written += 1

    total = len(cards)
    print(
        f"\n  Done. {total} audited — "
        f"pass={counts['pass']}, warn={counts['warn']}, "
        f"fail={counts['fail']}, error={counts['error']}."
    )
    print(f"  Report written: {out_path}  ({written} rows)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except OpenRouterError as exc:
        print(f"\nAborted: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
