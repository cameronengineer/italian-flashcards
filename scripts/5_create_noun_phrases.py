#!/usr/bin/env python3
"""Generate noun_phrases from noun word_entries using OpenRouter structured output."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "database.sqlite"
API_KEY_FILE = PROJECT_ROOT / ".openrouter"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "~google/gemini-flash-latest"
BATCH_SIZE = 10
MAX_NOUNS = 999999
MAX_RETRIES = 3
RETRY_DELAY = 5

ARTICULATED_PREPOSITIONS = {
    "a": {"il": "al", "lo": "allo", "l'": "all'", "la": "alla", "i": "ai", "gli": "agli", "le": "alle"},
    "di": {"il": "del", "lo": "dello", "l'": "dell'", "la": "della", "i": "dei", "gli": "degli", "le": "delle"},
    "da": {"il": "dal", "lo": "dallo", "l'": "dall'", "la": "dalla", "i": "dai", "gli": "dagli", "le": "dalle"},
    "in": {"il": "nel", "lo": "nello", "l'": "nell'", "la": "nella", "i": "nei", "gli": "negli", "le": "nelle"},
    "su": {"il": "sul", "lo": "sullo", "l'": "sull'", "la": "sulla", "i": "sui", "gli": "sugli", "le": "sulle"},
}

DEMONSTRATIVES = {
    "questo": {"il": "questo", "lo": "questo", "l'": "questo", "la": "questa", "i": "questi", "gli": "questi", "le": "queste"},
    "quello": {"il": "quel", "lo": "quello", "l'": "quello", "la": "quella", "i": "quei", "gli": "quegli", "le": "quelle"},
}

POSSESSIVES = {
    "mio": {"il": "il mio", "lo": "lo mio", "l'": "l' mio", "la": "la mia", "i": "i miei", "gli": "gli miei", "le": "le mie"},
    "tuo": {"il": "il tuo", "lo": "lo tuo", "l'": "l' tuo", "la": "la tua", "i": "i tuoi", "gli": "gli tuoi", "le": "le tue"},
    "suo": {"il": "il suo", "lo": "lo suo", "l'": "l' suo", "la": "la sua", "i": "i suoi", "gli": "gli suoi", "le": "le sue"},
    "nostro": {"il": "il nostro", "lo": "lo nostro", "l'": "l' nostro", "la": "la nostra", "i": "i nostri", "gli": "gli nostri", "le": "le nostre"},
    "vostro": {"il": "il vostro", "lo": "lo vostro", "l'": "l' vostro", "la": "la vostra", "i": "i vostri", "gli": "gli vostri", "le": "le vostre"},
    "loro": {"il": "il loro", "lo": "lo loro", "l'": "l' loro", "la": "la loro", "i": "i loro", "gli": "gli loro", "le": "le loro"},
}

INDEFINITE_ARTICLES = {
    "il": "un",
    "lo": "uno",
    "l'": "un'",
    "la": "una",
    "i": "dei",
    "gli": "degli",
    "le": "delle",
}

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
        "items": {
            "type": "array",
            "items": {
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
                                    "description": "a, di, da, in, su, questo, quello, mio, tuo, suo, nostro, vostro, loro, or empty for non-preposition phrases.",
                                },
                                "italian": {
                                    "type": "string",
                                    "description": "Concrete Italian noun phrase.",
                                },
                                "english": {
                                    "type": "string",
                                    "description": "Natural English flashcard prompt.",
                                },
                                "labels": {
                                    "type": "string",
                                    "description": "Pipe-separated labels, e.g. phrase: definite | number: singular.",
                                },
                            },
                            "required": ["phrase_type", "number", "preposition", "italian", "english", "labels"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["word_entry_id", "phrases"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
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


def load_api_key() -> str:
    if not API_KEY_FILE.exists():
        raise FileNotFoundError(f"OpenRouter API key file not found: {API_KEY_FILE}")
    api_key = API_KEY_FILE.read_text(encoding="utf-8").strip()
    if not api_key:
        raise ValueError(f"OpenRouter API key file is empty: {API_KEY_FILE}")
    return api_key


def load_entries(connection: sqlite3.Connection, limit: int) -> list[NounEntry]:
    rows = connection.execute(
        """
        SELECT id, lemma, english, singular, singular_english, plural, plural_english, gender,
               definite_singular, definite_plural, indefinite_singular
        FROM word_entries
        WHERE word_type = 'noun'
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


def phrase_join(article_or_prep: str, noun: str) -> str:
    if not article_or_prep:
        return noun
    if article_or_prep.endswith("'"):
        return f"{article_or_prep}{noun}"
    return f"{article_or_prep} {noun}"


