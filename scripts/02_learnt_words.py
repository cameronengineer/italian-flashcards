#!/usr/bin/env python3
"""Export a list of learnt words from Anki to learnt.txt.

A card is considered "learnt" when it has graduated from the learning queue
into review — i.e. type=2 (review) or type=3 (relearning).  New cards
(type=0) and cards still in learning (type=1) are excluded.

For each learnt card the Italian word/phrase (FrontText field on the it_to_en
card, which is the Italian side) and its English translation (BackHighlight)
are written to learnt.txt as a formatted table, grouped by deck.

Suspended cards are excluded — if a card has been suspended it is no longer
actively being reviewed.

Requires:
  - Anki running with the AnkiConnect add-on enabled (default port 8765)

Usage:
  python scripts/93_learnt_words.py
  python scripts/93_learnt_words.py --output path/to/output.txt
  python scripts/93_learnt_words.py --deck "Italian - Nouns"
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import tempfile
from pathlib import Path
from typing import Any

ANKI_CONNECT_URL = "http://127.0.0.1:8765"
ANKI_CONNECT_VERSION = 6

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = Path(tempfile.gettempdir()) / "italian-flashcards-learnt.txt"

ALL_DECKS = [
    "Italian - Nouns",
    "Italian - Verbs Infinitive",
    "Italian - Verbs Presente",
    "Italian - Verbs Passato Prossimo",
    "Italian - Verbs Imperfetto",
    "Italian - Verbs Imperativo",
    "Italian - Numbers",
    "Italian - Conjunctions",
    "Italian - Pronouns",
    "Italian - Interjections",
    "Italian - Espressioni con Avere",
    "Italian - Italki",
    "Italian - Italki Verbs Infinitive",
    "Italian - Italki Verbs Presente",
    "Italian - Italki Verbs Passato Prossimo",
    "Italian - Italki Verbs Imperfetto",
    "Italian - Italki Verbs Imperativo",
]


# ---------------------------------------------------------------------------
# AnkiConnect helpers
# ---------------------------------------------------------------------------

def invoke(action: str, **params: Any) -> Any:
    payload = json.dumps(
        {"action": action, "version": ANKI_CONNECT_VERSION, "params": params}
    ).encode("utf-8")
    try:
        with urllib.request.urlopen(ANKI_CONNECT_URL, payload, timeout=30) as resp:
            result = json.load(resp)
    except urllib.error.URLError as exc:
        raise RuntimeError(
            "Could not reach AnkiConnect. Make sure Anki is running with "
            "AnkiConnect enabled."
        ) from exc
    if len(result) != 2 or "error" not in result or "result" not in result:
        raise RuntimeError(f"Unexpected AnkiConnect response: {result!r}")
    if result["error"] is not None:
        raise RuntimeError(f"AnkiConnect error: {result['error']}")
    return result["result"]


def chunked(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def strip_html(text: str) -> str:
    """Remove HTML tags and decode common entities."""
    import re
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&nbsp;", " ")
    return text.strip()


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def get_learnt_cards(deck_name: str) -> list[tuple[str, str]]:
    """Return (italian, english) pairs for learnt cards in *deck_name*.

    Learnt = type 2 (review) or type 3 (relearning), not suspended.
    Uses the it_to_en card direction so FrontText is Italian and
    BackHighlight is the English translation.
    """
    escaped = deck_name.replace('"', '\\"')

    # is:review matches type=2 (review) and type=3 (relearning); exclude suspended
    card_ids: list[int] = invoke(
        "findCards",
        query=f'deck:"{escaped}" is:review -is:suspended',
    )
    if not card_ids:
        return []

    cards_info: list[dict] = []
    for chunk in chunked(card_ids, 500):
        cards_info.extend(invoke("cardsInfo", cards=chunk))

    # Collect note IDs for learnt cards (type 2 or 3 only)
    learnt_note_ids: list[int] = []
    seen_notes: set[int] = set()
    for card in cards_info:
        if not card:
            continue
        if card.get("type") not in (2, 3):
            continue
        note_id = card.get("note")
        if note_id and note_id not in seen_notes:
            seen_notes.add(note_id)
            learnt_note_ids.append(note_id)

    if not learnt_note_ids:
        return []

    # Fetch note fields to get Italian and English text
    notes_info: list[dict] = []
    for chunk in chunked(learnt_note_ids, 500):
        notes_info.extend(invoke("notesInfo", notes=chunk))

    pairs: list[tuple[str, str]] = []
    for note in notes_info:
        if not note:
            continue
        fields = note.get("fields", {})
        italian = strip_html(fields.get("FrontText", {}).get("value", ""))
        english = strip_html(fields.get("BackHighlight", {}).get("value", ""))
        if italian:
            pairs.append((italian, english))

    # Sort alphabetically by Italian word
    pairs.sort(key=lambda x: x[0].lower())
    return pairs


# ---------------------------------------------------------------------------
# Table formatting
# ---------------------------------------------------------------------------

def format_deck_table(deck_name: str, pairs: list[tuple[str, str]]) -> list[str]:
    """Return lines for a single deck's table."""
    if not pairs:
        return []

    col1_header = "Italian"
    col2_header = "English"
    col1_w = max(len(col1_header), max(len(p[0]) for p in pairs))
    col2_w = max(len(col2_header), max(len(p[1]) for p in pairs))

    def divider(char: str = "-") -> str:
        return f"+{char * (col1_w + 2)}+{char * (col2_w + 2)}+"

    def row(a: str, b: str) -> str:
        return f"| {a.ljust(col1_w)} | {b.ljust(col2_w)} |"

    lines: list[str] = []
    lines.append(deck_name)
    lines.append("=" * len(deck_name))
    lines.append(divider())
    lines.append(row(col1_header, col2_header))
    lines.append(divider("="))
    for italian, english in pairs:
        lines.append(row(italian, english))
    lines.append(divider())
    lines.append(f"{len(pairs)} word(s) learnt")
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def print_banner() -> None:
    title = "93 Learnt Words — export to learnt.txt"
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


