#!/usr/bin/env python3
"""Reorder new (unseen) cards in Anki to match the sort order from the generated decks.

Run after 22_import_anki_decks.py.

After importing an updated .apkg, Anki updates card content but leaves the new-card
queue positions unchanged. This script reads the SortKey field that was embedded in
every card during deck generation and reassigns each new card's `due` value to match,
so the new-card queue order reflects the frequency + sliding-window shuffle ordering.

Only new cards (type=0, queue=0) are touched. Cards that have already been seen/reviewed
are left completely alone — their scheduling is never modified.

Requires:
  - Anki running with the AnkiConnect add-on enabled (default port 8765)

Usage:
  python scripts/22_reorder_anki_cards.py
  python scripts/22_reorder_anki_cards.py --deck "Italian - Nouns"
  python scripts/22_reorder_anki_cards.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

ANKI_CONNECT_URL = "http://127.0.0.1:8765"
ANKI_CONNECT_VERSION = 6

# All Italian decks this pipeline manages. Omit a deck here to skip it.
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
        with urllib.request.urlopen(ANKI_CONNECT_URL, payload, timeout=30) as resp:
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


def reorder_deck(deck_name: str, dry_run: bool) -> dict[str, int]:
    """Reorder new cards in one deck by their SortKey field.

    Returns a dict with counts: total_new, reordered, skipped_no_sortkey.
    """
    stats = {"total_new": 0, "reordered": 0, "skipped_no_sortkey": 0}

    # Find all new (unseen) cards in this deck
    escaped = deck_name.replace('"', '\\"')
    card_ids: list[int] = invoke("findCards", query=f'deck:"{escaped}" is:new')

    if not card_ids:
        return stats

    stats["total_new"] = len(card_ids)

    # Fetch note info in chunks to get the SortKey field value
    # notesInfo takes note IDs, but we have card IDs — use cardsToNotes first
    note_ids: list[int] = invoke("cardsToNotes", cards=card_ids)

    # Build card_id → note_id mapping (one-to-one for this model)
    card_to_note: dict[int, int] = dict(zip(card_ids, note_ids))

    # Fetch note fields in chunks of 500
    note_id_list = list(set(note_ids))
    notes_info: dict[int, dict] = {}
    for chunk in chunked(note_id_list, 500):
        for note in invoke("notesInfo", notes=chunk):
            notes_info[note["noteId"]] = note

    # Build list of (sort_key_int, card_id) for cards that have a valid SortKey
    sortable: list[tuple[int, int]] = []
    for card_id in card_ids:
        note_id = card_to_note[card_id]
        note = notes_info.get(note_id)
        if not note:
            stats["skipped_no_sortkey"] += 1
            continue
        fields = note.get("fields", {})
        sort_key_value = fields.get("SortKey", {}).get("value", "").strip()
        if not sort_key_value or not sort_key_value.isdigit():
            stats["skipped_no_sortkey"] += 1
            continue
        sortable.append((int(sort_key_value), card_id))

    if not sortable:
        return stats

    # Sort by SortKey ascending — this is the desired new-card queue order
    sortable.sort(key=lambda x: x[0])

    # Assign due positions starting at 1, incrementing by 1
    # setSpecificValueOfCard with key "due" sets the new-card queue position
    if dry_run:
        stats["reordered"] = len(sortable)
        return stats

    for position, (_, card_id) in enumerate(sortable, start=1):
        invoke(
            "setSpecificValueOfCard",
            card=card_id,
            keys=["due"],
            newValues=[str(position)],
        )

    stats["reordered"] = len(sortable)
    return stats


def print_banner() -> None:
    title = "24 Reorder Anki new-card queue via AnkiConnect"
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


def main() -> int:
    print_banner()
    parser = argparse.ArgumentParser(
        description=(
            "Reorder new (unseen) Anki cards to match the sort order embedded in their "
            "SortKey field. Only new cards are touched; reviewed cards are never modified."
        )
    )
    parser.add_argument(
        "--deck",
        metavar="DECK_NAME",
        help="Reorder only this deck (default: all Italian decks).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be changed without modifying Anki.",
    )
    args = parser.parse_args()

    decks = [args.deck] if args.deck else ALL_DECKS

    if args.dry_run:
        print("DRY RUN — no changes will be made to Anki.\n")

    total_new = 0
    total_reordered = 0

    for deck_name in decks:
        try:
            stats = reorder_deck(deck_name, dry_run=args.dry_run)
        except RuntimeError as exc:
            print(f"  ERROR [{deck_name}]: {exc}", file=sys.stderr)
            continue

        new = stats["total_new"]
        reordered = stats["reordered"]
        skipped = stats["skipped_no_sortkey"]
        total_new += new
        total_reordered += reordered

        if new == 0:
            print(f"  {deck_name:<42}  no new cards")
        else:
            note = " (dry run)" if args.dry_run else ""
            skip_note = f"  {skipped} skipped (no SortKey)" if skipped else ""
            print(f"  {deck_name:<42}  {new:>5} new  →  {reordered} reordered{note}{skip_note}")

    print(f"\nDone. {total_reordered}/{total_new} new cards reordered.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
