#!/usr/bin/env python3
"""Import curated word lists from inputs/ directly into word_entries via AI enrichment.

Reads italian_interjections.csv, italian_pronouns.csv, and italian_conjunctions.csv
from the inputs/ folder. The Italian text from the CSV is stored as-is for the lemma
(original capitalisation and punctuation preserved, e.g. "Mamma mia!", "né... né...").
The AI only provides the English gloss, an optional usage note, and a confidence score.

Because these words do not come from SUBTLEX-IT, a synthetic input_words row is
created for each one (using a negative subtlex_id drawn from a deterministic hash of
the wordform) so the NOT NULL foreign-key constraint on word_entries.input_word_id is
satisfied without modifying the schema.

word_entries.id is derived as md5(word_type + ":" + wordform) so it never collides
with existing noun/verb entries that share the same lowercase lemma.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from common import DEFAULT_DB_PATH, load_api_key, lemma_id as _lemma_id

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INPUTS_DIR = PROJECT_ROOT / "inputs"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "~google/gemini-flash-latest"
BATCH_SIZE = 30
MAX_RETRIES = 3
RETRY_DELAY = 5

# Maps CSV path → (word_type, dom_pos code used in input_words)
INPUT_FILES: list[tuple[Path, str, str]] = [
    (INPUTS_DIR / "italian_interjections.csv", "interjection", "INT"),
    (INPUTS_DIR / "italian_pronouns.csv",       "pronoun",      "PRO"),
    (INPUTS_DIR / "italian_conjunctions.csv",   "conjunction",  "CON"),
]

# Word types managed by this script — used to identify legacy rows to clean up.
MANAGED_WORD_TYPES = {"interjection", "pronoun", "conjunction"}


# ---------------------------------------------------------------------------
# ID helpers
# ---------------------------------------------------------------------------

def typed_entry_id(word_type: str, wordform: str) -> str:
    """Return a word_entries.id unique per (word_type, wordform).

    Uses a type-prefixed MD5 so these entries never collide with noun/verb rows
    whose lemma_id is md5(lowercased_lemma) — e.g. 'addio' (noun) vs
    'Addio!' (interjection) both hash differently here.
    """
    return _lemma_id(f"{word_type}:{wordform}")


def input_words_row_id(wordform: str) -> str:
    """Stable input_words.id for a synthetic CSV entry.

    Uses md5 of the raw wordform (not lowercased) so 'Addio!' and 'addio'
    get distinct rows, avoiding collisions with real SUBTLEX entries whose ids
    are md5(lowercased_wordform).
    """
    return hashlib.md5(f"input:{wordform}".encode("utf-8")).hexdigest()


def synthetic_subtlex_id(wordform: str) -> int:
    """Return a stable negative integer for use as a synthetic subtlex_id."""
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
                    "english": {
                        "type": "string",
                        "description": (
                            "Concise English gloss suitable for a flashcard back face. "
                            "For interjections keep natural punctuation, e.g. 'Oh my goodness!'. "
                            "For pronouns/conjunctions a short definition, e.g. 'although / even though'. "
                            "Do NOT include register notes like (formal) here — put those in usage_note."
                        ),
                    },
                    "usage_note": {
                        "type": "string",
                        "description": (
                            "Very short register/dialect label if relevant, e.g. 'vulgar', "
                            "'formal', 'Sicilian', 'literary', 'Roman slang', 'phone greeting'. "
                            "Empty string if nothing noteworthy."
                        ),
                    },
                    "confidence": {
                        "type": "number",
                        "description": "Confidence 0.0–1.0 that this is a genuine Italian item of the stated type.",
                    },
                    "valid": {
                        "type": "boolean",
                        "description": "False only if the entry is clearly erroneous.",
                    },
                },
                "required": ["row_index", "english", "usage_note", "confidence", "valid"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InputEntry:
    row_index: int      # globally unique index across all CSV files
    wordform: str       # raw Italian text from CSV — used as the lemma verbatim
    english_hint: str   # raw English text from CSV — passed to AI as a hint
    word_type: str      # 'interjection' | 'pronoun' | 'conjunction'
    dom_pos: str        # 'INT' | 'PRO' | 'CON'


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def cleanup_legacy_entries(connection: sqlite3.Connection) -> None:
    """Delete word_entries and input_words rows from previous broken runs.

    Legacy runs stored lowercased, punctuation-stripped lemmas (e.g. 'a domani'
    instead of 'A domani!') with IDs derived from md5(lowercased_lemma), which
    could collide with real noun/verb entries. We identify them by:
      - word_type IN managed types, AND
      - input_words.subtlex_id < 0 (synthetic row created by this script), AND
      - word_entries.id != typed_entry_id(word_type, wordform)
        (i.e. the ID does not match the correct scheme)

    Rows with the correct ID scheme are left untouched, so this is safe to run
    every time.
    """
    # Collect all synthetic input_words rows (subtlex_id < 0) that back a
    # managed word_entry whose id was computed the old way.
    legacy = connection.execute(
        """
        SELECT we.id AS we_id, iw.id AS iw_id, we.word_type, iw.wordform
        FROM word_entries we
        JOIN input_words iw ON iw.id = we.input_word_id
        WHERE iw.subtlex_id < 0
          AND we.word_type IN ('interjection', 'pronoun', 'conjunction')
        """
    ).fetchall()

    stale_we_ids = []
    stale_iw_ids = []
    for row in legacy:
        expected_id = typed_entry_id(row["word_type"], row["wordform"])
        if row["we_id"] != expected_id:
            stale_we_ids.append(row["we_id"])
            stale_iw_ids.append(row["iw_id"])

    if not stale_we_ids:
        print("  No legacy entries to clean up.")
        return

    print(f"  Cleaning up {len(stale_we_ids)} legacy word_entries and their input_words stubs...")
    for we_id in stale_we_ids:
        connection.execute("DELETE FROM word_entries WHERE id = ?", (we_id,))
    # Only delete input_words stubs that are now fully orphaned.
    for iw_id in stale_iw_ids:
        still_used = connection.execute(
            "SELECT 1 FROM word_entries WHERE input_word_id = ? LIMIT 1", (iw_id,)
        ).fetchone()
        if not still_used:
            connection.execute("DELETE FROM input_words WHERE id = ?", (iw_id,))
    connection.commit()
    print(f"  Done cleaning up legacy entries.")


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_input_entries() -> list[InputEntry]:
    """Read all CSV files and return a flat list with globally unique row_index values."""
    entries: list[InputEntry] = []
    global_index = 0
    for csv_path, word_type, dom_pos in INPUT_FILES:
        if not csv_path.exists():
            print(f"  Warning: {csv_path} not found, skipping.")
            continue
        file_count = 0
        with csv_path.open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                italian = row.get("italian", "").strip()
                english = row.get("english", "").strip()
                if not italian:
                    continue
                entries.append(InputEntry(
                    row_index=global_index,
                    wordform=italian,
                    english_hint=english,
                    word_type=word_type,
                    dom_pos=dom_pos,
                ))
                global_index += 1
                file_count += 1
        print(f"  Loaded {file_count} entries from {csv_path.name}")
    return entries


def already_imported_wordforms(connection: sqlite3.Connection) -> set[str]:
    """Return wordforms that already have a word_entries row.

    A wordform is considered done if any word_entries row exists for that
    (word_type, lemma) pair — regardless of whether the row ID was generated
    by the current typed_entry_id scheme or an older scheme.  This prevents
    re-calling the AI for entries that are already present in the database
    under a legacy ID.

    Legacy-ID rows are NOT re-imported; insert_word_entry would silently skip
    them anyway due to the UNIQUE (word_type, lemma) constraint.
    """
    rows = connection.execute(
        """
        SELECT lemma
        FROM word_entries
        WHERE word_type IN ('interjection', 'pronoun', 'conjunction')
        """
    ).fetchall()
    return {row["lemma"] for row in rows}


# ---------------------------------------------------------------------------
# Database writes
# ---------------------------------------------------------------------------

def ensure_input_words_row(connection: sqlite3.Connection, entry: InputEntry) -> str:
    """Insert a synthetic input_words stub if needed; return its id."""
    row_id = input_words_row_id(entry.wordform)
    if connection.execute(
        "SELECT 1 FROM input_words WHERE id = ? LIMIT 1", (row_id,)
    ).fetchone():
        return row_id

    subtlex_id = synthetic_subtlex_id(entry.wordform)
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
            entry.wordform,
            entry.wordform.lower(),
            entry.dom_pos,
            entry.wordform,
        ),
    )
    return row_id


def insert_word_entry(
    connection: sqlite3.Connection,
    entry: InputEntry,
    item: dict,
) -> bool:
    """Insert one word_entries row. Returns True if inserted, False if skipped.

    The input_words stub is only written once we know we will actually insert,
    so repeated runs leave the database unchanged when all entries are present.
    """
    if not item.get("valid", True):
        return False

    english = item["english"].strip()
    if not english:
        return False

    usage_note = item.get("usage_note", "").strip()
    if usage_note:
        english = f"{english} ({usage_note})"

    confidence = float(item.get("confidence", 1.0))
    word_entry_id = typed_entry_id(entry.word_type, entry.wordform)

    # Check for an existing correct row before touching input_words.
    if connection.execute(
        "SELECT 1 FROM word_entries WHERE id = ? LIMIT 1", (word_entry_id,)
    ).fetchone():
        return False

    input_word_id = ensure_input_words_row(connection, entry)

    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO word_entries (
            id, input_word_id, word_type, lemma, english, confidence
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (word_entry_id, input_word_id, entry.word_type, entry.wordform, english, confidence),
    )
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# AI
# ---------------------------------------------------------------------------

def build_prompt(batch: list[InputEntry]) -> str:
    lines = [json.dumps({
        "row_index": e.row_index,
        "word_type": e.word_type,
        "italian": e.wordform,
        "english_hint": e.english_hint,
    }, ensure_ascii=False) for e in batch]

    return (
        "You are enriching Italian flashcard entries for an Anki deck. "
        "The entries come from curated CSV files of interjections, pronouns, and conjunctions.\n\n"
        "Word type guidance:\n"
        "  interjection — exclamations, greetings, social phrases, animal sounds, fillers.\n"
        "  pronoun — relative, interrogative, indefinite, demonstrative, quantifier pronouns.\n"
        "  conjunction — coordinating, subordinating, correlative conjunctions and connective phrases.\n\n"
        "For each item:\n"
        "  - Set english to a concise natural gloss suitable for a flashcard back face.\n"
        "  - Set usage_note to a very short register/dialect label if relevant "
        "(e.g. 'vulgar', 'formal', 'Sicilian', 'literary', 'Roman slang', 'phone greeting'). "
        "Empty string otherwise.\n"
        "  - Set confidence to how confident you are this is a genuine Italian item of the stated type.\n"
        "  - Set valid=false only if the entry is clearly erroneous.\n\n"
        "Return exactly one output item per input item with the same row_index.\n\n"
        "Items to process, one JSON object per line:\n"
        + "\n".join(lines)
    )


def openrouter_structured_request(prompt: str, api_key: str) -> dict:
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "input_word_entries",
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


def analyze_batch(batch: list[InputEntry], api_key: str) -> list[dict]:
    prompt = build_prompt(batch)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return openrouter_structured_request(prompt, api_key)["items"]
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            print(f"  [attempt {attempt}/{MAX_RETRIES}] Request error: {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)
        except (json.JSONDecodeError, KeyError) as exc:
            print(f"  [attempt {attempt}/{MAX_RETRIES}] Parse error: {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    raise RuntimeError("OpenRouter request failed after all retries")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def print_banner() -> None:
    title = "2 Import input word lists into word_entries"
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


def main() -> None:
    print_banner()
    parser = argparse.ArgumentParser(
        description="Import curated CSV word lists (interjections, pronouns, conjunctions) into word_entries."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print entries that would be processed without calling the API or writing to the DB.",
    )
    args = parser.parse_args()

    entries = load_input_entries()
    if not entries:
        print("No input entries found. Exiting.")
        return

    with sqlite3.connect(args.db, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")

        if not args.dry_run:
            cleanup_legacy_entries(connection)

        already_done = already_imported_wordforms(connection)
        pending = [e for e in entries if e.wordform not in already_done]

        print(
            f"Total entries: {len(entries)} | Already imported: {len(already_done)} | "
            f"Pending: {len(pending)}"
        )

        if not pending:
            print("Nothing to do. Exiting.")
            return

        if args.dry_run:
            for e in pending:
                print(f"  [{e.word_type}] {e.wordform!r} → {e.english_hint!r}")
            return

        api_key = load_api_key()
        inserted_total = 0
        skipped_total = 0
        total_batches = (len(pending) + args.batch_size - 1) // args.batch_size

        for batch_number, start in enumerate(range(0, len(pending), args.batch_size), 1):
            batch = pending[start: start + args.batch_size]
            print(
                f"Batch {batch_number}/{total_batches}: enriching {len(batch)} entries "
                f"({batch[0].word_type} … {batch[-1].word_type})...",
                flush=True,
            )

            items = analyze_batch(batch, api_key)
            item_by_index = {item["row_index"]: item for item in items}

            batch_inserted = 0
            batch_skipped = 0
            for entry in batch:
                item = item_by_index.get(entry.row_index)
                if item is None:
                    print(f"  Warning: no AI result for row_index={entry.row_index} ({entry.wordform!r})")
                    batch_skipped += 1
                    continue

                if insert_word_entry(connection, entry, item):
                    batch_inserted += 1
                else:
                    batch_skipped += 1

            connection.commit()
            inserted_total += batch_inserted
            skipped_total += batch_skipped
            print(f"  inserted {batch_inserted}, skipped {batch_skipped}")

            if batch_number < total_batches:
                time.sleep(1)

    print(
        f"\nDone. Inserted {inserted_total} new word_entries, "
        f"skipped {skipped_total} (duplicates / invalid)."
    )


if __name__ == "__main__":
    main()
