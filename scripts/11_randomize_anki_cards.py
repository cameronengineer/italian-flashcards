#!/usr/bin/env python3
"""Apply weighted random reordering to anki_cards after frequency sort, per deck.

This script adds controlled randomness to the card order while preserving
the overall frequency-based structure. Uses Option 4: Weighted Random Sort Key.

Formula: new_order = original_order * weight + random_offset

Where:
- weight (multiplier) = 1 (fixed)
- random_offset = 0 to RANDOM_RANGE (default 50, tunable)

This creates a "local shuffle" where:
- Position 1 gets sort_key between 1-50
- Position 2 gets sort_key between 2-51
- Ranges overlap, allowing nearby cards to swap within the range window
- Randomization is applied per-deck independently
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "database.sqlite"

# Multiplier: controls strength of order preservation
# Using 1 to allow cards to shuffle up to RANDOM_RANGE positions
WEIGHT = 1

# Default randomness range
# Represents maximum positions a card can move from its original position
DEFAULT_RANDOM_RANGE = 50

# Per-deck randomness configuration
# Each deck can have its own random range for different shuffle intensities
RANDOM_RANGE_PER_DECK = {
    "nouns": 25,                      # Half randomness: lighter shuffle
    "verbs_infinito": 0,              # No randomness: preserve frequency order exactly
    "verbs_presente": 50,             # Default: moderate shuffle
    "verbs_passatoprossimo": 50,      # Default: moderate shuffle
    "verbs_imperfetto": 50,           # Default: moderate shuffle
    "verbs_imperativo": 50,           # Default: moderate shuffle
    "numbers": 0,                     # No randomness: preserve numeric order exactly
}


def randomize_anki_cards_per_deck(
    connection: sqlite3.Connection,
    random_range: int,
    random_range_per_deck: dict[str, int] | None = None,
) -> tuple[int, dict[str, int], dict[str, int]]:
    """Apply weighted random reordering to anki_cards, per deck, reassign IDs in new order.
    
    Uses the formula: sort_order = original_id * WEIGHT + ABS(RANDOM() % random_range)
    
    With WEIGHT=1 and random_range=50, cards can shuffle within ~50 positions
    of their original order while staying mostly in sequence, per deck.
    
    Args:
        connection: SQLite connection
        random_range: Default random range if no per-deck config provided
        random_range_per_deck: Optional dict mapping deck names to their random ranges
    
    Returns tuple of (total_cards, dict of deck_name: count, dict of deck_name: random_range_used).
    """
    if random_range_per_deck is None:
        random_range_per_deck = {}
    
    # Get all unique decks
    deck_rows = connection.execute(
        "SELECT DISTINCT deck FROM anki_cards ORDER BY deck"
    ).fetchall()
    
    deck_names = [row["deck"] for row in deck_rows]
    
    if not deck_names:
        return 0, {}, {}
    
    total_cards = 0
    deck_counts = {}
    deck_ranges_used = {}
    
    # Create temporary copy to randomize from (preserves current sorted state)
    connection.execute("DROP TABLE IF EXISTS anki_cards_temp")
    connection.execute("CREATE TEMPORARY TABLE anki_cards_temp AS SELECT * FROM anki_cards")
    
    # Delete all rows from anki_cards
    connection.execute("DELETE FROM anki_cards")
    
    # Process each deck independently
    current_id = 1
    for deck_name in deck_names:
        # Determine random range for this deck
        deck_random_range = random_range_per_deck.get(deck_name, random_range)
        deck_ranges_used[deck_name] = deck_random_range
        
        # Fetch all anki_cards for this deck and randomize their order
        # Put RANDOM() directly in the SELECT to ensure it's calculated per-row
        # When deck_random_range is 0, skip randomization to preserve exact order
        if deck_random_range == 0:
            sort_expr = f"id * {WEIGHT}"
        else:
            sort_expr = f"id * {WEIGHT} + ABS(RANDOM() % {deck_random_range})"
        rows = connection.execute(
            f"""
            SELECT *
            FROM (
                SELECT
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
                    guid,
                    {sort_expr} AS sort_order
                FROM anki_cards_temp
                WHERE deck = ?
            )
            ORDER BY sort_order, id
            """,
            (deck_name,),
        ).fetchall()

        deck_card_count = len(rows)
        deck_counts[deck_name] = deck_card_count
        total_cards += deck_card_count
        
        # Reinsert rows with new sequential IDs
        for row in rows:
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
    return total_cards, deck_counts, deck_ranges_used


def print_banner() -> None:
    title = "11 Randomize anki_cards (weighted)"
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


def main() -> None:
    print_banner()
    parser = argparse.ArgumentParser(
        description="Apply weighted random reordering to anki_cards per deck (Option 4: weighted random sort key).",
        epilog=f"Multiplier: {WEIGHT} (fixed). Supports per-deck randomness configuration."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--random-range",
        type=int,
        default=DEFAULT_RANDOM_RANGE,
        help=f"Default position range for shuffling (default: {DEFAULT_RANDOM_RANGE}). "
             f"Can be overridden per-deck in RANDOM_RANGE_PER_DECK."
    )
    args = parser.parse_args()

    with sqlite3.connect(args.db, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")

        total, deck_counts, deck_ranges_used = randomize_anki_cards_per_deck(
            connection, args.random_range, RANDOM_RANGE_PER_DECK
        )
        
        # Show a sample of the new ordering
        sample = connection.execute(
            """
            SELECT
                ac.id,
                ac.deck,
                ac.front_labels,
                ac.front_text,
                ac.back_highlight,
                COALESCE(iw.zipf, 0) as zipf
            FROM anki_cards ac
            LEFT JOIN card_items ci ON ac.card_item_id = ci.id
            LEFT JOIN verb_forms vf ON ci.source_type = 'verb_form' AND ci.source_id = vf.id
            LEFT JOIN noun_phrases np ON ci.source_type = 'noun_phrase' AND ci.source_id = np.id
            LEFT JOIN word_entries we ON vf.word_entry_id = we.id OR np.word_entry_id = we.id
            LEFT JOIN input_words iw ON we.input_word_id = iw.id
            ORDER BY ac.deck, ac.id
            LIMIT 20
            """
        ).fetchall()
        
        print(f"Randomized {total} anki_cards per deck (weight={WEIGHT}).")
        print(f"Formula: sort_key = id * {WEIGHT} + ABS(RANDOM() % random_range_per_deck)")
        
        print("\nPer-deck configuration:")
        for deck in sorted(deck_ranges_used.keys()):
            count = deck_counts.get(deck, 0)
            range_val = deck_ranges_used[deck]
            print(f"  {deck:<40} {count:>5} cards  random_range={range_val:>2}")
        
        print("\nFirst 20 cards (sample):")
        print(f"{'ID':<5} {'Deck':<30} {'Zipf':<7} {'Labels':<25} {'Back':<18}")
        print("-" * 85)
        for row in sample:
            print(f"{row['id']:<5} {row['deck'][:28]:<30} {row['zipf']:<7.2f} {str(row['front_labels'])[:23]:<25} {row['back_highlight'][:16]:<18}")


if __name__ == "__main__":
    main()
