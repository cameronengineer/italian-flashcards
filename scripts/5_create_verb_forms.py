#!/usr/bin/env python3
"""Generate verb_forms from verb word_entries using OpenRouter structured output."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from common import DEFAULT_DB_PATH, load_api_key

PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_KEY_FILE = PROJECT_ROOT / ".openrouter"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "~google/gemini-flash-latest"
BATCH_SIZE = 5
MAX_VERBS = 999999
MAX_RETRIES = 3
RETRY_DELAY = 5
EXPECTED_FORM_COUNT = 22

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "word_entry_id": {
                        "type": "string",
                        "description": "The word_entries.id value exactly as provided.",
                    },
                    "forms": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "tense": {
                                    "type": "string",
                                    "enum": ["presente", "passato_prossimo", "imperfetto", "imperativo"],
                                },
                                "person": {
                                    "type": "string",
                                    "enum": ["io", "tu", "lui_lei", "noi", "voi", "loro", "Lei"],
                                },
                                "polarity": {
                                    "type": "string",
                                    "enum": ["positive"],
                                },
                                "italian": {
                                    "type": "string",
                                    "description": "The concrete Italian conjugated form or phrase.",
                                },
                                "english": {
                                    "type": "string",
                                    "description": "Natural English translation for a flashcard prompt.",
                                },
                                "labels": {
                                    "type": "string",
                                    "description": "Pipe-separated labels, e.g. tense: presente | subject: io.",
                                },
                            },
                            "required": ["tense", "person", "polarity", "italian", "english", "labels"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["word_entry_id", "forms"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class VerbEntry:
    id: str
    lemma: str
    english: str
    infinitive: str
    auxiliary: str
    past_participle: str
    is_reflexive: bool


def load_entries(connection: sqlite3.Connection, limit: int) -> list[VerbEntry]:
    rows = connection.execute(
        """
        SELECT id, lemma, english, infinitive, auxiliary, past_participle, is_reflexive
        FROM word_entries
        WHERE word_type = 'verb'
          AND NOT EXISTS (
              SELECT 1
              FROM verb_forms
              WHERE verb_forms.word_entry_id = word_entries.id
          )
        ORDER BY created_at, lemma
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        VerbEntry(
            id=row["id"],
            lemma=row["lemma"],
            english=row["english"] or "",
            infinitive=row["infinitive"] or row["lemma"],
            auxiliary=row["auxiliary"] or "unknown",
            past_participle=row["past_participle"] or "",
            is_reflexive=bool(row["is_reflexive"]),
        )
        for row in rows
    ]


def build_prompt(entries: list[VerbEntry]) -> str:
    lines = []
    for entry in entries:
        lines.append(
            json.dumps(
                {
                    "word_entry_id": entry.id,
                    "lemma": entry.lemma,
                    "english": entry.english,
                    "infinitive": entry.infinitive,
                    "auxiliary": entry.auxiliary,
                    "past_participle": entry.past_participle,
                    "is_reflexive": entry.is_reflexive,
                },
                ensure_ascii=False,
            )
        )

    return (
        "Generate Italian verb forms for a flashcard database. Return one item per input verb. "
        "For each verb, generate exactly these forms: presente for io, tu, lui_lei, noi, voi, loro; "
        "passato_prossimo for io, tu, lui_lei, noi, voi, loro; imperfetto for io, tu, lui_lei, noi, voi, loro; "
        "imperativo for tu, Lei, noi, voi. Do not generate io imperative.\n\n"
        "Rules:\n"
        "- Use the supplied auxiliary and past participle for passato_prossimo.\n"
        "- For reflexive verbs, include the correct reflexive pronouns.\n"
        "- person must be one of io, tu, lui_lei, noi, voi, loro, Lei.\n"
        "- polarity is always positive.\n"
        "- labels should be pipe-separated, like 'tense: presente | subject: io'.\n"
        "- english should be a natural prompt, e.g. 'we speak / we are speaking', 'I spoke / I have spoken', 'Speak!'.\n"
        "- Return the word_entry_id exactly as provided.\n\n"
        "Input verbs, one JSON object per line:\n"
        + "\n".join(lines)
    )


def openrouter_structured_request(prompt: str, api_key: str) -> dict:
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "verb_forms",
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
    with urllib.request.urlopen(request, timeout=90) as response:
        body = response.read().decode("utf-8")
    content = json.loads(body)["choices"][0]["message"]["content"]
    return json.loads(content)


def analyze_batch(entries: list[VerbEntry], api_key: str) -> list[dict]:
    prompt = build_prompt(entries)
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


def insert_forms(connection: sqlite3.Connection, items: list[dict]) -> int:
    inserted = 0
    for item in items:
        word_entry_id = item["word_entry_id"]
        for form in item["forms"]:
            italian = form["italian"].strip()
            english = form["english"].strip()
            if not italian or not english:
                continue
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO verb_forms (
                    word_entry_id,
                    tense,
                    person,
                    polarity,
                    italian,
                    english,
                    labels
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    word_entry_id,
                    form["tense"],
                    form["person"],
                    form["polarity"],
                    italian,
                    english,
                    form["labels"].strip(),
                ),
            )
            inserted += cursor.rowcount
    return inserted


def print_banner() -> None:
    title = "5 Generate verb_forms"
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


def main() -> None:
    print_banner()
    parser = argparse.ArgumentParser(
        description="Generate verb_forms for verb word_entries using OpenRouter."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--limit", type=int, default=MAX_VERBS)
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print verb entries without calling OpenRouter or writing forms.",
    )
    args = parser.parse_args()

    with sqlite3.connect(args.db, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        
        verb_entry_count = connection.execute(
            "SELECT COUNT(*) as count FROM word_entries WHERE word_type = 'verb'"
        ).fetchone()["count"]
        verb_forms_count = connection.execute(
            "SELECT COUNT(*) as count FROM verb_forms"
        ).fetchone()["count"]
        expected_forms = verb_entry_count * EXPECTED_FORM_COUNT
        if verb_forms_count >= expected_forms:
            print(f"Already have {verb_forms_count} verb_forms (expected: {expected_forms}). Exiting.")
            return
        
        entries = load_entries(connection, args.limit)

        if args.dry_run:
            print(f"Found {len(entries)} verb entries without forms.")
            for entry in entries:
                print(
                    f"word_entry_id={entry.id} lemma={entry.lemma!r} "
                    f"english={entry.english!r} auxiliary={entry.auxiliary!r}"
                )
            return

        api_key = load_api_key()
        inserted_total = 0
        total_batches = (len(entries) + args.batch_size - 1) // args.batch_size
        for batch_number, start in enumerate(range(0, len(entries), args.batch_size), 1):
            batch = entries[start : start + args.batch_size]
            print(f"Batch {batch_number}/{total_batches}: generating forms for {len(batch)} verbs...", flush=True)
            items = analyze_batch(batch, api_key)
            inserted = insert_forms(connection, items)
            connection.commit()
            inserted_total += inserted
            expected = len(batch) * EXPECTED_FORM_COUNT
            print(f"  inserted {inserted} verb_forms (expected up to {expected})")
            if batch_number < total_batches:
                time.sleep(1)

    print(f"Done. Inserted {inserted_total} new verb_forms.")


if __name__ == "__main__":
    main()
