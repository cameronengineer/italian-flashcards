#!/usr/bin/env python3
"""Create card_items from verb_forms and noun_phrases (nouns and verbs only)."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "database.sqlite"

TENSE_TO_DECK: dict[str, str] = {
    "presente": "Italian - Verbs Presente",
    "passato_prossimo": "Italian - Verbs Passato Prossimo",
    "imperfetto": "Italian - Verbs Imperfetto",
    "imperativo": "Italian - Verbs Imperativo",
}

NOUN_DECK = "Italian - Nouns"
NOUN_PHRASES_DECK = "Italian - Noun Phrases"
INFINITIVE_VERB_DECK = "Italian - Verbs Infinitive"

# Only definite articles (the dog / the dogs) go into NOUN_DECK.
# Everything else — indefinite, articulated prepositions, demonstratives,
# possessives — goes into NOUN_PHRASES_DECK.
NOUN_DECK_PHRASE_TYPES = {"definite"}


def create_verb_card_items(connection: sqlite3.Connection) -> int:
    rows = connection.execute(
        """
        SELECT
            vf.id,
            vf.word_entry_id,
            vf.tense,
            vf.person,
            vf.polarity,
            vf.italian,
            vf.english,
            vf.labels,
            we.infinitive
        FROM verb_forms vf
        JOIN word_entries we ON vf.word_entry_id = we.id
        WHERE NOT EXISTS (
            SELECT 1
            FROM card_items ci
            WHERE ci.source_type = 'verb_form' AND ci.source_id = vf.id
        )
        ORDER BY we.id, vf.tense, vf.person
        """
    ).fetchall()

    inserted = 0
    for row in rows:
        deck = TENSE_TO_DECK.get(row["tense"], f"verbs_{row['tense']}")
        natural_key = f"verb_form:{row['word_entry_id']}:{row['tense']}:{row['person']}:{row['polarity']}"
        cursor = connection.execute(
            """
            INSERT INTO card_items (
                source_type,
                source_id,
                natural_key,
                deck,
                front_text,
                front_labels,
                back_highlight,
                back_text,
                audio_text,
                image_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "verb_form",
                row["id"],
                natural_key,
                deck,
                row["english"],
                row["labels"],
                row["italian"],
                row["infinitive"],
                row["italian"],
                row["infinitive"],
            ),
        )
        inserted += cursor.rowcount
    return inserted


def create_infinitive_verb_card_items(connection: sqlite3.Connection) -> int:
    rows = connection.execute(
        """
        SELECT
            we.id as word_entry_id,
            we.infinitive,
            we.english
        FROM word_entries we
        WHERE we.word_type = 'verb'
          AND NOT EXISTS (
              SELECT 1
              FROM card_items ci
              WHERE ci.source_type = 'infinitive_verb' AND ci.source_id = we.id
          )
        ORDER BY we.id
        """
    ).fetchall()

    inserted = 0
    for row in rows:
        cursor = connection.execute(
            """
            INSERT INTO card_items (
                source_type,
                source_id,
                natural_key,
                deck,
                front_text,
                front_labels,
                back_highlight,
                back_text,
                audio_text,
                image_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "infinitive_verb",
                row["word_entry_id"],
                f"infinitive_verb:{row['word_entry_id']}",
                INFINITIVE_VERB_DECK,
                row["english"],
                "tense: infinitive",
                row["infinitive"],
                None,
                row["infinitive"],
                row["infinitive"],
            ),
        )
        inserted += cursor.rowcount
    return inserted


def create_noun_card_items(connection: sqlite3.Connection) -> int:
    rows = connection.execute(
        """
        SELECT
            np.id,
            np.word_entry_id,
            np.phrase_type,
            np.number,
            np.preposition,
            np.italian,
            np.english,
            np.labels,
            we.singular
        FROM noun_phrases np
        JOIN word_entries we ON np.word_entry_id = we.id
        WHERE NOT EXISTS (
            SELECT 1
            FROM card_items ci
            WHERE ci.source_type = 'noun_phrase' AND ci.source_id = np.id
        )
        ORDER BY we.id, np.phrase_type, np.number
        """
    ).fetchall()

    inserted = 0
    for row in rows:
        preposition = row["preposition"] or ""
        natural_key = f"noun_phrase:{row['word_entry_id']}:{row['phrase_type']}:{row['number']}:{preposition}"
        deck = NOUN_DECK if row["phrase_type"] in NOUN_DECK_PHRASE_TYPES else NOUN_PHRASES_DECK
        cursor = connection.execute(
            """
            INSERT INTO card_items (
                source_type,
                source_id,
                natural_key,
                deck,
                front_text,
                front_labels,
                back_highlight,
                back_text,
                audio_text,
                image_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "noun_phrase",
                row["id"],
                natural_key,
                deck,
                row["english"],
                row["labels"],
                row["italian"],
                None,
                row["italian"],
                row["singular"],
            ),
        )
        inserted += cursor.rowcount
    return inserted


def print_banner() -> None:
    title = "13 Create card_items (nouns and verbs)"
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


def main() -> None:
    print_banner()
    parser = argparse.ArgumentParser(
        description="Create card_items from verb_forms and noun_phrases."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args()

    with sqlite3.connect(args.db, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")

        verb_form_count = connection.execute(
            "SELECT COUNT(*) as count FROM verb_forms"
        ).fetchone()["count"]
        noun_phrase_count = connection.execute(
            "SELECT COUNT(*) as count FROM noun_phrases"
        ).fetchone()["count"]
        verb_entry_count = connection.execute(
            "SELECT COUNT(*) as count FROM word_entries WHERE word_type = 'verb'"
        ).fetchone()["count"]
        # Count only the source types this script is responsible for, so other scripts'
        # card_items (e.g. input_word items for conjunctions, avere expressions) don't
        # cause a spurious early exit.
        existing_count = connection.execute(
            """
            SELECT COUNT(*) as count FROM card_items
            WHERE source_type IN ('verb_form', 'noun_phrase', 'infinitive_verb')
            """
        ).fetchone()["count"]
        expected_count = verb_form_count + noun_phrase_count + verb_entry_count

        if existing_count >= expected_count:
            print(f"Already have {existing_count} verb/noun card_items (expected: {expected_count}). Exiting.")
            return

        verb_inserted = create_verb_card_items(connection)
        infinitive_inserted = create_infinitive_verb_card_items(connection)
        noun_inserted = create_noun_card_items(connection)
        connection.commit()

    total = verb_inserted + infinitive_inserted + noun_inserted
    print(f"Inserted {verb_inserted} verb conjugation card_items, {infinitive_inserted} infinitive verb card_items, and {noun_inserted} noun card_items ({total} total).")


if __name__ == "__main__":
    main()
