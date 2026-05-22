#!/usr/bin/env python3
"""Sort anki_cards by zipf frequency then word-level sliding-window shuffle within each deck.

Algorithm:
  1. Group cards by card_item_id (all directions of the same word form one group).
  2. Sort groups by zipf DESC (most common words first).
  3. Apply Fisher-Yates over word-groups with a forward window of WINDOW_SIZE —
     each group position i swaps with a random group in [i, i+W-1].
  4. Emit all groups' "first direction" cards as pass_a, then all groups'
     "second direction" cards as pass_b: [pass_a... | pass_b...].
     Each pass contains at most one card per word, so the two directions of
     the same word can never be adjacent.

Window size controls the trade-off:
  - Smaller window → tighter frequency ordering, less randomness
  - Larger window  → looser frequency ordering, more randomness

Decks with window_size=0 keep groups in zipf-sorted order but still separate
directions across two passes.
"""

from __future__ import annotations

import argparse
import random
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "database.sqlite"

# Window size: how far ahead each card can shift from its frequency-sorted position.
# At position i, a random card is chosen from [i, min(i + W - 1, N-1)] and swapped in.
# This is a locality-constrained Fisher-Yates — cards stay roughly in frequency order
# but are mixed within a moving window of this width.
DEFAULT_WINDOW_SIZE = 50

# Per-deck overrides. Omitted decks use DEFAULT_WINDOW_SIZE.
WINDOW_SIZE_PER_DECK: dict[str, int] = {
    "Italian - Verbs Infinitive":       50,
    "Italian - Nouns":                  50,
    "Italian - Verbs Presente":         50,
    "Italian - Verbs Passato Prossimo": 50,
    "Italian - Verbs Imperfetto":       50,
    "Italian - Verbs Imperativo":       50,
    "Italian - Numbers":                0,
    "Italian - Conjunctions":           0,
    "Italian - Pronouns":               0,
    "Italian - Interjections":          0,
    "Italian - Espressioni con Avere":  0,
    "Italian - Italki":                         0,
    "Italian - Italki Verbs Infinitive":        50,
    "Italian - Italki Verbs Presente":          50,
    "Italian - Italki Verbs Passato Prossimo":  50,
    "Italian - Italki Verbs Imperfetto":        50,
    "Italian - Italki Verbs Imperativo":        50,
}


def sliding_window_shuffle(rows: list, window_size: int) -> list:
    """Sort rows by zipf DESC then apply a word-level sliding-window shuffle.

    Cards are first grouped by card_item_id so that all directions of the same
    word form (e.g. en_to_it and it_to_en) are treated as a single unit.
    The sliding window operates over these word-groups so that the broad
    frequency ordering is preserved. After shuffling the word order, the
    individual direction-cards are emitted in two separate passes so that the
    two directions of the same word are never placed next to each other.

    Algorithm:
      1. Group cards by card_item_id; sort groups by zipf DESC.
      2. Apply Fisher-Yates over word-groups with the forward window
         [i, min(i+W-1, N-1)] — groups move as a unit.
      3. Emit pass_a (one randomly chosen card per group) followed by
         pass_b (the second card per group, if it exists).
         Within each pass every card belongs to a different word, so no
         two cards from the same word can ever be adjacent.

    window_size=0 skips the inter-group shuffle but still separates
    directions across two passes so same-word cards are not adjacent.
    """
    from collections import defaultdict

    # --- 1. Group cards by card_item_id ---
    groups: dict[int, list] = defaultdict(list)
    for row in rows:
        groups[row["card_item_id"]].append(row)

    # Sort groups by zipf DESC (all cards in a group share the same word so
    # zipf is identical; min id breaks ties deterministically).
    sorted_groups: list[list] = sorted(
        groups.values(),
        key=lambda g: (-max(r["zipf"] for r in g), min(r["id"] for r in g)),
    )

    # --- 2. Sliding window Fisher-Yates over word-groups ---
    n = len(sorted_groups)
    if window_size > 1:
        for i in range(n):
            j = random.randint(i, min(i + window_size - 1, n - 1))
            sorted_groups[i], sorted_groups[j] = sorted_groups[j], sorted_groups[i]

    # --- 3. Separate directions so same-word cards are never adjacent ---
    # Split each group into two passes: pass_a gets one card per group,
    # pass_b gets the second card per group (if it exists).
    # The final sequence is: all of pass_a, then all of pass_b.
    # Because pass_a and pass_b each contain exactly one card per word,
    # no two cards from the same word are ever adjacent within a pass.
    # The seam between pass_a and pass_b can only place the last word of
    # pass_a next to the first word of pass_b — those are different words.
    pass_a: list = []
    pass_b: list = []
    extras: list = []
    for group in sorted_groups:
        shuffled = list(group)
        random.shuffle(shuffled)
        if len(shuffled) >= 1:
            pass_a.append(shuffled[0])
        if len(shuffled) >= 2:
            pass_b.append(shuffled[1])
        if len(shuffled) > 2:
            extras.extend(shuffled[2:])

    return pass_a + pass_b + extras


def randomize_anki_cards_per_deck(
    connection: sqlite3.Connection,
    default_window_size: int,
    window_size_per_deck: dict[str, int],
) -> tuple[int, dict[str, int], dict[str, int]]:
    """Apply sliding-window shuffle reordering to anki_cards, per deck, reassign IDs.

    Returns (total_cards, deck_counts, deck_window_sizes_used).
    """
    deck_rows = connection.execute(
        "SELECT DISTINCT deck FROM anki_cards ORDER BY deck"
    ).fetchall()
    deck_names = [row["deck"] for row in deck_rows]

    if not deck_names:
        return 0, {}, {}

    total_cards = 0
    deck_counts: dict[str, int] = {}
    deck_windows_used: dict[str, int] = {}

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
        band_size = window_size_per_deck.get(deck_name, default_window_size)
        deck_windows_used[deck_name] = band_size

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

        ordered = sliding_window_shuffle(rows, band_size)

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
    return total_cards, deck_counts, deck_windows_used


def print_banner() -> None:
    title = "17 Randomize anki_cards (sliding window shuffle)"
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


def main() -> None:
    print_banner()
    parser = argparse.ArgumentParser(
        description=(
            "Sort anki_cards by zipf then apply a sliding-window shuffle within each deck. "
            "Window size controls randomness: larger = more shuffled."
        )
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--window-size",
        type=int,
        default=DEFAULT_WINDOW_SIZE,
        help=f"Default window size for decks not listed in WINDOW_SIZE_PER_DECK "
             f"(default: {DEFAULT_WINDOW_SIZE}). Use 0 to disable shuffling.",
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

        total, deck_counts, deck_windows_used = randomize_anki_cards_per_deck(
            connection, args.window_size, WINDOW_SIZE_PER_DECK
        )

        print(f"Randomized {total} anki_cards across {len(deck_counts)} decks.")
        print(f"\nPer-deck configuration (window_size=0 means no shuffle):")
        for deck in sorted(deck_windows_used.keys()):
            count = deck_counts.get(deck, 0)
            window = deck_windows_used[deck]
            shuffle_note = "no shuffle" if window == 0 else f"window_size={window}"
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