def select_phrase_option(singular: str, plural: str) -> tuple[str, str]:
    """
    Deterministically select one phrase option (from 14 options) based on singular and plural forms.
    Returns tuple of (phrase_type, phrase_key) e.g. ("possessive", "mio") or ("indefinite", "indefinite")
    """
    combined = f"{singular}|{plural}"
    digest = hashlib.md5(combined.encode("utf-8")).hexdigest()
    hash_int = int(digest[:8], 16)
    option_index = hash_int % len(PHRASE_OPTIONS)
    phrase_type, phrase_key = PHRASE_OPTIONS[option_index]
    return (phrase_type, phrase_key)


def deterministic_phrases(entry: NounEntry) -> list[dict[str, str]]:
    phrases: list[dict[str, str]] = []

    # Always add definite articles (singular and plural)
    if entry.definite_singular and entry.singular:
        phrases.append(
            {
                "phrase_type": "definite",
                "number": "singular",
                "preposition": "",
                "italian": phrase_join(entry.definite_singular, entry.singular),
                "labels": "phrase: definite | number: singular",
            }
        )
    if entry.definite_plural and entry.plural:
        phrases.append(
            {
                "phrase_type": "definite",
                "number": "plural",
                "preposition": "",
                "italian": phrase_join(entry.definite_plural, entry.plural),
                "labels": "phrase: definite | number: plural",
            }
        )

    # Select ONE option from the 14 phrase options
    phrase_type, phrase_key = select_phrase_option(entry.singular or "", entry.plural or "")

    # Generate singular form of the selected option
    if phrase_type == "indefinite":
        if entry.indefinite_singular and entry.singular:
            phrases.append(
                {
                    "phrase_type": "indefinite",
                    "number": "singular",
                    "preposition": "",
                    "italian": phrase_join(entry.indefinite_singular, entry.singular),
                    "labels": "phrase: indefinite | number: singular",
                }
            )
    elif phrase_type == "articulated_preposition":
        article_map = ARTICULATED_PREPOSITIONS[phrase_key]
        if entry.definite_singular in article_map and entry.singular:
            phrases.append(
                {
                    "phrase_type": "articulated_preposition",
                    "number": "singular",
                    "preposition": phrase_key,
                    "italian": phrase_join(article_map[entry.definite_singular], entry.singular),
                    "labels": f"phrase: articulated_preposition | preposition: {phrase_key} | number: singular",
                }
            )
    elif phrase_type == "demonstrative":
        demo_map = DEMONSTRATIVES[phrase_key]
        if entry.definite_singular in demo_map and entry.singular:
            phrases.append(
                {
                    "phrase_type": "demonstrative",
                    "number": "singular",
                    "preposition": phrase_key,
                    "italian": phrase_join(demo_map[entry.definite_singular], entry.singular),
                    "labels": f"phrase: demonstrative | preposition: {phrase_key} | number: singular",
                }
            )
    elif phrase_type == "possessive":
        poss_map = POSSESSIVES[phrase_key]
        if entry.definite_singular in poss_map and entry.singular:
            phrases.append(
                {
                    "phrase_type": "possessive",
                    "number": "singular",
                    "preposition": phrase_key,
                    "italian": phrase_join(poss_map[entry.definite_singular], entry.singular),
                    "labels": f"phrase: possessive | preposition: {phrase_key} | number: singular",
                }
            )

    # Generate plural form of the selected option
    if phrase_type == "indefinite":
        # Indefinite plural uses "dei/degli/delle"
        indefinite_plural = INDEFINITE_ARTICLES.get(entry.definite_plural, "")
        if indefinite_plural and entry.plural:
            phrases.append(
                {
                    "phrase_type": "indefinite",
                    "number": "plural",
                    "preposition": "",
                    "italian": phrase_join(indefinite_plural, entry.plural),
                    "labels": "phrase: indefinite | number: plural",
                }
            )
    elif phrase_type == "articulated_preposition":
        article_map = ARTICULATED_PREPOSITIONS[phrase_key]
        if entry.definite_plural in article_map and entry.plural:
            phrases.append(
                {
                    "phrase_type": "articulated_preposition",
                    "number": "plural",
                    "preposition": phrase_key,
                    "italian": phrase_join(article_map[entry.definite_plural], entry.plural),
                    "labels": f"phrase: articulated_preposition | preposition: {phrase_key} | number: plural",
                }
            )
    elif phrase_type == "demonstrative":
        demo_map = DEMONSTRATIVES[phrase_key]
        if entry.definite_plural in demo_map and entry.plural:
            phrases.append(
                {
                    "phrase_type": "demonstrative",
                    "number": "plural",
                    "preposition": phrase_key,
                    "italian": phrase_join(demo_map[entry.definite_plural], entry.plural),
                    "labels": f"phrase: demonstrative | preposition: {phrase_key} | number: plural",
                }
            )
    elif phrase_type == "possessive":
        poss_map = POSSESSIVES[phrase_key]
        if entry.definite_plural in poss_map and entry.plural:
            phrases.append(
                {
                    "phrase_type": "possessive",
                    "number": "plural",
                    "preposition": phrase_key,
                    "italian": phrase_join(poss_map[entry.definite_plural], entry.plural),
                    "labels": f"phrase: possessive | preposition: {phrase_key} | number: plural",
                }
            )

    return phrases


