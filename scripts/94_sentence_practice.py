#!/usr/bin/env python3
"""Generate English sentence practice from learnt Anki cards.

Pulls N random learnt cards from all Italian decks, then calls OpenRouter to
generate English sentences that incorporate those words/concepts.  The output
is written to practice.txt (and printed to stdout) for the user to translate
into Italian.

Requires:
  - Anki running with the AnkiConnect add-on enabled (default port 8765)
  - OpenRouter API key in .openrouter file at project root

Usage:
  python scripts/94_sentence_practice.py
  python scripts/94_sentence_practice.py --count 30
  python scripts/94_sentence_practice.py --sentences 15
  python scripts/94_sentence_practice.py --output path/to/practice.txt
  python scripts/94_sentence_practice.py --deck "Italian - Nouns"
  python scripts/94_sentence_practice.py --length long
  python scripts/94_sentence_practice.py --style direct-pronouns --style indirect-pronouns
  python scripts/94_sentence_practice.py --style combined-pronouns --length long
  python scripts/94_sentence_practice.py --list-styles
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ANKI_CONNECT_URL = "http://127.0.0.1:8765"
ANKI_CONNECT_VERSION = 6
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "~google/gemini-flash-latest"
MAX_RETRIES = 3
RETRY_DELAY = 5

PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_KEY_FILE = PROJECT_ROOT / ".openrouter"
DEFAULT_OUTPUT = PROJECT_ROOT / "practice.txt"

DEFAULT_WORD_COUNT = 50
DEFAULT_SENTENCE_COUNT = 10
DEFAULT_LENGTH = "medium"

LENGTH_GUIDANCE = {
    "short": "The sentence should be short and simple (about 5-10 words).",
    "medium": "The sentence should be of moderate length (about 12-20 words), with at least one subordinate or relative clause.",
    "long": (
        "The sentence should be long and complex (about 20-35 words), "
        "with multiple clauses (e.g. subordinate, relative, or conditional). "
        "It should still feel natural and conversational, not run-on."
    ),
}

# Each style maps to an instruction appended to the prompt. Multiple may be
# combined via repeated --style flags.
STYLE_GUIDANCE = {
    "direct-pronouns": (
        "The Italian translation MUST use at least one direct object pronoun "
        "(mi, ti, lo, la, ci, vi, li, le) — ideally more than one."
    ),
    "indirect-pronouns": (
        "The Italian translation MUST use at least one indirect object pronoun "
        "(mi, ti, gli, le, ci, vi, gli/loro)."
    ),
    "combined-pronouns": (
        "The Italian translation MUST use at least one combined pronoun "
        "(e.g. me lo, te la, glielo, gliela, ce ne, ve li) — i.e. a direct + "
        "indirect object pronoun fused together."
    ),
    "reflexive": (
        "The Italian translation MUST use at least one reflexive verb "
        "(e.g. svegliarsi, lavarsi, divertirsi, accorgersi)."
    ),
    "passato-prossimo": (
        "The Italian translation MUST be in the passato prossimo tense, "
        "with correct auxiliary (essere/avere) and past participle agreement."
    ),
    "imperfetto": (
        "The Italian translation MUST use the imperfetto tense, "
        "ideally describing a habitual past action or a setting/description."
    ),
    "imperfetto-vs-passato": (
        "The Italian translation MUST contrast the imperfetto and passato "
        "prossimo in the same sentence (background action vs. completed action)."
    ),
    "future": (
        "The Italian translation MUST use the futuro semplice tense."
    ),
    "conditional": (
        "The Italian translation MUST use the condizionale (present or past)."
    ),
    "subjunctive": (
        "The Italian translation MUST use the congiuntivo, "
        "triggered by an appropriate expression (e.g. penso che, è importante che, benché)."
    ),
    "imperative": (
        "The Italian translation MUST use at least one imperativo form "
        "(tu, noi, voi, or formal Lei)."
    ),
    "ci-ne": (
        "The Italian translation MUST use the particle 'ci' and/or 'ne' "
        "(e.g. ci vado, ne ho due, ce ne sono)."
    ),
    "relative": (
        "The Italian translation MUST contain at least one relative clause "
        "(introduced by che, cui, il quale, etc.)."
    ),
    "conditional-if": (
        "The Italian translation MUST be a 'periodo ipotetico' (if/then) "
        "sentence — first, second, or third type — using the appropriate "
        "tense/mood combination."
    ),
    "question": (
        "The English sentence MUST be phrased as a question, "
        "and the Italian translation should reflect natural question word order."
    ),
}

ALL_DECKS = [
    "Italian - Nouns",
    "Italian - Verbs Infinitive",
    "Italian - Verbs Presente",
    "Italian - Verbs Passato Prossimo",
    "Italian - Verbs Imperfetto",
    "Italian - Verbs Imperativo",
    "Italian - Numbers",
    "Italian - Conjunctions",
    "Italian - Pronouns",
    "Italian - Interjections",
    "Italian - Espressioni con Avere",
    "Italian - Italki",
    "Italian - Italki Verbs Infinitive",
    "Italian - Italki Verbs Presente",
    "Italian - Italki Verbs Passato Prossimo",
    "Italian - Italki Verbs Imperfetto",
    "Italian - Italki Verbs Imperativo",
]

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "english": {
            "type": "string",
            "description": "A natural English sentence.",
        },
        "italian": {
            "type": "string",
            "description": "The correct Italian translation of the English sentence.",
        },
        "words_used": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The English translations of the Italian words used in this sentence.",
        },
    },
    "required": ["english", "italian", "words_used"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# AnkiConnect helpers
# ---------------------------------------------------------------------------

def invoke(action: str, **params: Any) -> Any:
    payload = json.dumps(
        {"action": action, "version": ANKI_CONNECT_VERSION, "params": params}
    ).encode("utf-8")
    try:
        with urllib.request.urlopen(ANKI_CONNECT_URL, payload, timeout=30) as resp:
            result = json.load(resp)
    except urllib.error.URLError as exc:
        raise RuntimeError(
            "Could not reach AnkiConnect. Make sure Anki is running with "
            "AnkiConnect enabled."
        ) from exc
    if len(result) != 2 or "error" not in result or "result" not in result:
        raise RuntimeError(f"Unexpected AnkiConnect response: {result!r}")
    if result["error"] is not None:
        raise RuntimeError(f"AnkiConnect error: {result['error']}")
    return result["result"]


def chunked(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def strip_html(text: str) -> str:
    """Remove HTML tags and decode common entities."""
    text = re.sub(r"<[^>]+>", "", text)
    text = (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&nbsp;", " ")
    )
    return text.strip()


# ---------------------------------------------------------------------------
# Anki card fetching
# ---------------------------------------------------------------------------

def get_learnt_cards(deck_name: str) -> list[tuple[str, str]]:
    """Return (italian, english) pairs for all learnt cards in *deck_name*."""
    escaped = deck_name.replace('"', '\\"')
    card_ids: list[int] = invoke(
        "findCards",
        query=f'deck:"{escaped}" is:review -is:suspended',
    )
    if not card_ids:
        return []

    cards_info: list[dict] = []
    for chunk in chunked(card_ids, 500):
        cards_info.extend(invoke("cardsInfo", cards=chunk))

    learnt_note_ids: list[int] = []
    seen_notes: set[int] = set()
    for card in cards_info:
        if not card:
            continue
        if card.get("type") not in (2, 3):
            continue
        if card.get("queue") == -1:  # suspended — belt-and-suspenders check
            continue
        note_id = card.get("note")
        if note_id and note_id not in seen_notes:
            seen_notes.add(note_id)
            learnt_note_ids.append(note_id)

    if not learnt_note_ids:
        return []

    notes_info: list[dict] = []
    for chunk in chunked(learnt_note_ids, 500):
        notes_info.extend(invoke("notesInfo", notes=chunk))

    pairs: list[tuple[str, str]] = []
    for note in notes_info:
        if not note:
            continue
        fields = note.get("fields", {})
        italian = strip_html(fields.get("FrontText", {}).get("value", ""))
        english = strip_html(fields.get("BackHighlight", {}).get("value", ""))
        if italian:
            pairs.append((italian, english))

    return pairs


def fetch_all_learnt(decks: list[str]) -> list[tuple[str, str]]:
    """Fetch all learnt (italian, english) pairs across *decks*, deduplicated."""
    all_pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for deck_name in decks:
        try:
            pairs = get_learnt_cards(deck_name)
        except RuntimeError as exc:
            print(f"  WARNING [{deck_name}]: {exc}", file=sys.stderr)
            continue
        for italian, english in pairs:
            if italian not in seen:
                seen.add(italian)
                all_pairs.append((italian, english))
    return all_pairs


# ---------------------------------------------------------------------------
# OpenRouter helpers
# ---------------------------------------------------------------------------

def load_api_key() -> str:
    if not API_KEY_FILE.exists():
        raise FileNotFoundError(
            f"OpenRouter API key not found: {API_KEY_FILE}\n"
            f"Create it with: echo 'your-key-here' > {API_KEY_FILE}"
        )
    key = API_KEY_FILE.read_text(encoding="utf-8").strip()
    if not key:
        raise ValueError(f"API key file is empty: {API_KEY_FILE}")
    return key


def build_prompt(
    words: list[tuple[str, str]],
    length: str = DEFAULT_LENGTH,
    styles: list[str] | None = None,
) -> str:
    word_list = "\n".join(
        f"  - {italian} ({english})" for italian, english in words
    )

    length_line = LENGTH_GUIDANCE.get(length, LENGTH_GUIDANCE[DEFAULT_LENGTH])

    style_lines: list[str] = []
    for style in styles or []:
        guidance = STYLE_GUIDANCE.get(style)
        if guidance:
            style_lines.append(f"- {guidance}")

    constraints_block = ""
    if style_lines:
        constraints_block = (
            "\nGrammatical constraints (ALL must be satisfied):\n"
            + "\n".join(style_lines)
            + "\n"
        )

    return (
        f"You are helping an English speaker practise translating into Italian.\n\n"
        f"Below is a word bank of {len(words)} Italian vocabulary items the learner knows, "
        f"with their English meanings.\n\n"
        f"Word bank:\n{word_list}\n\n"
        f"Write a natural English sentence that uses at least one word from the word bank "
        f"(refer to them by their English meaning — do NOT write Italian in the sentence). "
        f"The sentence should feel natural and conversational, at an intermediate level.\n\n"
        f"Length: {length_line}\n"
        f"{constraints_block}\n"
        f"Also provide:\n"
        f"- italian: the correct, natural Italian translation of that sentence.\n"
        f"- words_used: the English translations from the word bank that appear in the sentence.\n\n"
        f"Return a single sentence object in the JSON schema provided."
    )


def call_openrouter(prompt: str, api_key: str) -> dict:  # noqa: D401
    """Call OpenRouter and return a single sentence dict."""
    import time

    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "sentence_practice",
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
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                body = response.read().decode("utf-8")
            content = json.loads(body)["choices"][0]["message"]["content"]
            return json.loads(content)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            print(f"  [attempt {attempt}/{MAX_RETRIES}] Request error: {exc}", file=sys.stderr)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            print(f"  [attempt {attempt}/{MAX_RETRIES}] Parse error: {exc}", file=sys.stderr)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    raise RuntimeError("OpenRouter request failed after all retries.")


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def format_output(sentences: list[dict]) -> str:
    """Format the full session for saving to disk.

    Each sentence dict is expected to have: english, italian, words_used,
    word_bank (list of (italian, english) tuples added by the generation loop).
    """
    lines: list[str] = []

    lines.append("=" * 60)
    lines.append("  ITALIAN TRANSLATION PRACTICE")
    lines.append("=" * 60)
    lines.append("")

    for i, item in enumerate(sentences, 1):
        lines.append(f"  {i}. {item['english']}")
        lines.append(f"     → {item['italian']}")
        lines.append(f"     Words used: {', '.join(item.get('words_used', []))}")
        lines.append("")
        word_bank: list[tuple[str, str]] = item.get("word_bank", [])
        if word_bank:
            col_w = max(len(it) for it, _ in word_bank)
            lines.append("     Word bank:")
            for it, en in sorted(word_bank, key=lambda x: x[0].lower()):
                lines.append(f"       {it.ljust(col_w)}  —  {en}")
        lines.append("")
        lines.append("  " + "-" * 56)
        lines.append("")

    return "\n".join(lines)


def build_feedback_prompt(english: str, correct_italian: str, attempt: str) -> str:
    return (
        "You are a concise Italian language tutor.\n\n"
        f"English sentence: {english}\n"
        f"Correct Italian: {correct_italian}\n"
        f"Learner's attempt: {attempt}\n\n"
        "Give brief, specific feedback on the learner's attempt. "
        "Point out only the errors (grammar, word choice, conjugation, articles, etc.) "
        "and how to fix them. Do NOT rewrite the whole sentence. "
        "If the attempt is fully correct, just say so in one short line. "
        "Keep the response to 1-4 short lines maximum."
    )


def get_feedback(english: str, correct_italian: str, attempt: str, api_key: str) -> str:
    """Call OpenRouter and return plain-text feedback on the learner's attempt."""
    import time

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "user", "content": build_feedback_prompt(english, correct_italian, attempt)}
        ],
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
    for attempt_n in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read().decode("utf-8")
            return json.loads(body)["choices"][0]["message"]["content"].strip()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            print(f"  [attempt {attempt_n}/{MAX_RETRIES}] Request error: {exc}", file=sys.stderr)
            if attempt_n < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt_n)
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            print(f"  [attempt {attempt_n}/{MAX_RETRIES}] Parse error: {exc}", file=sys.stderr)
            if attempt_n < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    return "(Could not retrieve feedback)"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def print_banner() -> None:
    title = "94 Sentence Practice — generate English sentences from learnt words"
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


