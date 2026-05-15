#!/usr/bin/env python3
"""Create verb word_entries from input_words using OpenRouter structured output."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from common import DEFAULT_DB_PATH, load_api_key, lemma_id

PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_KEY_FILE = PROJECT_ROOT / ".openrouter"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "~google/gemini-flash-latest"
BATCH_SIZE = 20
MAX_VERBS = 400
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
                        "description": "The dominant verb lemma from dom_lemma exactly as provided.",
                    },
                    "valid": {
                        "type": "boolean",
                        "description": "True when source_lemma can be resolved to a real Italian verb infinitive.",
                    },
                    "lemma": {
                        "type": "string",
                        "description": "Canonical Italian verb infinitive, e.g. essere, andare, fare.",
                    },
                    "english": {
                        "type": "string",
                        "description": "Concise English infinitive translation, e.g. to be, to go.",
                    },
                    "confidence": {
                        "type": "number",
                        "description": "Confidence from 0.0 to 1.0.",
                    },
                    "infinitive": {
                        "type": "string",
                        "description": "Canonical Italian infinitive; normally the same as lemma.",
                    },
                    "auxiliary": {
                        "type": "string",
                        "enum": ["avere", "essere", "both", "unknown"],
                        "description": "Auxiliary used for compound tenses.",
                    },
                    "past_participle": {
                        "type": "string",
                        "description": "Masculine singular past participle, e.g. fatto, andato.",
                    },
                    "is_reflexive": {
                        "type": "boolean",
                        "description": "True for reflexive/pronominal verbs such as svegliarsi.",
                    },
                },
                "required": [
                    "input_word_id",
                    "source_wordform",
                    "source_lemma",
                    "valid",
                    "lemma",
                    "english",
                    "confidence",
                    "infinitive",
                    "auxiliary",
                    "past_participle",
                    "is_reflexive",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class VerbCandidate:
    input_word_id: str
    frequency_rank: int | None
    wordform: str
    source_lemma: str
    dom_pos: str
    existing_word_entry_id: str | None = None


def migrate_word_entry_id_schema(connection: sqlite3.Connection) -> None:
    word_entry_column = connection.execute("PRAGMA table_info(word_entries)").fetchall()[0]
    input_word_column = connection.execute("PRAGMA table_info(input_words)").fetchall()[0]
    if (
        word_entry_column["name"] == "id"
        and word_entry_column["type"].upper() == "TEXT"
        and input_word_column["name"] == "id"
        and input_word_column["type"].upper() == "TEXT"
    ):
        return

    connection.executescript(
        """
        PRAGMA foreign_keys = OFF;
        DROP TABLE IF EXISTS anki_cards;
        DROP TABLE IF EXISTS card_items;
        DROP TABLE IF EXISTS noun_phrases;
        DROP TABLE IF EXISTS verb_forms;
        DROP TABLE IF EXISTS word_entries;

        CREATE TABLE word_entries (
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

        CREATE TABLE verb_forms (
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

        CREATE TABLE noun_phrases (
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

        CREATE TABLE card_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL,
            source_id INTEGER,
            deck TEXT NOT NULL,
            front_text TEXT NOT NULL,
            front_labels TEXT,
            back_highlight TEXT NOT NULL,
            back_text TEXT,
            audio_text TEXT,
            image_text TEXT,
            UNIQUE(source_type, source_id)
        );

        CREATE INDEX IF NOT EXISTS idx_card_items_deck ON card_items(deck);
        CREATE INDEX IF NOT EXISTS idx_card_items_source ON card_items(source_type, source_id);

        CREATE TABLE anki_cards (
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
        PRAGMA foreign_keys = ON;
        """
    )


def load_candidates(connection: sqlite3.Connection, limit: int) -> list[VerbCandidate]:
    rows = connection.execute(
        """
        SELECT id, frequency_rank, wordform, dom_pos, dom_lemma
        FROM input_words
        WHERE dom_pos = 'VER'
          AND dom_lemma IS NOT NULL
          AND dom_lemma != '<unknown>'
        ORDER BY frequency_rank, id
        """
    ).fetchall()

    incomplete_entries = {
        row["input_word_id"]: row["id"]
        for row in connection.execute(
            """
            SELECT id, input_word_id
            FROM word_entries
            WHERE word_type = 'verb'
              AND (
                  english IS NULL OR TRIM(english) = '' OR
                  confidence IS NULL OR
                  infinitive IS NULL OR TRIM(infinitive) = '' OR
                  auxiliary IS NULL OR TRIM(auxiliary) = '' OR auxiliary = 'unknown' OR
                  past_participle IS NULL OR TRIM(past_participle) = ''
              )
            """
        ).fetchall()
    }

    processed_input_word_ids = {
        row["input_word_id"]
        for row in connection.execute(
            """
            SELECT DISTINCT input_word_id
            FROM word_entries
            WHERE word_type = 'verb'
            """
        ).fetchall()
    }

    candidates: list[VerbCandidate] = []
    seen_lemmas: set[str] = set()
    for row in rows:
        source_lemma = row["dom_lemma"].strip()
        normalized_lemma = source_lemma.lower()
        
        if normalized_lemma in seen_lemmas:
            continue
        
        existing_word_entry_id = incomplete_entries.get(row["id"])
        
        if row["id"] in processed_input_word_ids and existing_word_entry_id is None:
            continue

        seen_lemmas.add(normalized_lemma)
        if len(seen_lemmas) > limit:
            return candidates

        candidates.append(
            VerbCandidate(
                input_word_id=row["id"],
                frequency_rank=row["frequency_rank"],
                wordform=row["wordform"],
                source_lemma=source_lemma,
                dom_pos=row["dom_pos"],
                existing_word_entry_id=existing_word_entry_id,
            )
        )
    return candidates


def build_prompt(candidates: list[VerbCandidate]) -> str:
    lines = []
    for candidate in candidates:
        lines.append(
            json.dumps(
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
        )

    return (
        "You are creating verb dictionary entries for an Italian flashcard database. "
        "Each item comes from one SUBTLEX-IT input_words row where dom_pos is VER. "
        "Use the dominant dom_lemma value supplied as source_lemma, not normalized_word and not all_pos_lemma. "
        "Do not create entries for secondary or rare analyses from all_pos_lemma. Duplicate source lemmas were already removed before this request.\n\n"
        "For each item, return exactly one output item with the same input_word_id, source_wordform, "
        "and source_lemma. If source_lemma is a valid Italian verb or can be safely resolved to a valid "
        "Italian infinitive, set valid=true and fill the canonical verb fields. If it is not a verb, is a "
        "data artifact, or cannot be resolved safely, set valid=false, use empty strings for text fields, "
        "auxiliary='unknown', confidence=0, and is_reflexive=false.\n\n"
        "Rules:\n"
        "- lemma and infinitive should be the canonical Italian infinitive.\n"
        "- english should be concise and start with 'to' where natural, e.g. 'to be'.\n"
        "- past_participle should be masculine singular.\n"
        "- auxiliary must be one of: avere, essere, both, unknown.\n"
        "- confidence must be between 0 and 1.\n\n"
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
                "name": "verb_word_entries",
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


def analyze_batch(candidates: list[VerbCandidate], api_key: str) -> list[dict]:
    prompt = build_prompt(candidates)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            parsed = openrouter_structured_request(prompt, api_key)
            return parsed["items"]
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            print(f"  [attempt {attempt}/{MAX_RETRIES}] Request error: {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)
        except (json.JSONDecodeError, KeyError) as exc:
            print(f"  [attempt {attempt}/{MAX_RETRIES}] Parse error: {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    raise RuntimeError("OpenRouter request failed after all retries")


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
        infinitive = (item["infinitive"].strip().lower() or lemma)
        if not lemma or not infinitive:
            continue

        word_entry_id = lemma_id(lemma)
        existing_for_input = connection.execute(
            """
            SELECT id
            FROM word_entries
            WHERE input_word_id = ? AND word_type = 'verb'
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
                    infinitive = ?,
                    auxiliary = ?,
                    past_participle = ?,
                    is_reflexive = ?
                WHERE id = ?
                """,
                (
                    word_entry_id,
                    lemma,
                    item["english"].strip(),
                    float(item["confidence"]),
                    infinitive,
                    item["auxiliary"],
                    item["past_participle"].strip().lower(),
                    1 if item["is_reflexive"] else 0,
                    existing_for_input["id"],
                ),
            )
            inserted += 1
            continue

        existing = connection.execute(
            """
            SELECT 1
            FROM word_entries
            WHERE id = ? OR input_word_id = ? OR lemma = ? OR (word_type = 'verb' AND infinitive = ?)
            LIMIT 1
            """,
            (word_entry_id, input_word_id, lemma, infinitive),
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
                infinitive,
                auxiliary,
                past_participle,
                is_reflexive
            ) VALUES (?, ?, 'verb', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                word_entry_id,
                input_word_id,
                lemma,
                item["english"].strip(),
                float(item["confidence"]),
                infinitive,
                item["auxiliary"],
                item["past_participle"].strip().lower(),
                1 if item["is_reflexive"] else 0,
            ),
        )
        inserted += 1
    return inserted


def print_banner() -> None:
    title = "3 Create verb word_entries"
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


def main() -> None:
    print_banner()
    parser = argparse.ArgumentParser(
        description="Analyze the first unique dominant VER lemmas and create verb word_entries."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--limit", type=int, default=MAX_VERBS)
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print candidate rows without calling OpenRouter or writing to the database.",
    )
    args = parser.parse_args()

    with sqlite3.connect(args.db, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        migrate_word_entry_id_schema(connection)

        existing_verb_count = connection.execute(
            "SELECT COUNT(*) as count FROM word_entries WHERE word_type = 'verb'"
        ).fetchone()["count"]
        if existing_verb_count >= args.limit:
            print(f"Already have {existing_verb_count} verb word_entries (limit: {args.limit}). Exiting.")
            return

        candidates = load_candidates(connection, args.limit)

        if args.dry_run:
            print(f"Found {len(candidates)} unique verb lemma candidates.")
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
        inserted_total = 0
        total_batches = (len(candidates) + args.batch_size - 1) // args.batch_size
        for batch_number, start in enumerate(range(0, len(candidates), args.batch_size), 1):
            batch = candidates[start : start + args.batch_size]
            print(f"Batch {batch_number}/{total_batches}: analyzing {len(batch)} unique verb lemmas...", flush=True)
            items = analyze_batch(batch, api_key)
            inserted = insert_word_entries(connection, items)
            connection.commit()
            inserted_total += inserted
            print(f"  inserted {inserted} word_entries")
            if batch_number < total_batches:
                time.sleep(1)

    print(f"Done. Inserted {inserted_total} new verb word_entries.")


if __name__ == "__main__":
    main()