def build_prompt(entries: list[NounEntry]) -> str:
    lines = []
    for entry in entries:
        lines.append(
            json.dumps(
                {
                    "word_entry_id": entry.id,
                    "lemma": entry.lemma,
                    "english": entry.english,
                    "singular": entry.singular,
                    "singular_english": entry.singular_english,
                    "plural": entry.plural,
                    "plural_english": entry.plural_english,
                    "gender": entry.gender,
                    "definite_singular": entry.definite_singular,
                    "definite_plural": entry.definite_plural,
                    "indefinite_singular": entry.indefinite_singular,
                    "phrases_to_translate": deterministic_phrases(entry),
                },
                ensure_ascii=False,
            )
        )

    return (
        "Generate English prompts for Italian noun phrases for a flashcard database. "
        "Return one item per input noun. The Italian phrase list has already been generated from the README rules; "
        "do not add, remove, or alter Italian phrases. Copy phrase_type, number, preposition, italian, and labels exactly. "
        "Only fill natural English prompts.\n\n"
        "English rules:\n"
        "- definite: use 'the ...' (e.g. 'the friend', 'the friends').\n"
        "- indefinite: use 'a/an ...' or plural forms (e.g. 'a friend', 'some friends').\n"
        "- articulated_preposition: use natural prepositional prompts (e.g. 'to the house', 'of the friend', 'in the houses').\n"
        "- demonstrative: use 'this ...' for questo or 'that ...' for quello (e.g. 'this friend', 'that friend', 'these friends', 'those friends').\n"
        "- possessive: use natural possessive prompts (e.g. 'my friend', 'your friend', 'his friend', 'our friend', 'your (pl) friend', 'their friend').\n"
        "- Return the word_entry_id exactly as provided.\n\n"
        "Input nouns, one JSON object per line:\n"
        + "\n".join(lines)
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


def analyze_batch(entries: list[NounEntry], api_key: str) -> list[dict]:
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


def insert_phrases(connection: sqlite3.Connection, items: list[dict]) -> int:
    inserted = 0
    for item in items:
        word_entry_id = item["word_entry_id"]
        for phrase in item["phrases"]:
            italian = phrase["italian"].strip()
            english = phrase["english"].strip()
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
    title = "5 Generate noun_phrases"
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


def main() -> None:
    print_banner()
    parser = argparse.ArgumentParser(
        description="Generate noun_phrases for noun word_entries using OpenRouter."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--limit", type=int, default=MAX_NOUNS)
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print noun entries and deterministic phrases without calling OpenRouter or writing phrases.",
    )
    args = parser.parse_args()

    with sqlite3.connect(args.db, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        entries = load_entries(connection, args.limit)

        if args.dry_run:
            print(f"Found {len(entries)} noun entries without phrases.")
            for entry in entries:
                phrases = deterministic_phrases(entry)
                print(
                    f"word_entry_id={entry.id} lemma={entry.lemma!r} "
                    f"english={entry.english!r} phrases={len(phrases)}"
                )
                for phrase in phrases:
                    print(f"  {phrase['phrase_type']} {phrase['number']}: {phrase['italian']}")
            return

        noun_entry_count = connection.execute(
            "SELECT COUNT(*) as count FROM word_entries WHERE word_type = 'noun'"
        ).fetchone()["count"]
        noun_phrase_count = connection.execute(
            "SELECT COUNT(*) as count FROM noun_phrases"
        ).fetchone()["count"]
        if noun_phrase_count > 0 and noun_entry_count > 0:
            print(f"Already have {noun_phrase_count} noun_phrases for {noun_entry_count} noun entries. Exiting.")
            return

        api_key = load_api_key()
        inserted_total = 0
        total_batches = (len(entries) + args.batch_size - 1) // args.batch_size
        for batch_number, start in enumerate(range(0, len(entries), args.batch_size), 1):
            batch = entries[start : start + args.batch_size]
            print(f"Batch {batch_number}/{total_batches}: generating phrases for {len(batch)} nouns...", flush=True)
            items = analyze_batch(batch, api_key)
            inserted = insert_phrases(connection, items)
            connection.commit()
            inserted_total += inserted
            print(f"  inserted {inserted} noun_phrases")
            if batch_number < total_batches:
                time.sleep(1)

    print(f"Done. Inserted {inserted_total} new noun_phrases.")


if __name__ == "__main__":
    main()
