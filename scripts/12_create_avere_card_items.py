#!/usr/bin/env python3
"""Create card_items for avere_expression entries.

Each avere_expression (e.g. "avere fame") produces two card_items, one for each of two
deterministically-chosen persons from the six present-tense options:
  io, tu, lui_lei, noi, voi, loro

The two persons are chosen by hashing the expression's word_entries.id so the selection
is stable across regeneration runs.  The same hashing approach is used in
10_sort_anki_cards.py for distributing cards.

Present tense conjugation of avere:
  io     → ho
  tu     → hai
  lui_lei → ha
  noi    → abbiamo
  voi    → avete
  loro   → hanno

The card face layout mirrors conjunctions:
  front_text    = English meaning (e.g. "to be hungry")
  back_highlight = Italian conjugated expression (e.g. "ho fame")
  front_labels  = "type: avere expression | subject: io"
  audio_text    = Italian conjugated form
  image_text    = None (same as conjunctions)

Deck: "Italian - Espressioni con Avere"
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "database.sqlite"

DECK = "Italian - Espressioni con Avere"

# All six present-tense persons in a stable order used for index selection.
PERSONS: list[str] = ["io", "tu", "lui_lei", "noi", "voi", "loro"]

# avere present-tense conjugations keyed by person
AVERE: dict[str, str] = {
    "io":      "ho",
    "tu":      "hai",
    "lui_lei": "ha",
    "noi":     "abbiamo",
    "voi":     "avete",
    "loro":    "hanno",
}

# Natural subject labels for front pills
SUBJECT_LABEL: dict[str, str] = {
    "io":      "io",
    "tu":      "tu",
    "lui_lei": "lui / lei",
    "noi":     "noi",
    "voi":     "voi",
    "loro":    "loro",
}

# English subject context for natural prompts
SUBJECT_ENGLISH: dict[str, str] = {
    "io":      "I",
    "tu":      "you",
    "lui_lei": "he / she",
    "noi":     "we",
    "voi":     "you all",
    "loro":    "they",
}

NUM_CARDS_PER_EXPRESSION = 2


def pick_persons(word_entry_id: str) -> list[str]:
    """Deterministically pick NUM_CARDS_PER_EXPRESSION persons for this expression.

    Uses successive bytes of the MD5 digest to choose indices into PERSONS without
    replacement.  This is stable: the same word_entry_id always yields the same pair.
    """
    digest = hashlib.md5(word_entry_id.encode("utf-8")).digest()
    chosen: list[str] = []
    used: set[int] = set()
    for byte in digest:
        idx = byte % len(PERSONS)
        if idx not in used:
            chosen.append(PERSONS[idx])
            used.add(idx)
        if len(chosen) == NUM_CARDS_PER_EXPRESSION:
            break
    # Fallback (should never be needed): fill from front if digest bytes exhausted
    if len(chosen) < NUM_CARDS_PER_EXPRESSION:
        for i, person in enumerate(PERSONS):
            if i not in used:
                chosen.append(person)
                used.add(i)
            if len(chosen) == NUM_CARDS_PER_EXPRESSION:
                break
    return chosen


def build_italian(person: str, expression: str) -> str:
    """Return the conjugated Italian phrase, e.g. 'ho fame', 'abbiamo bisogno di'.

    Handles:
      'avere fame'      → 'ho fame'
      'non avere senso' → 'non ho senso'
      'avere X anni'    → 'ho X anni'
    """
    conjugated = AVERE[person]
    expr = expression.strip()
    lower = expr.lower()

    if lower.startswith("non avere "):
        noun_part = expr[len("non avere "):]
        return f"non {conjugated} {noun_part}"
    elif lower.startswith("avere "):
        noun_part = expr[len("avere "):]
        return f"{conjugated} {noun_part}"
    else:
        # Unexpected format — use as-is (shouldn't happen)
        return f"{conjugated} {expr}"


def _conjugate_phrase(person: str, verb_phrase: str) -> str:
    """Conjugate a bare verb phrase (no leading 'to ') for the given person.

    Handles:
      'be hungry'           → 'am hungry' / 'is hungry' / 'are hungry'
      'not be X'            → 'am not X' / 'is not X' / 'are not X'
      'not matter'          → "don't matter" / "doesn't matter"
      'have a fever'        → 'have a fever' / 'has a fever'
      'need'                → 'need' / 'needs'
      'feel like'           → 'feel like' / 'feels like'
      'be in a hurry'       → 'am in a hurry' / ...
    """
    phrase = verb_phrase.strip()

    if phrase.startswith("be ") or phrase == "be":
        rest = phrase[3:] if phrase.startswith("be ") else ""
        if person == "io":
            return f"am {rest}".strip()
        elif person == "lui_lei":
            return f"is {rest}".strip()
        else:
            return f"are {rest}".strip()

    if phrase.startswith("not be ") or phrase == "not be":
        rest = phrase[7:] if phrase.startswith("not be ") else ""
        if person == "io":
            return f"am not {rest}".strip()
        elif person == "lui_lei":
            return f"is not {rest}".strip()
        else:
            return f"are not {rest}".strip()

    if phrase.startswith("not "):
        rest = phrase[4:]
        if person == "lui_lei":
            return f"doesn't {rest}"
        else:
            return f"don't {rest}"

    # Normal verb (possibly multi-word like "have a fever", "feel like", "take care of")
    # Extract the first word to conjugate, preserve the rest.
    words = phrase.split(" ", 1)
    first_word = words[0]
    remainder = (" " + words[1]) if len(words) > 1 else ""

    if person == "lui_lei":
        if first_word == "have":
            conjugated = "has"
        elif first_word.endswith("s") or first_word.endswith("x") or first_word.endswith("z"):
            conjugated = first_word + "es"
        elif (
            first_word.endswith("y")
            and len(first_word) > 1
            and first_word[-2] not in "aeiou"
        ):
            conjugated = first_word[:-1] + "ies"
        else:
            conjugated = first_word + "s"
    else:
        conjugated = first_word

    return conjugated + remainder


def build_english_prompt(person: str, base_english: str) -> str:
    """Build a natural English flashcard prompt, e.g. 'I am hungry'.

    The base_english from the CSV is something like 'to be hungry'.  We convert
    the infinitive to a personal form and handle ' / ' alternatives.
    """
    subject = SUBJECT_ENGLISH[person]
    eng = base_english.strip()

    # Strip leading 'to ' and conjugate each alternative
    if eng.lower().startswith("to "):
        verb_phrase = eng[3:]
    else:
        # Fallback: prefix subject
        return f"{subject}: {eng}"

    # Split on ' / ' alternatives; each part may itself start with 'to '
    alternatives = [p.strip() for p in verb_phrase.split(" / ")]
    conjugated_parts = []
    for alt in alternatives:
        # Each alternative may carry a redundant 'to ' prefix from the CSV
        if alt.lower().startswith("to "):
            alt = alt[3:]
        conjugated_parts.append(_conjugate_phrase(person, alt))

    result = " / ".join(conjugated_parts)
    return f"{subject} {result}"


def load_avere_entries(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return all avere_expression word_entries."""
    return connection.execute(
        """
        SELECT id, lemma, english
        FROM word_entries
        WHERE word_type = 'avere_expression'
        ORDER BY lemma
        """
    ).fetchall()


