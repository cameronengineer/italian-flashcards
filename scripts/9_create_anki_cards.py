#!/usr/bin/env python3
"""Create anki_cards from card_items with stable GUIDs."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import genanki

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "database.sqlite"


def generate_guid(card_item_id: int, deck: str, front_text: str, back_text: str, direction: str = "en_to_it") -> str:
    """Generate stable GUID using genanki's method plus card_item_id for uniqueness."""
    # Include card_item_id to ensure uniqueness for cases where deck/front_text/back_text
    # might be identical (e.g., multiple noun phrases with same English prompt).
    # Include direction so en_to_it and it_to_en cards get distinct GUIDs.
    return genanki.guid_for(deck, front_text, back_text, str(card_item_id), direction)


def create_anki_cards(connection: sqlite3.Connection) -> int:
    # Find card_items missing either direction so this function is idempotent.
    rows = connection.execute(
        """
        SELECT
            ci.id,
            ci.deck,
            ci.front_text,
            ci.front_labels,
            ci.back_highlight,
            ci.back_text,
            ci.audio_text,
            ci.image_text
        FROM card_items ci
        WHERE NOT EXISTS (
            SELECT 1
            FROM anki_cards ac
            WHERE ac.card_item_id = ci.id
              AND ac.direction = 'en_to_it'
        )
           OR NOT EXISTS (
            SELECT 1
            FROM anki_cards ac
            WHERE ac.card_item_id = ci.id
              AND ac.direction = 'it_to_en'
        )
        ORDER BY ci.id
        """
    ).fetchall()

    inserted = 0
    for row in rows:
        # --- en_to_it card (English prompt → Italian answer) ---
        guid_en = generate_guid(
            row["id"],
            row["deck"],
            row["front_text"],
            row["back_text"] or "",
            "en_to_it",
        )
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO anki_cards (
                card_item_id,
                direction,
                deck,
                front_text,
                front_labels,
                back_highlight,
                back_text,
                audio_text,
                image_text,
                guid
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                "en_to_it",
                row["deck"],
                row["front_text"],
                row["front_labels"],
                row["back_highlight"],
                row["back_text"],
                row["audio_text"],
                row["image_text"],
                guid_en,
            ),
        )
        inserted += cursor.rowcount

        # --- it_to_en card (Italian prompt → English answer) ---
        # front_text becomes the Italian form; back_highlight becomes the English prompt.
        # Audio and image keys are unchanged — they still reference the Italian text.
        guid_it = generate_guid(
            row["id"],
            row["deck"],
            row["front_text"],
            row["back_text"] or "",
            "it_to_en",
        )
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO anki_cards (
                card_item_id,
                direction,
                deck,
                front_text,
                front_labels,
                back_highlight,
                back_text,
                audio_text,
                image_text,
                guid
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                "it_to_en",
                row["deck"],
                row["back_highlight"],
                row["front_labels"],
                row["front_text"],
                row["back_text"],
                row["audio_text"],
                row["image_text"],
                guid_it,
            ),
        )
        inserted += cursor.rowcount

    return inserted


def print_banner() -> None:
    title = "9 Create anki_cards"
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


def main() -> None:
    print_banner()
    parser = argparse.ArgumentParser(
        description="Create anki_cards from card_items with stable GUIDs."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args()

    with sqlite3.connect(args.db, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")

        card_item_count = connection.execute(
            "SELECT COUNT(*) as count FROM card_items"
        ).fetchone()["count"]
        existing_count = connection.execute(
            "SELECT COUNT(*) as count FROM anki_cards"
        ).fetchone()["count"]
        expected_count = card_item_count * 2  # en_to_it + it_to_en per card_item

        if existing_count >= expected_count:
            print(f"Already have {existing_count} anki_cards (expected: {expected_count}). Exiting.")
            return

        inserted = create_anki_cards(connection)
        connection.commit()

    print(f"Inserted {inserted} anki_cards.")


if __name__ == "__main__":
    main()
