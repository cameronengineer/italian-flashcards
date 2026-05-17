#!/usr/bin/env python3
"""Sort anki_cards by zipf frequency then band-shuffle within each deck.

Algorithm:
  1. Sort all cards for a deck by zipf DESC (most common words first).
  2. Divide the sorted list into consecutive bands of BAND_SIZE cards.
  3. Fully shuffle (Fisher-Yates) within each band independently.
  4. Reassign sequential IDs in the new order.

This preserves the broad frequency progression (you learn common words first)
while producing significant local randomness within each band.

Band size controls the trade-off:
  - Smaller band  → tighter frequency ordering, less randomness
  - Larger band   → looser frequency ordering, more randomness

Decks with no zipf data (Numbers) keep their existing sort order unchanged.
"""

from __future__ import annotations

import argparse
import random
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "database.sqlite"

# Band size: number of cards shuffled together as a group.
# Cards within the same band are fully randomised relative to each other.
DEFAULT_BAND_SIZE = 200

# Per-deck overrides. Omitted decks use DEFAULT_BAND_SIZE.
BAND_SIZE_PER_DECK: dict[str, int] = {
    "Italian - Verbs Infinitive":       50,   # preserve frequency closely
    "Italian - Nouns":                  200,  # strong shuffle
    "Italian - Verbs Presente":         200,
    "Italian - Verbs Passato Prossimo": 200,
    "Italian - Verbs Imperfetto":       200,
    "Italian - Verbs Imperativo":       200,
    "Italian - Numbers":                0,    # 0 = no shuffle, keep sort order exactly
    "Italian - Conjunctions":           0,    # small decks — no shuffle needed
    "Italian - Pronouns":               0,
    "Italian - Interjections":          0,
    "Italian - Espressioni con Avere":  0,
}


def band_shuffle(rows: list, band_size: int) -> list:
    """Sort rows by zipf DESC then shuffle within bands of band_size.

    If band_size is 0, return rows sorted by zipf DESC with no shuffling.
    """
    # Sort by zipf descending, then by existing id as stable tiebreak
    sorted_rows = sorted(rows, key=lambda r: (-r["zipf"], r["id"]))

    if band_size == 0:
        return sorted_rows

    result = []
    for start in range(0, len(sorted_rows), band_size):
        band = list(sorted_rows[start : start + band_size])
        random.shuffle(band)
        result.extend(band)
    return result