def create_avere_card_items(connection: sqlite3.Connection) -> int:
    """Insert card_items for all avere_expression entries that don't already have them.

    Returns number of rows inserted.
    """
    entries = load_avere_entries(connection)
    inserted = 0

    for row in entries:
        word_entry_id = row["id"]
        expression = row["lemma"]       # e.g. "avere fame"
        base_english = row["english"]   # e.g. "to be hungry"

        persons = pick_persons(word_entry_id)

        for person in persons:
            natural_key = f"avere_expression:{word_entry_id}:{person}"

            # Skip if already exists
            existing = connection.execute(
                "SELECT 1 FROM card_items WHERE natural_key = ? LIMIT 1",
                (natural_key,),
            ).fetchone()
            if existing:
                continue

            italian = build_italian(person, expression)
            english_prompt = build_english_prompt(person, base_english)
            subject_lbl = SUBJECT_LABEL[person]
            front_labels = f"type: avere expression | subject: {subject_lbl}"

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
                    "input_word",
                    # source_id is the word_entries.id; we store as text but column is INTEGER —
                    # use the same approach as conjunctions (which use we.id directly)
                    # Actually card_items.source_id is INTEGER; for input_word entries
                    # the convention is source_id = word_entries rowid equivalent.
                    # Looking at script 7: it uses we.id (TEXT) cast to source_id (INTEGER).
                    # SQLite stores TEXT in INTEGER column without error; keep consistent.
                    word_entry_id,
                    natural_key,
                    DECK,
                    english_prompt,       # front: English prompt
                    front_labels,
                    italian,              # back_highlight: Italian answer
                    None,                 # back_text: no infinitive needed
                    italian,              # audio_text: speak the Italian
                    None,                 # image_text: no image
                ),
            )
            inserted += cursor.rowcount

    return inserted


def print_banner() -> None:
    title = "12 Create card_items (espressioni con avere)"
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


def main() -> None:
    print_banner()
    parser = argparse.ArgumentParser(
        description="Create card_items for avere_expression entries (2 persons each)."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args()

    with sqlite3.connect(args.db, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")

        total_expressions = connection.execute(
            "SELECT COUNT(*) AS count FROM word_entries WHERE word_type = 'avere_expression'"
        ).fetchone()["count"]

        if total_expressions == 0:
            print("No avere_expression entries found. Run script 2 first.")
            return

        expected_count = total_expressions * NUM_CARDS_PER_EXPRESSION
        existing_count = connection.execute(
            """
            SELECT COUNT(*) AS count FROM card_items
            WHERE source_type = 'input_word'
              AND natural_key LIKE 'avere_expression:%'
            """
        ).fetchone()["count"]

        if existing_count >= expected_count:
            print(
                f"Already have {existing_count} avere_expression card_items "
                f"(expected: {expected_count}). Exiting."
            )
            return

        inserted = create_avere_card_items(connection)
        connection.commit()

    print(f"Inserted {inserted} avere_expression card_items into '{DECK}'.")


if __name__ == "__main__":
    main()