def main() -> int:
    print_banner()
    parser = argparse.ArgumentParser(
        description=(
            "Pull learnt cards from Anki and generate English sentences to "
            "practise translating into Italian."
        )
    )
    parser.add_argument(
        "--count",
        type=int,
        default=DEFAULT_WORD_COUNT,
        metavar="N",
        help=f"Number of random words to pull from Anki (default: {DEFAULT_WORD_COUNT}).",
    )
    parser.add_argument(
        "--sentences",
        type=int,
        default=DEFAULT_SENTENCE_COUNT,
        metavar="N",
        help=f"Number of sentences to generate (default: {DEFAULT_SENTENCE_COUNT}).",
    )
    parser.add_argument(
        "--deck",
        metavar="DECK_NAME",
        help="Pull words from only this deck (default: all Italian decks).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        metavar="FILE",
        help=f"Output file path (default: {DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        metavar="SEED",
        help="Random seed for reproducible word selection.",
    )
    parser.add_argument(
        "--length",
        choices=sorted(LENGTH_GUIDANCE.keys()),
        default=DEFAULT_LENGTH,
        help=(
            "Target sentence length / complexity "
            f"(default: {DEFAULT_LENGTH})."
        ),
    )
    parser.add_argument(
        "--style",
        action="append",
        choices=sorted(STYLE_GUIDANCE.keys()),
        metavar="STYLE",
        default=[],
        help=(
            "Grammatical constraint to require in the Italian translation. "
            "May be passed multiple times to combine constraints. "
            "Choices: " + ", ".join(sorted(STYLE_GUIDANCE.keys())) + "."
        ),
    )
    parser.add_argument(
        "--list-styles",
        action="store_true",
        help="Print the available --style options with descriptions and exit.",
    )
    args = parser.parse_args()

    if args.list_styles:
        print("\nAvailable --style options:\n")
        width = max(len(s) for s in STYLE_GUIDANCE)
        for name in sorted(STYLE_GUIDANCE):
            print(f"  {name.ljust(width)}  {STYLE_GUIDANCE[name]}")
        print()
        return 0

    decks = [args.deck] if args.deck else ALL_DECKS

    # --- Fetch learnt cards ---
    print("\nFetching learnt cards from Anki...", flush=True)
    all_pairs = fetch_all_learnt(decks)

    if not all_pairs:
        print("No learnt cards found.", file=sys.stderr)
        return 1

    print(f"  Found {len(all_pairs)} learnt words across all decks.", flush=True)

    # --- Load API key ---
    try:
        api_key = load_api_key()
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    rng = random.Random(args.seed)
    word_count = min(args.count, len(all_pairs))
    completed: list[dict] = []

    print()
    print("=" * 60)
    print("  ITALIAN TRANSLATION PRACTICE")
    print("=" * 60)
    print(f"  Length: {args.length}")
    if args.style:
        print(f"  Styles: {', '.join(args.style)}")
    else:
        print("  Styles: (none — free-form)")
    print("  Type your Italian translation, or press Enter to skip.")
    print("=" * 60)

    for i in range(1, args.sentences + 1):
        # Fresh word sample for every sentence
        selected = rng.sample(all_pairs, word_count)

        print(f"\n  Generating sentence {i}/{args.sentences}...", flush=True)
        try:
            item = call_openrouter(
                build_prompt(selected, length=args.length, styles=args.style),
                api_key,
            )
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            break

        # Attach word bank to item for the saved file
        item["word_bank"] = selected

        print(f"\n  {i}/{args.sentences}  {item['english']}")
        print()
        try:
            attempt = input("  Your Italian: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            completed.append(item)
            break

        if attempt:
            print("\n  Getting feedback...", flush=True)
            feedback = get_feedback(item["english"], item["italian"], attempt, api_key)
            print()
            for line in feedback.splitlines():
                print(f"  {line}")

        print()
        print(f"  ✓ {item['italian']}")
        print()
        print("  " + "-" * 56)

        completed.append(item)

    # --- Save session to disk ---
    if completed:
        output_text = format_output(completed)
        args.output.write_text(output_text, encoding="utf-8")
        print(f"\nSession saved to: {args.output}", flush=True)

    print()
    print("=" * 60)
    print("  Done!")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
