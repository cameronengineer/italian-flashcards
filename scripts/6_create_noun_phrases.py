#!/usr/bin/env python3
"""Generate noun_phrases from noun word_entries using OpenRouter structured output."""

from __future__ import annotations

import argparse
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

from common import DEFAULT_DB_PATH, load_api_key

PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_KEY_FILE = PROJECT_ROOT / ".openrouter"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "~google/gemini-flash-latest"
MAX_NOUNS = 999999
MAX_RETRIES = 3
RETRY_DELAY = 5

# 14 phrase options: 1 indefinite + 5 prepositions + 2 demonstratives + 6 possessives
PHRASE_OPTIONS = [
    ("indefinite", "indefinite"),
    ("articulated_preposition", "a"),
    ("articulated_preposition", "di"),
    ("articulated_preposition", "da"),
    ("articulated_preposition", "in"),
    ("articulated_preposition", "su"),
    ("demonstrative", "questo"),
    ("demonstrative", "quello"),
    ("possessive", "mio"),
    ("possessive", "tuo"),
    ("possessive", "suo"),
    ("possessive", "nostro"),
    ("possessive", "vostro"),
    ("possessive", "loro"),
]

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "word_entry_id": {
            "type": "string",
            "description": "The word_entries.id value exactly as provided.",
        },
        "phrases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "phrase_type": {
                        "type": "string",
                        "enum": ["definite", "indefinite", "articulated_preposition", "demonstrative", "possessive"],
                    },
                    "number": {
                        "type": "string",
                        "enum": ["singular", "plural"],
                    },
                    "preposition": {
                        "type": "string",
                        "description": "a, di, da, in, su, questo, quello, mio, tuo, suo, nostro, vostro, loro, or empty string for definite/indefinite phrases.",
                    },
                    "italian": {
                        "type": "string",
                        "description": "Correct Italian noun phrase built by the AI.",
                    },
                    "english": {
                        "type": "string",
                        "description": "Natural English flashcard prompt.",
                    },
                    "usage_note": {
                        "type": "string",
                        "description": (
                            "Very short register or style label if this phrase form is "
                            "archaic, formal, vulgar, literary, or regional. "
                            "Use 'archaic' for forms no longer in common modern use. "
                            "Empty string if nothing noteworthy."
                        ),
                    },
                    "labels": {
                        "type": "string",
                        "description": "Pipe-separated labels, e.g. phrase: definite | number: singular.",
                    },
                },
                "required": ["phrase_type", "number", "preposition", "italian", "english", "usage_note", "labels"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["word_entry_id", "phrases"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class NounEntry:
    id: str
    lemma: str
    english: str
    singular: str
    singular_english: str
    plural: str
    plural_english: str
    gender: str
    definite_singular: str
    definite_plural: str
    indefinite_singular: str


def load_entries(connection: sqlite3.Connection, limit: int) -> list[NounEntry]:
    rows = connection.execute(
        """
        SELECT id, lemma, english, singular, singular_english, plural, plural_english, gender,
               definite_singular, definite_plural, indefinite_singular
        FROM word_entries
        WHERE word_type = 'noun'
          AND singular IS NOT NULL AND singular != ''
          AND definite_singular IS NOT NULL AND definite_singular != ''
          AND NOT EXISTS (
              SELECT 1
              FROM noun_phrases
              WHERE noun_phrases.word_entry_id = word_entries.id
          )
        ORDER BY created_at, lemma
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        NounEntry(
            id=row["id"],
            lemma=row["lemma"],
            english=row["english"] or "",
            singular=row["singular"] or row["lemma"],
            singular_english=row["singular_english"] or row["english"] or "",
            plural=row["plural"] or "",
            plural_english=row["plural_english"] or "",
            gender=row["gender"] or "unknown",
            definite_singular=row["definite_singular"] or "",
            definite_plural=row["definite_plural"] or "",
            indefinite_singular=row["indefinite_singular"] or "",
        )
        for row in rows
    ]


def select_phrase_option(singular: str, plural: str) -> tuple[str, str]:
    """
    Deterministically select one phrase option (from 14 options) based on singular and plural forms.
    Returns tuple of (phrase_type, phrase_key) e.g. ("possessive", "mio") or ("indefinite", "indefinite")
    """
    combined = f"{singular}|{plural}"
    digest = hashlib.md5(combined.encode("utf-8")).hexdigest()
    hash_int = int(digest[:8], 16)
    option_index = hash_int % len(PHRASE_OPTIONS)
    return PHRASE_OPTIONS[option_index]


def build_prompt(entry: NounEntry, phrase_type: str, phrase_key: str) -> str:
    has_plural = bool(entry.plural)

    phrases_needed = [
        "1. definite singular — e.g. 'il cane', 'la casa'",
    ]
    if has_plural:
        phrases_needed.append("2. definite plural — e.g. 'i cani', 'le case'")

    if phrase_type == "indefinite":
        phrases_needed.append("3. indefinite singular — e.g. 'un cane', 'una casa'")
        if has_plural:
            phrases_needed.append("4. indefinite plural — e.g. 'dei cani', 'delle case'")
    elif phrase_type == "articulated_preposition":
        phrases_needed.append(f"3. articulated preposition '{phrase_key}' singular — e.g. 'al cane', 'alla casa'")
        if has_plural:
            phrases_needed.append(f"4. articulated preposition '{phrase_key}' plural — e.g. 'ai cani', 'alle case'")
    elif phrase_type == "demonstrative":
        phrases_needed.append(f"3. demonstrative '{phrase_key}' singular — e.g. 'questo cane', 'questa casa'")
        if has_plural:
            phrases_needed.append(f"4. demonstrative '{phrase_key}' plural — e.g. 'questi cani', 'queste case'")
    elif phrase_type == "possessive":
        phrases_needed.append(f"3. possessive '{phrase_key}' singular — e.g. 'il mio cane', 'la mia casa'")
        if has_plural:
            phrases_needed.append(f"4. possessive '{phrase_key}' plural — e.g. 'i miei cani', 'le mie case'")

    item = json.dumps(
        {
            "word_entry_id": entry.id,
            "lemma": entry.lemma,
            "english": entry.english,
            "singular": entry.singular,
            "singular_english": entry.singular_english,
            "plural": entry.plural if has_plural else None,
            "plural_english": entry.plural_english if has_plural else None,
            "gender": entry.gender,
            "definite_singular": entry.definite_singular,
            "definite_plural": entry.definite_plural if has_plural else None,
            "indefinite_singular": entry.indefinite_singular,
        },
        ensure_ascii=False,
    )

    return (
        "Generate Italian noun phrases and their English flashcard prompts for an Italian flashcard database.\n"
        f"For this noun, build EXACTLY the following {len(phrases_needed)} phrase(s) (no more, no less):\n\n"
        "Required phrases:\n"
        + "\n".join(phrases_needed)
        + "\n\n"
        "Italian phrase rules:\n"
        "- Use the correct definite article (il/lo/l'/la/i/gli/le) based on the noun's gender and starting sound.\n"
        "- Use the correct indefinite article (un/uno/un'/una/dei/degli/delle) based on the noun's gender and starting sound.\n"
        "- Use the correct articulated preposition form (e.g. 'al', 'allo', 'all\\'', 'alla' for 'a + article').\n"
        "- Use the correct demonstrative form (e.g. 'quel', 'quello', 'quell\\'', 'quella', 'quei', 'quegli', 'quelle').\n"
        "- Use the correct possessive form agreeing with the noun's gender and number.\n"
        "- For nouns with no plural, only generate singular phrases.\n\n"
        "English rules:\n"
        "- definite: 'the ...' (e.g. 'the dog', 'the dogs').\n"
        "- indefinite: 'a/an ...' singular, 'some ...' plural.\n"
        "- articulated_preposition: natural prepositional phrase (e.g. 'to the dog', 'of the house', 'in the houses').\n"
        "- demonstrative: 'this/these ...' for questo, 'that/those ...' for quello.\n"
        "- possessive: 'my', 'your', 'his/her', 'our', 'your (pl)', 'their' — always 'your (pl)' for vostro.\n"
        "- usage_note: add a very short label if a specific form is archaic, formal, vulgar, literary, or regional. Leave empty for ordinary modern forms.\n"
        "- labels: pipe-separated, e.g. 'phrase: definite | number: singular'.\n"
        "- Return the word_entry_id exactly as provided.\n\n"
        f"Input noun:\n{item}"
    )


def openrouter_structured_request(prompt: str, api_key: str) -> dict:
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "noun_phrases",
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


def analyze_one(entry: NounEntry, api_key: str) -> dict:
    phrase_type, phrase_key = select_phrase_option(entry.singular, entry.plural)
    prompt = build_prompt(entry, phrase_type, phrase_key)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            parsed = openrouter_structured_request(prompt, api_key)
            return parsed
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            print(f"  [attempt {attempt}/{MAX_RETRIES}] {entry.lemma!r}: Request error: {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            print(f"  [attempt {attempt}/{MAX_RETRIES}] {entry.lemma!r}: Parse error: {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    raise RuntimeError(f"OpenRouter request failed for {entry.lemma!r}")


def insert_phrases(connection: sqlite3.Connection, item: dict) -> int:
    inserted = 0
    word_entry_id = item["word_entry_id"]
    for phrase in item["phrases"]:
        italian = phrase["italian"].strip()
        english = phrase["english"].strip()
        usage_note = phrase.get("usage_note", "").strip()
        if usage_note:
            english = f"{english} [{usage_note}]"
        if not italian or not english:
            continue
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO noun_phrases (
                word_entry_id,
                phrase_type,
                number,
                preposition,
                italian,
                english,
                labels
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                word_entry_id,
                phrase["phrase_type"],
                phrase["number"],
                phrase["preposition"].strip() or None,
                italian,
                english,
                phrase["labels"].strip(),
            ),
        )
        inserted += cursor.rowcount
    return inserted


def print_banner() -> None:
    title = "6 Generate noun_phrases"
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


def main() -> None:
    print_banner()
    parser = argparse.ArgumentParser(
        description="Generate noun_phrases for noun word_entries using OpenRouter."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--limit", type=int, default=MAX_NOUNS)
    parser.add_argument("--workers", type=int, default=10,
                        help="Number of parallel AI request threads (default: 10).")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print noun entries and selected phrase options without calling OpenRouter.",
    )
    args = parser.parse_args()

    with sqlite3.connect(args.db, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        entries = load_entries(connection, args.limit)

    if args.dry_run:
        print(f"Found {len(entries)} noun entries without phrases.")
        for entry in entries:
            phrase_type, phrase_key = select_phrase_option(entry.singular, entry.plural)
            has_plural = bool(entry.plural)
            n_phrases = (2 if has_plural else 1) * 2  # definite + selected option, ×2 if plural exists
            print(
                f"word_entry_id={entry.id} lemma={entry.lemma!r} "
                f"gender={entry.gender!r} plural={entry.plural or '(none)'!r} "
                f"selected={phrase_type}:{phrase_key} phrases={n_phrases}"
            )
        return

    api_key = load_api_key()
    db_path = args.db
    db_lock = threading.Lock()
    inserted_total = 0
    total = len(entries)

    def process_one(entry: NounEntry) -> int:
        with sqlite3.connect(db_path, timeout=30) as chk:
            already = chk.execute(
                "SELECT 1 FROM noun_phrases WHERE word_entry_id = ? LIMIT 1",
                (entry.id,),
            ).fetchone()
        if already:
            print(f"  {entry.lemma!r}: skipped (phrases already exist)", flush=True)
            return 0
        item = analyze_one(entry, api_key)
        with db_lock:
            with sqlite3.connect(db_path, timeout=30) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys = ON")
                n = insert_phrases(conn, item)
                conn.commit()
        print(f"  {entry.lemma!r}: inserted {n} phrases", flush=True)
        return n

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process_one, e): e for e in entries}
        done = 0
        for future in as_completed(futures):
            done += 1
            exc = future.exception()
            if exc:
                ent = futures[future]
                print(f"  [error] {ent.lemma!r}: {exc}", flush=True)
            else:
                inserted_total += future.result()
            if done % 50 == 0:
                print(f"  Progress: {done}/{total}", flush=True)

    print(f"Done. Inserted {inserted_total} new noun_phrases.")


if __name__ == "__main__":
    main()
