#!/usr/bin/env python3
"""Import italki.csv into word_entries using AI enrichment.

Reads inputs/italki/italki.csv (columns: italian, english).
This file contains vocabulary from italki conversation classes: nouns,
expressions, phrases, multi-word items, and some verbs.

Verbs that should receive full conjugation treatment are listed separately
in italki_verbs.csv and handled by 04_italki_import_verbs.py.  This script
imports everything else as word_type='italki_expression'.

The AI is asked to:
  - Provide a clean English gloss (the CSV hint may be rough).
  - Classify the part of speech (noun, expression, phrase, adjective, adverb,
    verb — though verbs here are kept as expressions, not conjugated).
  - Flag items that are already sentences or conjugated forms (imported as-is).

Downstream, 13_italki_create_card_items.py creates card_items for these
entries in the "Italian - Italki" deck.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from common import DEFAULT_DB_PATH, load_api_key, lemma_id as _lemma_id

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ITALKI_CSV = PROJECT_ROOT / "inputs" / "italki" / "italki.csv"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "~google/gemini-flash-latest"
MAX_RETRIES = 3
RETRY_DELAY = 5
WORD_TYPE = "italki_expression"


# ---------------------------------------------------------------------------
# ID helpers
# ---------------------------------------------------------------------------

def typed_entry_id(wordform: str) -> str:
    """Stable word_entries.id for an italki_expression entry."""
    return _lemma_id(f"{WORD_TYPE}:{wordform}")


def input_words_row_id(wordform: str) -> str:
    return hashlib.md5(f"input:{wordform}".encode("utf-8")).hexdigest()


def synthetic_subtlex_id(wordform: str) -> int:
    digest = int(hashlib.md5(f"INPUT:{wordform}".encode("utf-8")).hexdigest(), 16)
    return -(digest % (2**31 - 1)) - 1


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "row_index": {
                        "type": "integer",
                        "description": "The row_index value exactly as provided.",
                    },
                    "valid": {
                        "type": "boolean",
                        "description": "false only if the entry is clearly erroneous or not Italian.",
                    },
                    "english": {
                        "type": "string",
                        "description": (
                            "Clean, concise English gloss for the flashcard back face. "
                            "For nouns: 'the cat', 'a dog'. "
                            "For verbs/infinitives: start with 'to'. "
                            "For phrases and expressions: natural English equivalent. "
                            "For sentences: natural English translation."
                        ),
                    },
                    "part_of_speech": {
                        "type": "string",
                        "enum": [
                            "noun", "verb", "adjective", "adverb",
                            "phrase", "sentence", "other"
                        ],
                        "description": "Best-fit part of speech or category for this item.",
                    },
                    "disambiguation": {
                        "type": "string",
                        "description": (
                            "Short parenthetical clarifier when the Italian could be confused "
                            "with another common word. Empty string if not needed."
                        ),
                    },
                    "usage_note": {
                        "type": "string",
                        "description": (
                            "Very short register label if relevant: 'archaic', 'formal', "
                            "'vulgar', 'literary', 'regional', 'colloquial'. "
                            "Empty string for ordinary modern usage."
                        ),
                    },
                    "confidence": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                    },
                },
                "required": [
                    "row_index", "valid", "english", "part_of_speech",
                    "disambiguation", "usage_note", "confidence",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ItaltkiExpression:
    row_index: int
    wordform: str       # raw Italian text from CSV — stored as lemma verbatim
    english_hint: str   # raw English from CSV — passed to AI as a hint


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_entries() -> list[ItaltkiExpression]:
    if not ITALKI_CSV.exists():
        raise FileNotFoundError(f"CSV not found: {ITALKI_CSV}")
    entries: list[ItaltkiExpression] = []
    with ITALKI_CSV.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for idx, row in enumerate(reader):
            italian = row.get("italian", "").strip()
            english = row.get("english", "").strip()
            if not italian:
                continue
            entries.append(ItaltkiExpression(
                row_index=idx,
                wordform=italian,
                english_hint=english,
            ))
    print(f"  Loaded {len(entries)} entries from {ITALKI_CSV.name}")
    return entries


def already_imported(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT lemma FROM word_entries WHERE word_type = ?", (WORD_TYPE,)
    ).fetchall()
    return {row["lemma"] for row in rows}


# ---------------------------------------------------------------------------
# Database writes
# ---------------------------------------------------------------------------

def ensure_input_words_row(connection: sqlite3.Connection, wordform: str) -> str:
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
        (row_id, subtlex_id, wordform, wordform.lower(), "ITE", wordform),
    )
    return row_id


def insert_word_entry(
    connection: sqlite3.Connection,
    entry: ItaltkiExpression,
    item: dict,
) -> bool:
    """Insert one word_entries row. Returns True if inserted, False if skipped."""
    if not item.get("valid", True):
        return False

    english = item["english"].strip()
    if not english:
        return False

    disambiguation = item.get("disambiguation", "").strip()
    if disambiguation:
        english = f"{english} ({disambiguation})"

    usage_note = item.get("usage_note", "").strip()
    if usage_note:
        english = f"{english} [{usage_note}]"

    word_entry_id = typed_entry_id(entry.wordform)

    if connection.execute(
        "SELECT 1 FROM word_entries WHERE id = ? LIMIT 1", (word_entry_id,)
    ).fetchone():
        return False

    input_word_id = ensure_input_words_row(connection, entry.wordform)

    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO word_entries (
            id, input_word_id, word_type, lemma, english, confidence
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            word_entry_id,
            input_word_id,
            WORD_TYPE,
            entry.wordform,
            english,
            float(item.get("confidence", 1.0)),
        ),
    )
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# AI
# ---------------------------------------------------------------------------

def build_prompt(entry: ItaltkiExpression) -> str:
    item = json.dumps({
        "row_index": entry.row_index,
        "italian": entry.wordform,
        "english_hint": entry.english_hint,
    }, ensure_ascii=False)

    return (
        "You are enriching an Italian vocabulary entry for an Anki flashcard database. "
        "The entry comes from vocabulary recorded during italki conversation classes. "
        "Items may be single words, multi-word phrases, expressions, or short sentences.\n\n"
        "For the item:\n"
        "  - Set english to a clean, concise flashcard-quality gloss. "
        "Use the english_hint as a starting point but improve it if needed.\n"
        "  - Set part_of_speech to the best-fit category.\n"
        "  - Set disambiguation to a short parenthetical only when the Italian could be "
        "confused with another common word. Empty string otherwise.\n"
        "  - Set usage_note to a very short label only if notably archaic, formal, vulgar, "
        "literary, regional, or colloquial. Empty string for ordinary usage.\n"
        "  - Set valid=false only if the entry is clearly erroneous or not Italian.\n"
        "  - Return exactly one item with the same row_index.\n\n"
        f"Item to process:\n{item}"
    )


def openrouter_structured_request(prompt: str, api_key: str) -> dict:
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "italki_expression_entries",
                "strict": True,
                "schema": RESPONSE_SCHEMA,
            },
        },
    }
    request = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Title": "Italian Flashcards",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        body = response.read().decode("utf-8")
    content = json.loads(body)["choices"][0]["message"]["content"]
    return json.loads(content)


def analyze_one(entry: ItaltkiExpression, api_key: str) -> dict:
    prompt = build_prompt(entry)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            items = openrouter_structured_request(prompt, api_key)["items"]
            if items:
                return items[0]
            raise ValueError("Empty items list in response")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            print(f"  [attempt {attempt}/{MAX_RETRIES}] {entry.wordform!r}: Request error: {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            print(f"  [attempt {attempt}/{MAX_RETRIES}] {entry.wordform!r}: Parse error: {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    raise RuntimeError(f"OpenRouter request failed for {entry.wordform!r}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def print_banner() -> None:
    title = "05 Italki import expressions into word_entries"
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


def main() -> None:
    print_banner()
    parser = argparse.ArgumentParser(
        description="Import italki.csv into word_entries (word_type=italki_expression)."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--workers", type=int, default=5,
                        help="Number of parallel AI request threads (default: 5).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print entries that would be processed without calling the API.")
    args = parser.parse_args()

    entries = load_entries()
    if not entries:
        print("No entries found. Exiting.")
        return

    with sqlite3.connect(args.db, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")

        done = already_imported(connection)
        pending = [e for e in entries if e.wordform not in done]

        print(
            f"Total: {len(entries)} | Already imported: {len(done)} | Pending: {len(pending)}"
        )

        if not pending:
            print("Nothing to do. Exiting.")
            return

        if args.dry_run:
            for e in pending:
                print(f"  {e.wordform!r} → hint: {e.english_hint!r}")
            return

    api_key = load_api_key()
    db_path = args.db
    db_lock = threading.Lock()
    inserted_total = 0
    skipped_total = 0
    total = len(pending)

    def process_one(entry: ItaltkiExpression) -> tuple[int, int]:
        word_entry_id = typed_entry_id(entry.wordform)
        with sqlite3.connect(db_path, timeout=30) as chk:
            already = chk.execute(
                "SELECT 1 FROM word_entries WHERE id = ? LIMIT 1", (word_entry_id,)
            ).fetchone()
        if already:
            return 0, 1
        item = analyze_one(entry, api_key)
        with db_lock:
            with sqlite3.connect(db_path, timeout=30) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys = ON")
                if insert_word_entry(conn, entry, item):
                    conn.commit()
                    return 1, 0
                return 0, 1

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process_one, e): e for e in pending}
        done_count = 0
        for future in as_completed(futures):
            done_count += 1
            exc = future.exception()
            ent = futures[future]
            if exc:
                print(f"  [{done_count}/{total}] {ent.wordform!r}: ERROR: {exc}", flush=True)
            else:
                ins, skp = future.result()
                inserted_total += ins
                skipped_total += skp
                print(
                    f"  [{done_count}/{total}] {ent.wordform!r}: "
                    f"{'inserted' if ins else 'skipped'}",
                    flush=True,
                )

    print(
        f"\nDone. Inserted {inserted_total} new word_entries, "
        f"skipped {skipped_total} (duplicates / invalid)."
    )


if __name__ == "__main__":
    main()
