#!/usr/bin/env python3
"""Create card_items from input word entries (interjections, pronouns, conjunctions).

These entries are imported by script 2 from the inputs/ CSV files. Each word_entry
with word_type IN ('interjection', 'pronoun', 'conjunction') gets a single card_item
in the appropriate deck.

Decks created:
  - interjections  (word_type = 'interjection')
  - pronouns       (word_type = 'pronoun')
  - conjunctions   (word_type = 'conjunction')
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "database.sqlite"

# Maps word_type → deck key
WORD_TYPE_TO_DECK: dict[str, str] = {
    "interjection": "interjections",
    "pronoun": "pronouns",
    "conjunction": "conjunctions",
}


def create_input_word_card_items(connection: sqlite3.Connection) -> dict[str, int]:
    """Insert card_items for all input-word entries that don't already have one.

    Returns a dict mapping deck name → number of rows inserted.
    """
    rows = connection.execute(
        """
        SELECT
            we.id AS word_entry_id,
            we.word_type,
            we.lemma,
            we.english
        FROM word_entries we
        WHERE we.word_type IN ('interjection', 'pronoun', 'conjunction')
          AND NOT EXISTS (
              SELECT 1
              FROM card_items ci
              WHERE ci.source_type = 'input_word' AND ci.source_id = we.id
          )
        ORDER BY we.word_type, we.lemma
        """
    ).fetchall()

    counts: dict[str, int] = {}
    for row in rows:
        deck = WORD_TYPE_TO_DECK.get(row["word_type"], row["word_type"] + "s")
        label = f"type: {row['word_type']}"
        cursor = connection.execute(
            """
            INSERT INTO card_items (
                source_type,
                source_id,
                deck,
                front_text,
                front_labels,
                back_highlight,
                back_text,
                audio_text,
                image_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "input_word",
                row["word_entry_id"],
                deck,
                row["english"],
                label,
                row["lemma"],
                None,
                row["lemma"],
                row["lemma"],
            ),
        )
        if cursor.rowcount:
            counts[deck] = counts.get(deck, 0) + 1
    return counts


def print_banner() -> None:
    title = "7 Create card_items (interjections, pronouns, conjunctions)"
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


def main() -> None:
    print_banner()
    parser = argparse.ArgumentParser(
        description="Create card_items for input word entries (interjections, pronouns, conjunctions)."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args()

    with sqlite3.connect(args.db, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")

        # Count existing input_word card_items for early-exit check
        existing_count = connection.execute(
            "SELECT COUNT(*) AS count FROM card_items WHERE source_type = 'input_word'"
        ).fetchone()["count"]
        expected_count = connection.execute(
            """
            SELECT COUNT(*) AS count FROM word_entries
            WHERE word_type IN ('interjection', 'pronoun', 'conjunction')
            """
        ).fetchone()["count"]

        if existing_count >= expected_count:
            print(
                f"Already have {existing_count} input_word card_items "
                f"(expected: {expected_count}). Exiting."
            )
            return

        counts = create_input_word_card_items(connection)
        connection.commit()

    total = sum(counts.values())
    for deck in ("interjections", "pronouns", "conjunctions"):
        n = counts.get(deck, 0)
        if n:
            print(f"  Inserted {n} card_items into '{deck}'")
    print(f"Inserted {total} input_word card_items total.")


if __name__ == "__main__":
    main()
