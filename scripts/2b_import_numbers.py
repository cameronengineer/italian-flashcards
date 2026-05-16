#!/usr/bin/env python3
"""Import italian_numbers.csv directly into word_entries (no AI enrichment needed).

Numbers have a fully authoritative english/italian mapping in the CSV, so this
script inserts them straight into word_entries without calling any LLM.

Each number entry:
  - word_type = 'number'
  - lemma     = Italian text (e.g. 'quarantadue')
  - english   = English text (e.g. '42 / forty-two')
  - confidence = 1.0

A synthetic input_words stub is created for each entry (negative subtlex_id) to
satisfy the NOT NULL foreign-key constraint on word_entries.input_word_id.

word_entries.id is derived as md5('number:' + italian) so it never collides with
noun/verb entries.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import sqlite3
from pathlib import Path

from common import DEFAULT_DB_PATH, lemma_id as _lemma_id

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INPUTS_DIR = PROJECT_ROOT / "inputs"
NUMBERS_CSV = INPUTS_DIR / "italian_numbers.csv"


# ---------------------------------------------------------------------------
# ID helpers
# ---------------------------------------------------------------------------

def typed_entry_id(word_type: str, wordform: str) -> str:
    """Return a word_entries.id unique per (word_type, wordform)."""
    return _lemma_id(f"{word_type}:{wordform}")


def input_words_row_id(wordform: str) -> str:
    """Stable input_words.id for a synthetic number entry."""
    return hashlib.md5(f"input:{wordform}".encode("utf-8")).hexdigest()


def synthetic_subtlex_id(wordform: str) -> int:
    """Return a stable negative integer for use as a synthetic subtlex_id."""
    digest = int(hashlib.md5(f"INPUT:{wordform}".encode("utf-8")).hexdigest(), 16)
    return -(digest % (2**31 - 1)) - 1


# ---------------------------------------------------------------------------
# Database writes
# ---------------------------------------------------------------------------

def ensure_input_words_row(connection: sqlite3.Connection, wordform: str) -> str:
    """Insert a synthetic input_words stub if needed; return its id."""
    row_id = input_words_row_id(wordform)
    if connection.execute(
        "SELECT 1 FROM input_words WHERE id = ? LIMIT 1", (row_id,)
    ).fetchone():
        return row_id

    subtlex_id = synthetic_subtlex_id(wordform)
    while connection.execute(
        "SELECT 1 FROM input_words WHERE subtlex_id = ? LIMIT 1", (subtlex_id,)
    ).fetchone():
        subtlex_id -= 1

    connection.execute(
        """
        INSERT INTO input_words (
            id, subtlex_id, wordform, normalized_word,
            frequency_rank, freq_count, zipf, cd_count,
            dom_pos, dom_lemma
        ) VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?)
        """,
        (
            row_id,
            subtlex_id,
            wordform,
            wordform.lower(),
            "NUM",
            wordform,
        ),
    )
    return row_id


def insert_word_entry(
    connection: sqlite3.Connection,
    italian: str,
    english: str,
) -> bool:
    """Insert one word_entries row for a number. Returns True if inserted."""
    word_entry_id = typed_entry_id("number", italian)

    if connection.execute(
        "SELECT 1 FROM word_entries WHERE id = ? LIMIT 1", (word_entry_id,)
    ).fetchone():
        return False

    input_word_id = ensure_input_words_row(connection, italian)

    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO word_entries (
            id, input_word_id, word_type, lemma, english, confidence
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (word_entry_id, input_word_id, "number", italian, english, 1.0),
    )
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_numbers() -> list[tuple[str, str]]:
    """Read italian_numbers.csv; return list of (english, italian) pairs."""
    entries: list[tuple[str, str]] = []
    with NUMBERS_CSV.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            italian = row.get("italian", "").strip()
            english = row.get("english", "").strip()
            if italian and english:
                entries.append((english, italian))
    return entries


def already_imported(connection: sqlite3.Connection) -> set[str]:
    """Return Italian lemmas already present in word_entries for word_type='number'."""
    rows = connection.execute(
        "SELECT lemma FROM word_entries WHERE word_type = 'number'"
    ).fetchall()
    return {row["lemma"] for row in rows}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def print_banner() -> None:
    title = "2b Import numbers into word_entries"
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


def main() -> None:
    print_banner()
    parser = argparse.ArgumentParser(
        description="Import italian_numbers.csv into word_entries (no AI needed)."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args()

    if not NUMBERS_CSV.exists():
        print(f"Error: {NUMBERS_CSV} not found.")
        return

    entries = load_numbers()
    print(f"Loaded {len(entries)} entries from {NUMBERS_CSV.name}")

    with sqlite3.connect(args.db, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")

        done = already_imported(connection)
        pending = [(en, it) for en, it in entries if it not in done]

        print(
            f"Total: {len(entries)} | Already imported: {len(done)} | Pending: {len(pending)}"
        )

        if not pending:
            print("Nothing to do. Exiting.")
            return

        inserted = 0
        for english, italian in pending:
            if insert_word_entry(connection, italian, english):
                inserted += 1

        connection.commit()

    print(f"Inserted {inserted} number word_entries.")


if __name__ == "__main__":
    main()
