#!/usr/bin/env python3
"""Create noun word_entries from input_words using OpenRouter structured output."""

from __future__ import annotations

import argparse
import json
import sqlite3
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from common import DEFAULT_DB_PATH, load_api_key, lemma_id

PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_KEY_FILE = PROJECT_ROOT / ".openrouter"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "~google/gemini-flash-latest"
MAX_NOUNS = 1000
MAX_RETRIES = 3
RETRY_DELAY = 5

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "input_word_id": {
                        "type": "string",
                        "description": "The input_words.id value exactly as provided.",
                    },
                    "source_wordform": {
                        "type": "string",
                        "description": "The input surface word exactly as provided.",
                    },
                    "source_lemma": {
                        "type": "string",
                        "description": "The dominant noun lemma from dom_lemma exactly as provided.",
                    },
                    "valid": {
                        "type": "boolean",
                        "description": "True when source_lemma can be resolved to a real Italian noun.",
                    },
                    "lemma": {
                        "type": "string",
                        "description": "Canonical Italian noun lemma, usually singular.",
                    },
                    "english": {
                        "type": "string",
                        "description": "Concise English translation of the noun.",
                    },
                    "disambiguation": {
                        "type": "string",
                        "description": (
                            "Short parenthetical clarifier when the noun could be confused with another. "
                            "Example: 'bank (financial)' vs 'bank (river)' for banca/riva. "
                            "Empty string if not needed."
                        ),
                    },
                    "usage_note": {
                        "type": "string",
                        "description": (
                            "Very short register, style, or dialect label if relevant. "
                            "Use 'archaic' for nouns no longer in common modern use. "
                            "Use 'formal' for nouns restricted to formal or written contexts. "
                            "Other examples: 'vulgar', 'literary', 'regional'. "
                            "Empty string if nothing noteworthy."
                        ),
                    },
                    "confidence": {
                        "type": "number",
                        "description": "Confidence from 0.0 to 1.0.",
                    },
                    "singular": {
                        "type": "string",
                        "description": "Italian singular noun form.",
                    },
                    "singular_english": {
                        "type": "string",
                        "description": "English translation for the singular noun form.",
                    },
                    "plural": {
                        "type": "string",
                        "description": "Italian plural noun form, or empty string if unknown/unusual.",
                    },
                    "plural_english": {
                        "type": "string",
                        "description": "English translation for the plural noun form, or empty string if no plural is supplied.",
                    },
                    "gender": {
                        "type": "string",
                        "enum": ["masculine", "feminine", "both", "unknown"],
                        "description": "Grammatical gender of the noun.",
                    },
                    "definite_singular": {
                        "type": "string",
                        "description": "Singular definite article: il, lo, l', or la.",
                    },
                    "definite_plural": {
                        "type": "string",
                        "description": "Plural definite article: i, gli, or le.",
                    },
                    "indefinite_singular": {
                        "type": "string",
                        "description": "Singular indefinite article: un, uno, una, or un'.",
                    },
                },
                "required": [
                    "input_word_id",
                    "source_wordform",
                    "source_lemma",
                    "valid",
                    "lemma",
                    "english",
                    "disambiguation",
                    "usage_note",
                    "confidence",
                    "singular",
                    "singular_english",
                    "plural",
                    "plural_english",
                    "gender",
                    "definite_singular",
                    "definite_plural",
                    "indefinite_singular",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class NounCandidate:
    input_word_id: str
    frequency_rank: int | None
    wordform: str
    source_lemma: str
    dom_pos: str
    existing_word_entry_id: str | None = None


def is_garbage_lemma(lemma: str) -> bool:
    """Return True if a dom_lemma is clearly not a usable Italian noun.

    Catches:
    - Single letters (e.g. 'c', 'x', 'B', 'M')
    - Lemmas containing non-letter characters: pipe separators (e.g. 'mano|mani'),
      symbols, punctuation, digits
    - Pure ASCII uppercase single tokens that look like abbreviation noise
    """
    if not lemma:
        return True
    # Single character that is just a letter — not a word
    if len(lemma) == 1 and unicodedata.category(lemma[0]) in ("Lu", "Ll"):
        return True
    # Any character that is not a Unicode letter or combining mark (e.g. '|', '#', '<', '>')
    for ch in lemma:
        cat = unicodedata.category(ch)
        if not (cat.startswith("L") or cat.startswith("M")):
            return True
    return False


def load_candidates(connection: sqlite3.Connection, limit: int) -> list[NounCandidate]:
    rows = connection.execute(
        """
        SELECT id, frequency_rank, wordform, dom_pos, dom_lemma
        FROM input_words
        WHERE dom_pos = 'NOM'
          AND dom_lemma IS NOT NULL
          AND dom_lemma != '<unknown>'
        ORDER BY frequency_rank, id
        """
    ).fetchall()

    existing_lemmas = {
        row["lemma"].strip().lower()
        for row in connection.execute(
            """
            SELECT lemma
            FROM word_entries
            """
        ).fetchall()
    }
    existing_lemmas.update(
        row["singular"].strip().lower()
        for row in connection.execute(
            """
            SELECT singular
            FROM word_entries
            WHERE word_type = 'noun' AND singular IS NOT NULL
            """
        ).fetchall()
        if row["singular"]
    )
    existing_input_word_ids = {
        row["input_word_id"]
        for row in connection.execute(
            """
            SELECT input_word_id
            FROM word_entries
            """
        ).fetchall()
    }
    incomplete_entries = {
        row["input_word_id"]: row["id"]
        for row in connection.execute(
            """
            SELECT id, input_word_id
            FROM word_entries
            WHERE word_type = 'noun'
              AND (
                  english IS NULL OR TRIM(english) = '' OR
                  confidence IS NULL OR
                  singular IS NULL OR TRIM(singular) = '' OR
                  singular_english IS NULL OR TRIM(singular_english) = '' OR
                  gender IS NULL OR TRIM(gender) = '' OR gender = 'unknown' OR
                  definite_singular IS NULL OR TRIM(definite_singular) = '' OR
                  indefinite_singular IS NULL OR TRIM(indefinite_singular) = '' OR
                  (plural IS NOT NULL AND TRIM(plural) != '' AND (plural_english IS NULL OR TRIM(plural_english) = '')) OR
                  (plural IS NOT NULL AND TRIM(plural) != '' AND (definite_plural IS NULL OR TRIM(definite_plural) = ''))
              )
            """
        ).fetchall()
    }

    candidates: list[NounCandidate] = []
    seen_lemmas: set[str] = set()
    for row in rows:
        existing_word_entry_id = incomplete_entries.get(row["id"])
        if row["id"] in existing_input_word_ids and existing_word_entry_id is None:
            continue

        source_lemma = row["dom_lemma"].strip()
        if is_garbage_lemma(source_lemma):
            continue

        normalized_lemma = source_lemma.lower()
        if normalized_lemma in seen_lemmas:
            continue
        seen_lemmas.add(normalized_lemma)
        if len(seen_lemmas) > limit:
            return candidates

        if normalized_lemma in existing_lemmas and existing_word_entry_id is None:
            continue

        candidates.append(
            NounCandidate(
                input_word_id=row["id"],
                frequency_rank=row["frequency_rank"],
                wordform=row["wordform"],
                source_lemma=source_lemma,
                dom_pos=row["dom_pos"],
                existing_word_entry_id=existing_word_entry_id,
            )
        )
    return candidates


def build_prompt(candidate: NounCandidate) -> str:
    item = json.dumps(
        {
            "input_word_id": candidate.input_word_id,
            "frequency_rank": candidate.frequency_rank,
            "source_wordform": candidate.wordform,
            "source_lemma": candidate.source_lemma,
            "dom_pos": candidate.dom_pos,
            "existing_word_entry_id": candidate.existing_word_entry_id,
        },
        ensure_ascii=False,
    )

    return (
        "You are creating a noun dictionary entry for an Italian flashcard database. "
        "The item comes from one SUBTLEX-IT input_words row where dom_pos is NOM. "
        "Use the dominant dom_lemma value supplied as source_lemma, not normalized_word and not all_pos_lemma. "
        "Do not create entries for secondary or rare analyses from all_pos_lemma.\n\n"
        "Return exactly one output item with the same input_word_id, source_wordform, "
        "and source_lemma. If source_lemma is a valid Italian noun or proper noun, set valid=true and fill "
        "the canonical noun fields. If it is not a noun, is a data artifact, or cannot be resolved safely, "
        "set valid=false, use empty strings for text fields, gender='unknown', and confidence=0.\n\n"
        "Rules:\n"
        "- lemma and singular should normally be the canonical singular form.\n"
        "- singular_english should translate the singular form, usually without an article, e.g. 'house'.\n"
        "- plural_english should translate the plural form, usually without an article, e.g. 'houses'. Leave empty if plural is empty.\n"
        "- For plural-only nouns, put the normal dictionary form in lemma and singular; use gender='unknown' if unsure.\n"
        "- english should be concise, usually without an article, e.g. 'house'.\n"
        "- disambiguation: add a short parenthetical clarifier when the noun is ambiguous, e.g. banco → 'bench (seat)' vs 'counter (bank)'. Leave empty if not needed.\n"
        "- usage_note: add a very short label if the noun is archaic, formal, vulgar, literary, or regional. Leave empty for ordinary modern nouns.\n"
        "- definite_singular must be one of il, lo, l', la, or empty if unknown/not applicable.\n"
        "- definite_plural must be one of i, gli, le, or empty if unknown/not applicable.\n"
        "- indefinite_singular must be one of un, uno, una, un', or empty if unknown/not applicable.\n"
        "- confidence must be between 0 and 1.\n\n"
        f"Item to process:\n{item}"
    )


def openrouter_structured_request(prompt: str, api_key: str) -> dict:
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "noun_word_entries",
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


def analyze_one(candidate: NounCandidate, api_key: str) -> dict:
    prompt = build_prompt(candidate)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            parsed = openrouter_structured_request(prompt, api_key)
            items = parsed["items"]
            if items:
                return items[0]
            raise ValueError("Empty items list in response")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            print(f"  [attempt {attempt}/{MAX_RETRIES}] {candidate.source_lemma!r}: Request error: {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            print(f"  [attempt {attempt}/{MAX_RETRIES}] {candidate.source_lemma!r}: Parse error: {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    raise RuntimeError(f"OpenRouter request failed for {candidate.source_lemma!r}")


def insert_word_entries(connection: sqlite3.Connection, items: list[dict]) -> int:
    inserted = 0
    for item in items:
        input_word_id = str(item["input_word_id"])
        if not item["valid"]:
            source_lemma = item["source_lemma"].strip().lower()
            if not source_lemma:
                continue

            word_entry_id = lemma_id(source_lemma)
            existing = connection.execute(
                """
                SELECT 1
                FROM word_entries
                WHERE id = ? OR input_word_id = ? OR lemma = ?
                LIMIT 1
                """,
                (word_entry_id, input_word_id, source_lemma),
            ).fetchone()
            if existing:
                continue

            connection.execute(
                """
                INSERT INTO word_entries (
                    id,
                    input_word_id,
                    word_type,
                    lemma,
                    english,
                    confidence
                ) VALUES (?, ?, 'other', ?, '', 0)
                """,
                (word_entry_id, input_word_id, source_lemma),
            )
            inserted += 1
            continue

        lemma = item["lemma"].strip().lower()
        singular = item["singular"].strip().lower() or lemma
        if not lemma or not singular:
            continue

        english = item["english"].strip()
        disambiguation = item.get("disambiguation", "").strip()
        usage_note = item.get("usage_note", "").strip()
        if disambiguation:
            english = f"{english} ({disambiguation})"
        if usage_note:
            english = f"{english} [{usage_note}]"

        word_entry_id = lemma_id(lemma)
        existing_for_input = connection.execute(
            """
            SELECT id
            FROM word_entries
            WHERE input_word_id = ? AND word_type = 'noun'
            LIMIT 1
            """,
            (input_word_id,),
        ).fetchone()
        if existing_for_input:
            connection.execute(
                """
                UPDATE word_entries
                SET id = ?,
                    lemma = ?,
                    english = ?,
                    confidence = ?,
                    singular = ?,
                    singular_english = ?,
                    plural = ?,
                    plural_english = ?,
                    gender = ?,
                    definite_singular = ?,
                    definite_plural = ?,
                    indefinite_singular = ?
                WHERE id = ?
                """,
                (
                    word_entry_id,
                    lemma,
                    english,
                    float(item["confidence"]),
                    singular,
                    item["singular_english"].strip(),
                    item["plural"].strip().lower(),
                    item["plural_english"].strip(),
                    item["gender"],
                    item["definite_singular"].strip().lower(),
                    item["definite_plural"].strip().lower(),
                    item["indefinite_singular"].strip().lower(),
                    existing_for_input["id"],
                ),
            )
            inserted += 1
            continue

        existing = connection.execute(
            """
            SELECT 1
            FROM word_entries
            WHERE id = ? OR input_word_id = ? OR lemma = ? OR (word_type = 'noun' AND singular = ?)
            LIMIT 1
            """,
            (word_entry_id, input_word_id, lemma, singular),
        ).fetchone()
        if existing:
            continue

        connection.execute(
            """
            INSERT INTO word_entries (
                id,
                input_word_id,
                word_type,
                lemma,
                english,
                confidence,
                singular,
                singular_english,
                plural,
                plural_english,
                gender,
                definite_singular,
                definite_plural,
                indefinite_singular
            ) VALUES (?, ?, 'noun', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                word_entry_id,
                input_word_id,
                lemma,
                english,
                float(item["confidence"]),
                singular,
                item["singular_english"].strip(),
                item["plural"].strip().lower(),
                item["plural_english"].strip(),
                item["gender"],
                item["definite_singular"].strip().lower(),
                item["definite_plural"].strip().lower(),
                item["indefinite_singular"].strip().lower(),
            ),
        )
        inserted += 1
    return inserted


def print_banner() -> None:
    title = "08 Create noun word_entries"
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


def main() -> None:
    print_banner()
    parser = argparse.ArgumentParser(
        description="Analyze the first unique dominant NOM lemmas and create noun word_entries."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--limit", type=int, default=MAX_NOUNS)
    parser.add_argument("--workers", type=int, default=10,
                        help="Number of parallel AI request threads (default: 10).")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print candidate rows without calling OpenRouter or writing to the database.",
    )
    args = parser.parse_args()

    with sqlite3.connect(args.db, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")

        existing_noun_count = connection.execute(
            "SELECT COUNT(*) as count FROM word_entries WHERE word_type = 'noun'"
        ).fetchone()["count"]
        if existing_noun_count >= args.limit:
            print(f"Already have {existing_noun_count} noun word_entries (limit: {args.limit}). Exiting.")
            return

        candidates = load_candidates(connection, args.limit)

        if args.dry_run:
            print(f"Found {len(candidates)} unique noun lemma candidates.")
            for candidate in candidates:
                print(
                    f"id={lemma_id(candidate.source_lemma)} "
                    f"input_word_id={candidate.input_word_id} "
                    f"rank={candidate.frequency_rank} "
                    f"wordform={candidate.wordform!r} "
                    f"source_lemma={candidate.source_lemma!r}"
                )
            return

    api_key = load_api_key()
    db_path = args.db
    db_lock = threading.Lock()
    inserted_total = 0
    total = len(candidates)

    def process_one(candidate: NounCandidate) -> int:
        with sqlite3.connect(db_path, timeout=30) as chk:
            already = chk.execute(
                "SELECT 1 FROM word_entries WHERE input_word_id = ? AND word_type = 'noun' LIMIT 1",
                (candidate.input_word_id,),
            ).fetchone()
        if already:
            print(f"  {candidate.source_lemma!r}: skipped (already exists)", flush=True)
            return 0
        item = analyze_one(candidate, api_key)
        with db_lock:
            with sqlite3.connect(db_path, timeout=30) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys = ON")
                n = insert_word_entries(conn, [item])
                conn.commit()
        print(f"  {candidate.source_lemma!r}: inserted {n}", flush=True)
        return n

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process_one, c): c for c in candidates}
        done = 0
        for future in as_completed(futures):
            done += 1
            exc = future.exception()
            if exc:
                cand = futures[future]
                print(f"  [error] {cand.source_lemma!r}: {exc}", flush=True)
            else:
                inserted_total += future.result()
            if done % 50 == 0:
                print(f"  Progress: {done}/{total}", flush=True)

    print(f"Done. Inserted {inserted_total} new noun word_entries.")


if __name__ == "__main__":
    main()
