#!/usr/bin/env python3
"""Delete cards from Anki that no longer exist in the local database.

After importing updated .apkg files, any card that was removed from the pipeline
(e.g. a word entry deleted, a card regenerated with a new GUID) will still sit in
Anki untouched. This script finds those orphans and deletes them.

Strategy:
  - For each managed deck, fetch all notes from Anki and read their SortKey field.
  - Load all valid SortKey values (anki_cards.id) from the local database.
  - Any note whose SortKey is not in the DB set is an orphan — delete it.

Requires:
  - Anki running with the AnkiConnect add-on enabled (default port 8765)
  - The SortKey field must exist on all cards (added by 21_create_decks.py)

Run after 22_import_anki_decks.py and before or after 23_reorder_anki_cards.py.

Usage:
  python scripts/24_delete_orphan_anki_cards.py
  python scripts/24_delete_orphan_anki_cards.py --deck "Italian - Nouns"
  python scripts/24_delete_orphan_anki_cards.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ANKI_CONNECT_URL = "http://127.0.0.1:8765"
ANKI_CONNECT_VERSION = 6

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "database.sqlite"

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


def invoke(action: str, **params: Any) -> Any:
    payload = json.dumps(
        {"action": action, "version": ANKI_CONNECT_VERSION, "params": params}
    ).encode("utf-8")
    try:
        with urllib.request.urlopen(ANKI_CONNECT_URL, payload, timeout=60) as resp:
            result = json.load(resp)
    except urllib.error.URLError as exc:
        raise RuntimeError(
            "Could not reach AnkiConnect. Make sure Anki is running with AnkiConnect enabled."
        ) from exc
    if len(result) != 2 or "error" not in result or "result" not in result:
        raise RuntimeError(f"Unexpected AnkiConnect response: {result!r}")
    if result["error"] is not None:
        raise RuntimeError(f"AnkiConnect error: {result['error']}")
    return result["result"]


def chunked(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def load_valid_sort_keys(db_path: Path) -> set[str]:
    """Return all anki_cards.id values from the local DB as strings."""
    with sqlite3.connect(db_path, timeout=30) as conn:
        rows = conn.execute("SELECT id FROM anki_cards").fetchall()
    return {str(row[0]) for row in rows}


def find_orphans_in_deck(deck_name: str, valid_sort_keys: set[str]) -> list[int]:
    """Return note IDs in Anki for this deck whose SortKey is not in valid_sort_keys."""
    escaped = deck_name.replace('"', '\\"')
    note_ids: list[int] = invoke("findNotes", query=f'deck:"{escaped}"')

    if not note_ids:
        return []

    orphan_note_ids: list[int] = []

    for chunk in chunked(note_ids, 500):
        notes = invoke("notesInfo", notes=chunk)
        for note in notes:
            sort_key = note.get("fields", {}).get("SortKey", {}).get("value", "").strip()
            if not sort_key or sort_key not in valid_sort_keys:
                orphan_note_ids.append(note["noteId"])

    return orphan_note_ids


def print_banner() -> None:
    title = "23 Delete orphan Anki cards via AnkiConnect"
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


def main() -> int:
    print_banner()
    parser = argparse.ArgumentParser(
        description=(
            "Delete cards from Anki whose SortKey no longer exists in the local database. "
            "Only cards belonging to managed Italian decks are considered."
        )
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--deck",
        metavar="DECK_NAME",
        help="Check only this deck (default: all managed Italian decks).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report which cards would be deleted without actually deleting them.",
    )
    args = parser.parse_args()

    decks = [args.deck] if args.deck else ALL_DECKS

    if args.dry_run:
        print("DRY RUN — no changes will be made to Anki.\n")

    valid_sort_keys = load_valid_sort_keys(args.db)
    print(f"Loaded {len(valid_sort_keys)} valid SortKey values from local DB.")

    total_orphans = 0
    total_deleted = 0

    for deck_name in decks:
        try:
            orphan_ids = find_orphans_in_deck(deck_name, valid_sort_keys)
        except RuntimeError as exc:
            print(f"  ERROR [{deck_name}]: {exc}", file=sys.stderr)
            continue

        if not orphan_ids:
            print(f"  {deck_name:<42}  no orphans")
            continue

        total_orphans += len(orphan_ids)
        note = " (dry run)" if args.dry_run else ""
        print(f"  {deck_name:<42}  {len(orphan_ids)} orphan(s) found{note}")

        if not args.dry_run:
            for chunk in chunked(orphan_ids, 500):
                invoke("deleteNotes", notes=chunk)
            total_deleted += len(orphan_ids)

    if args.dry_run:
        print(f"\nDone. {total_orphans} orphan(s) would be deleted.")
    else:
        print(f"\nDone. {total_deleted} orphan note(s) deleted.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