def randomize_anki_cards_per_deck(
    connection: sqlite3.Connection,
    default_band_size: int,
    band_size_per_deck: dict[str, int],
) -> tuple[int, dict[str, int], dict[str, int]]:
    """Apply band-shuffle reordering to anki_cards, per deck, reassign IDs.

    Returns (total_cards, deck_counts, deck_band_sizes_used).
    """
    deck_rows = connection.execute(
        "SELECT DISTINCT deck FROM anki_cards ORDER BY deck"
    ).fetchall()
    deck_names = [row["deck"] for row in deck_rows]

    if not deck_names:
        return 0, {}, {}

    total_cards = 0
    deck_counts: dict[str, int] = {}
    deck_bands_used: dict[str, int] = {}

    # Snapshot current state before any deletions
    connection.execute("DROP TABLE IF EXISTS anki_cards_temp")
    connection.execute(
        "CREATE TEMPORARY TABLE anki_cards_temp AS SELECT * FROM anki_cards"
    )

    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("DELETE FROM anki_cards")
    connection.execute("PRAGMA foreign_keys = ON")

    current_id = 1
    for deck_name in deck_names:
        band_size = band_size_per_deck.get(deck_name, default_band_size)
        deck_bands_used[deck_name] = band_size

        rows = connection.execute(
            """
            SELECT
                ac.id,
                ac.card_item_id,
                ac.direction,
                ac.deck,
                ac.front_text,
                ac.front_labels,
                ac.back_highlight,
                ac.back_text,
                ac.audio_text,
                ac.image_text,
                ac.guid,
                COALESCE(iw.zipf, 0) as zipf
            FROM anki_cards_temp ac
            LEFT JOIN card_items ci ON ac.card_item_id = ci.id
            LEFT JOIN noun_phrases np
                ON ci.source_type = 'noun_phrase' AND ci.source_id = np.id
            LEFT JOIN verb_forms vf
                ON ci.source_type = 'verb_form' AND ci.source_id = vf.id
            LEFT JOIN word_entries we
                ON np.word_entry_id = we.id OR vf.word_entry_id = we.id
            LEFT JOIN input_words iw ON we.input_word_id = iw.id
            WHERE ac.deck = ?
            """,
            (deck_name,),
        ).fetchall()

        ordered = band_shuffle(rows, band_size)

        deck_card_count = len(ordered)
        deck_counts[deck_name] = deck_card_count
        total_cards += deck_card_count

        for row in ordered:
            connection.execute(
                """
                INSERT INTO anki_cards (
                    id,
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
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    current_id,
                    row["card_item_id"],
                    row["direction"],
                    row["deck"],
                    row["front_text"],
                    row["front_labels"],
                    row["back_highlight"],
                    row["back_text"],
                    row["audio_text"],
                    row["image_text"],
                    row["guid"],
                ),
            )
            current_id += 1

    connection.commit()
    return total_cards, deck_counts, deck_bands_used


def print_banner() -> None:
    title = "11 Randomize anki_cards (band shuffle)"
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


def main() -> None:
    print_banner()
    parser = argparse.ArgumentParser(
        description=(
            "Sort anki_cards by zipf then band-shuffle within each deck. "
            "Band size controls randomness: larger = more shuffled."
        )
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--band-size",
        type=int,
        default=DEFAULT_BAND_SIZE,
        help=f"Default band size for decks not listed in BAND_SIZE_PER_DECK "
             f"(default: {DEFAULT_BAND_SIZE}). Use 0 to disable shuffling.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Optional random seed for reproducible shuffles.",
    )
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    with sqlite3.connect(args.db, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")

        total, deck_counts, deck_bands_used = randomize_anki_cards_per_deck(
            connection, args.band_size, BAND_SIZE_PER_DECK
        )

        print(f"Randomized {total} anki_cards across {len(deck_counts)} decks.")
        print(f"\nPer-deck configuration (band_size=0 means no shuffle):")
        for deck in sorted(deck_bands_used.keys()):
            count = deck_counts.get(deck, 0)
            band = deck_bands_used[deck]
            shuffle_note = "no shuffle" if band == 0 else f"band_size={band}"
            print(f"  {deck:<42} {count:>5} cards  {shuffle_note}")

        # Sample: show first 20 noun cards with their zipf to verify shuffling
        sample = connection.execute(
            """
            SELECT
                ac.id,
                ac.front_text,
                ac.back_highlight,
                ROUND(COALESCE(iw.zipf, 0), 2) as zipf
            FROM anki_cards ac
            LEFT JOIN card_items ci ON ac.card_item_id = ci.id
            LEFT JOIN noun_phrases np
                ON ci.source_type = 'noun_phrase' AND ci.source_id = np.id
            LEFT JOIN word_entries we ON np.word_entry_id = we.id
            LEFT JOIN input_words iw ON we.input_word_id = iw.id
            WHERE ac.deck = 'Italian - Nouns' AND ac.direction = 'en_to_it'
            ORDER BY ac.id
            LIMIT 20
            """
        ).fetchall()

        print("\nFirst 20 noun (en_to_it) cards — zipf should be roughly high→low but shuffled:")
        print(f"  {'ID':<6} {'Zipf':<6} {'Front':<30} {'Back'}")
        print("  " + "-" * 70)
        for row in sample:
            print(f"  {row['id']:<6} {row['zipf']:<6} {row['front_text'][:28]:<30} {row['back_highlight']}")


if __name__ == "__main__":
    main()