def main() -> int:
    print_banner()
    parser = argparse.ArgumentParser(
        description=(
            "Export Italian words that have been learnt (graduated to review) "
            "from Anki decks into a formatted table in learnt.txt."
        )
    )
    parser.add_argument(
        "--deck",
        metavar="DECK_NAME",
        help="Export only this deck (default: all Italian decks).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        metavar="FILE",
        help=f"Output file path (default: {DEFAULT_OUTPUT}).",
    )
    args = parser.parse_args()

    decks = [args.deck] if args.deck else ALL_DECKS

    all_lines: list[str] = []
    summary_rows: list[tuple[str, int]] = []

    for deck_name in decks:
        try:
            pairs = get_learnt_cards(deck_name)
        except RuntimeError as exc:
            print(f"  ERROR [{deck_name}]: {exc}", file=sys.stderr)
            continue

        summary_rows.append((deck_name, len(pairs)))
        if pairs:
            all_lines.extend(format_deck_table(deck_name, pairs))

    # Write output file
    args.output.write_text("\n".join(all_lines), encoding="utf-8")

    # Print summary table to stdout
    col_w = max(len(d) for d, _ in summary_rows) if summary_rows else 4

    def divider() -> str:
        return f"+{'-' * (col_w + 2)}+{'-' * 9}+"

    def row(name: str, count: str) -> str:
        return f"| {name.ljust(col_w)} | {count.rjust(7)} |"

    print()
    print(divider())
    print(row("Deck", "Learnt"))
    print(f"+{'=' * (col_w + 2)}+{'=' * 9}+")
    for deck_name, count in summary_rows:
        print(row(deck_name, str(count)))
    print(divider())
    total = sum(c for _, c in summary_rows)
    print(row("TOTAL", str(total)))
    print(divider())

    print(f"\nWritten to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
