#!/usr/bin/env python3
"""Create the SQLite schema and import SUBTLEX-IT rows."""

from __future__ import annotations

import argparse
import csv
import hashlib
import sqlite3
from pathlib import Path

DEFAULT_CSV_PATH = Path("freqdic/subtlex-it.cleaned.cleaned.csv")
DEFAULT_DB_PATH = Path("database.sqlite")
DEFAULT_LIMIT = 20_000

INTEGER_COLUMNS = {
    "freq_count",
    "cd_count",
    "id",
}
REAL_COLUMNS = {
    "zipf",
}

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS input_words (
    id TEXT PRIMARY KEY,
    subtlex_id INTEGER NOT NULL UNIQUE,
    wordform TEXT NOT NULL,
    normalized_word TEXT NOT NULL,
    frequency_rank INTEGER,
    freq_count INTEGER,
    zipf REAL,
    cd_count INTEGER,
    dom_pos TEXT,
    dom_lemma TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_input_words_rank ON input_words(frequency_rank);
CREATE INDEX IF NOT EXISTS idx_input_words_normalized_word ON input_words(normalized_word);
CREATE INDEX IF NOT EXISTS idx_input_words_dom_pos ON input_words(dom_pos);
CREATE INDEX IF NOT EXISTS idx_input_words_dom_lemma ON input_words(dom_lemma);

CREATE TABLE IF NOT EXISTS word_entries (
    id TEXT PRIMARY KEY,
    input_word_id TEXT NOT NULL,
    word_type TEXT NOT NULL,
    lemma TEXT NOT NULL,
    english TEXT,
    confidence REAL,
    infinitive TEXT,
    auxiliary TEXT,
    past_participle TEXT,
    is_reflexive INTEGER NOT NULL DEFAULT 0,
    singular TEXT,
    singular_english TEXT,
    plural TEXT,
    plural_english TEXT,
    gender TEXT,
    definite_singular TEXT,
    definite_plural TEXT,
    indefinite_singular TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (input_word_id) REFERENCES input_words(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_word_entries_input_word_id ON word_entries(input_word_id);
CREATE INDEX IF NOT EXISTS idx_word_entries_type ON word_entries(word_type);
CREATE INDEX IF NOT EXISTS idx_word_entries_lemma ON word_entries(lemma);
CREATE UNIQUE INDEX IF NOT EXISTS idx_word_entries_type_lemma ON word_entries(word_type, lemma);

CREATE TABLE IF NOT EXISTS verb_forms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    word_entry_id TEXT NOT NULL,
    tense TEXT NOT NULL,
    person TEXT,
    polarity TEXT NOT NULL DEFAULT 'positive',
    italian TEXT NOT NULL,
    english TEXT NOT NULL,
    labels TEXT,
    FOREIGN KEY (word_entry_id) REFERENCES word_entries(id) ON DELETE CASCADE,
    UNIQUE(word_entry_id, tense, person, polarity, italian)
);

CREATE INDEX IF NOT EXISTS idx_verb_forms_entry ON verb_forms(word_entry_id);
CREATE INDEX IF NOT EXISTS idx_verb_forms_tense ON verb_forms(tense);

CREATE TABLE IF NOT EXISTS noun_phrases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    word_entry_id TEXT NOT NULL,
    phrase_type TEXT NOT NULL,
    number TEXT NOT NULL,
    preposition TEXT,
    italian TEXT NOT NULL,
    english TEXT NOT NULL,
    labels TEXT,
    FOREIGN KEY (word_entry_id) REFERENCES word_entries(id) ON DELETE CASCADE,
    UNIQUE(word_entry_id, phrase_type, number, preposition, italian)
);

CREATE INDEX IF NOT EXISTS idx_noun_phrases_entry ON noun_phrases(word_entry_id);
CREATE INDEX IF NOT EXISTS idx_noun_phrases_type ON noun_phrases(phrase_type);

CREATE TABLE IF NOT EXISTS card_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_id INTEGER,
    natural_key TEXT,
    deck TEXT NOT NULL,
    front_text TEXT NOT NULL,
    front_labels TEXT,
    back_highlight TEXT NOT NULL,
    back_text TEXT,
    audio_text TEXT,
    image_text TEXT
);

