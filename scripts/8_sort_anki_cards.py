#!/usr/bin/env python3
"""Sort anki_cards by zipf frequency and labels, per deck.

This script reorders the anki_cards table independently for each deck to ensure that cards are ordered by:
1. Zipf frequency (descending - most frequent first)
2. Card labels (ascending - alphabetically)

Sorting is done per-deck so that each deck (nouns, verbs_presente, etc.) maintains its own
frequency progression without interference from other deck types.

This resolves the issue where cards for the same word (e.g., all verb forms or all 4 noun forms)
were grouped together as they were generated at the same time. By sorting by frequency within
each deck, we get better interleaving of cards.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "database.sqlite"


def sort_anki_cards_per_deck(connection: sqlite3.Connection) -> tuple[int, dict[str, int]]:
    """Sort anki_cards by zipf frequency, verb/noun distribution, and labels, per deck.
    
    Sort order:
    1. zipf frequency DESC (most common words first)
    2. hash of base form (infinitive/singular) for distributing different lemmas
    3. labels ASC (alphabetically for tie-breaking)
    
    This ensures that cards from different verbs/nouns are interleaved,
    rather than all forms of one verb being grouped together.
    
    Returns a tuple of (total_cards_processed, dict of deck_name: count).
    """
    import hashlib
    
    # Get all unique decks
    deck_rows = connection.execute(
        "SELECT DISTINCT deck FROM anki_cards ORDER BY deck"
    ).fetchall()
    
    deck_names = [row["deck"] for row in deck_rows]
    
    if not deck_names:
        return 0, {}
    
    total_cards = 0
    deck_counts = {}
    
    # Delete all rows from anki_cards
    connection.execute("DELETE FROM anki_cards")
    
    # Process each deck independently
    current_id = 1
    for deck_name in deck_names:
        # Fetch all anki_cards for this deck with their associated zipf frequency and base form
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
                COALESCE(iw.zipf, 0) as zipf,
                COALESCE(we.infinitive, we.singular, ac.back_text, '') as base_form,
                COALESCE(ac.front_labels, '') as labels
            FROM anki_cards_temp ac
            LEFT JOIN card_items ci ON ac.card_item_id = ci.id
            LEFT JOIN verb_forms vf ON ci.source_type = 'verb_form' AND ci.source_id = vf.id
            LEFT JOIN noun_phrases np ON ci.source_type = 'noun_phrase' AND ci.source_id = np.id
            LEFT JOIN word_entries we ON vf.word_entry_id = we.id OR np.word_entry_id = we.id
            LEFT JOIN input_words iw ON we.input_word_id = iw.id
            WHERE ac.deck = ?
            ORDER BY zipf DESC, ac.id ASC
            """,
            (deck_name,),
        ).fetchall()
        
        # Sort in Python to include hash-based distribution
        def sort_key(row):
            # Hash the base form to get a deterministic but spread-out value for distribution
            if row["base_form"]:
                base_hash = int(hashlib.md5(row["base_form"].encode()).hexdigest()[:8], 16)
            else:
                base_hash = 0
            
            return (
                -row["zipf"],  # DESC
                base_hash % 100,  # Distribute lemmas across range 0-99 within same zipf level
                row["labels"],  # ASC
            )
        
        rows_sorted = sorted(rows, key=sort_key)

        deck_card_count = len(rows_sorted)
        deck_counts[deck_name] = deck_card_count
        total_cards += deck_card_count
        
        # Reinsert rows with new sequential IDs (reset per deck)
        for new_id, row in enumerate(rows_sorted, start=1):
            actual_id = current_id
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
                    actual_id,
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
    return total_cards, deck_counts


def print_banner() -> None:
    title = "8 Sort anki_cards by frequency"
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


def main() -> None:
    print_banner()
    parser = argparse.ArgumentParser(
        description="Sort anki_cards by zipf frequency and labels, per deck.",
        epilog="Each deck is sorted independently to maintain separate frequency progressions."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args()

    with sqlite3.connect(args.db, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        
        # Create temporary copy to sort from
        connection.execute("CREATE TEMPORARY TABLE anki_cards_temp AS SELECT * FROM anki_cards")
        
        total, deck_counts = sort_anki_cards_per_deck(connection)
        
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
            LIMIT 15
            """
        ).fetchall()
        
        print(f"Sorted {total} anki_cards by zipf frequency and labels (per deck).")
        print("\nPer-deck counts:")
        for deck, count in sorted(deck_counts.items()):
            print(f"  {deck:<40} {count:>5} cards")
        
        print("\nFirst 15 cards (sample):")
        print(f"{'ID':<5} {'Deck':<30} {'Zipf':<7} {'Labels':<20} {'Back':<20}")
        print("-" * 82)
        for row in sample:
            print(f"{row['id']:<5} {row['deck'][:28]:<30} {row['zipf']:<7.2f} {str(row['front_labels'])[:18]:<20} {row['back_highlight'][:18]:<20}")


if __name__ == "__main__":
    main()
