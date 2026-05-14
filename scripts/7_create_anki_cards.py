#!/usr/bin/env python3
"""Create anki_cards from card_items with stable GUIDs."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import genanki

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "database.sqlite"


def generate_guid(card_item_id: int, deck: str, front_text: str, back_text: str) -> str:
    """Generate stable GUID using genanki's method plus card_item_id for uniqueness."""
    # Include card_item_id to ensure uniqueness for cases where deck/front_text/back_text
    # might be identical (e.g., multiple noun phrases with same English prompt)
    return genanki.guid_for(deck, front_text, back_text, str(card_item_id))


def create_anki_cards(connection: sqlite3.Connection) -> int:
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
        )
        ORDER BY ci.id
        """
    ).fetchall()

    inserted = 0
    for row in rows:
        guid = generate_guid(
            row["id"],
            row["deck"],
            row["front_text"],
            row["back_text"] or "",
        )
        cursor = connection.execute(
            """
            INSERT INTO anki_cards (
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
                guid,
            ),
        )
        inserted += cursor.rowcount
    return inserted


def print_banner() -> None:
    title = "7 Create anki_cards"
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

        if existing_count >= card_item_count:
            print(f"Already have {existing_count} anki_cards (expected: {card_item_count}). Exiting.")
            return

        inserted = create_anki_cards(connection)
        connection.commit()

    print(f"Inserted {inserted} anki_cards.")


if __name__ == "__main__":
    main()