CREATE INDEX IF NOT EXISTS idx_card_items_deck ON card_items(deck);
CREATE INDEX IF NOT EXISTS idx_card_items_source ON card_items(source_type, source_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_card_items_natural_key ON card_items(natural_key) WHERE natural_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS anki_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_item_id INTEGER NOT NULL,
    direction TEXT NOT NULL DEFAULT 'en_to_it',
    deck TEXT NOT NULL,
    front_text TEXT NOT NULL,
    front_labels TEXT,
    back_highlight TEXT NOT NULL,
    back_text TEXT,
    audio_text TEXT,
    image_text TEXT,
    guid TEXT NOT NULL UNIQUE,
    FOREIGN KEY (card_item_id) REFERENCES card_items(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_anki_cards_deck ON anki_cards(deck);
CREATE INDEX IF NOT EXISTS idx_anki_cards_direction ON anki_cards(direction);
"""


def _detect_encoding(path: Path) -> str:
    """Return 'utf-8' if the file is valid UTF-8, otherwise 'cp1252'."""
    try:
        path.read_bytes().decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "cp1252"


def hash_text(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def parse_value(column: str, value: str) -> int | float | str | None:
    if value == "":
        return None
    if column in INTEGER_COLUMNS:
        return int(value)
    if column in REAL_COLUMNS:
        return float(value.replace(",", "."))
    return value


def normalize_word(wordform: str) -> str:
    return wordform.strip().lower()


def create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_SQL)


def input_word_row(row: dict[str, str], frequency_rank: int) -> tuple[object, ...]:
    return (
        hash_text(row["wordform"]),
        parse_value("id", row["id"]),
        row["wordform"],
        normalize_word(row["wordform"]),
        frequency_rank,
        parse_value("freq_count", row["freq_count"]),
        parse_value("zipf", row["zipf"]),
        parse_value("cd_count", row["cd_count"]),
        row["dom_pos"] or None,
        row["dom_lemma"] or None,
    )


def trim_input_words(connection: sqlite3.Connection, limit: int) -> int:
    before_count = connection.execute("SELECT COUNT(*) FROM input_words").fetchone()[0]
    connection.execute(
        """
        DELETE FROM input_words
        WHERE id NOT IN (
            SELECT id
            FROM input_words
            ORDER BY frequency_rank, id
            LIMIT ?
        )
        """,
        (limit,),
    )
    after_count = connection.execute("SELECT COUNT(*) FROM input_words").fetchone()[0]
    return before_count - after_count


def upsert_csv(csv_path: Path, db_path: Path, limit: int) -> tuple[int, int, int]:
    """
    Import CSV into database, checking each row against existing data.
    Returns (inserted_count, updated_count, trimmed_count).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as connection:
        create_schema(connection)

        inserted_count = 0
        updated_count = 0

        with csv_path.open(newline="", encoding=_detect_encoding(csv_path)) as csv_file:
            reader = csv.DictReader(csv_file, delimiter=";")
            if reader.fieldnames is None:
                raise ValueError(f"No header row found in {csv_path}")

            for frequency_rank, row in enumerate(reader, start=1):
                if frequency_rank > limit:
                    break

                wordform = row["wordform"].strip()
                subtlex_id = parse_value("id", row["id"])
                if not wordform or subtlex_id is None:
                    continue

                row_id = hash_text(wordform)
                normalized_word = normalize_word(wordform)
                freq_count = parse_value("freq_count", row["freq_count"])
                zipf = parse_value("zipf", row["zipf"])
                cd_count = parse_value("cd_count", row["cd_count"])
                dom_pos = row["dom_pos"] or None
                dom_lemma = row["dom_lemma"] or None

                # Check if row exists by wordform hash (primary key)
                existing = connection.execute(
                    """
                    SELECT subtlex_id, wordform, normalized_word, frequency_rank,
                           freq_count, zipf, cd_count, dom_pos, dom_lemma
                    FROM input_words
                    WHERE id = ?
                    """,
                    (row_id,),
                ).fetchone()

                if existing:
                    # Row exists; check if anything changed
                    (
                        existing_subtlex_id,
                        existing_wordform,
                        existing_normalized,
                        existing_rank,
                        existing_freq,
                        existing_zipf,
                        existing_cd,
                        existing_pos,
                        existing_lemma,
                    ) = existing

                    if (
                        existing_subtlex_id != subtlex_id
                        or existing_wordform != wordform
                        or existing_normalized != normalized_word
                        or existing_rank != frequency_rank
                        or existing_freq != freq_count
                        or existing_zipf != zipf
                        or existing_cd != cd_count
                        or existing_pos != dom_pos
                        or existing_lemma != dom_lemma
                    ):
                        # Data differs; update
                        connection.execute(
                            """
                            UPDATE input_words
                            SET subtlex_id = ?,
                                wordform = ?,
                                normalized_word = ?,
                                frequency_rank = ?,
                                freq_count = ?,
                                zipf = ?,
                                cd_count = ?,
                                dom_pos = ?,
                                dom_lemma = ?
                            WHERE id = ?
                            """,
                            (
                                subtlex_id,
                                wordform,
                                normalized_word,
                                frequency_rank,
                                freq_count,
                                zipf,
                                cd_count,
                                dom_pos,
                                dom_lemma,
                                row_id,
                            ),
                        )
                        updated_count += 1
                else:
                    # Row doesn't exist; insert
                    connection.execute(
                        """
                        INSERT INTO input_words (
                            id,
                            subtlex_id,
                            wordform,
                            normalized_word,
                            frequency_rank,
                            freq_count,
                            zipf,
                            cd_count,
                            dom_pos,
                            dom_lemma
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            row_id,
                            subtlex_id,
                            wordform,
                            normalized_word,
                            frequency_rank,
                            freq_count,
                            zipf,
                            cd_count,
                            dom_pos,
                            dom_lemma,
                        ),
                    )
                    inserted_count += 1

        trimmed_count = trim_input_words(connection, limit)
        connection.commit()

        return inserted_count, updated_count, trimmed_count


def print_banner() -> None:
    title = "01 Import SUBTLEX-IT input words"
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


def main() -> None:
    print_banner()
    parser = argparse.ArgumentParser(
        description="Create the MVP SQLite schema and import SUBTLEX-IT input words."
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=DEFAULT_CSV_PATH,
        help=f"Path to the source CSV file. Defaults to {DEFAULT_CSV_PATH}.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Path to the output SQLite database. Defaults to {DEFAULT_DB_PATH}.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Maximum frequency-ranked input words to retain. Defaults to {DEFAULT_LIMIT}.",
    )
    args = parser.parse_args()

    inserted_count, updated_count, trimmed_count = upsert_csv(
        args.csv, args.db, args.limit
    )
    print(
        f"Inserted {inserted_count} rows, updated {updated_count} rows into {args.db}:input_words; "
        f"trimmed {trimmed_count} rows beyond the first {args.limit}."
    )


if __name__ == "__main__":
    main()
