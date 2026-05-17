#!/usr/bin/env python3
"""Audit card_items with AI to detect quality issues and write findings to ISSUES.txt.

Sends one card per AI call, massively parallel via ThreadPoolExecutor.
Issues are appended to ISSUES.txt immediately (thread-safe) so progress is
preserved if the run is interrupted.

Usage:
    python scripts/90_audit_cards.py                    # audit all cards
    python scripts/90_audit_cards.py --limit 100        # first 100 cards
    python scripts/90_audit_cards.py --resume           # skip already-audited cards
    python scripts/90_audit_cards.py --deck "Italian - Nouns"
    python scripts/90_audit_cards.py --source-type noun_phrase
    python scripts/90_audit_cards.py --workers 100      # parallel threads (default 50)
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from common import DEFAULT_DB_PATH, load_api_key

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ISSUES_FILE = PROJECT_ROOT / "ISSUES.txt"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

MODEL = "openai/gpt-5-nano"

MAX_RETRIES = 3
RETRY_DELAY = 5

# ── Prompts ───────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are an expert Italian language teacher checking a single flashcard for errors.

The card fields are:
  id             - row id
  source_type    - noun_phrase | verb_form | infinitive_verb | input_word
  deck           - e.g. "Italian - Verbs Presente"
  front_text     - English prompt
  front_labels   - metadata, e.g. "tense: presente | subject: io"
  back_highlight - the Italian answer being tested
  back_text      - supplementary info (e.g. the verb infinitive); may be blank
  audio_text     - Italian text for TTS; usually same as back_highlight
  image_text     - Italian text for image generation; may be the infinitive

Return a JSON array of issues. Return [] if the card is correct.
Only flag an issue if you are 100% certain it is wrong. When in doubt, return [].
Never produce an issue where "found" and "expected" are identical.

━━━ INTENTIONAL DESIGN — NEVER FLAG THESE ━━━

1. DUAL ENGLISH PROMPTS
   All tense cards show two English forms separated by " / ":
     "I call / I am calling"      → chiamo   ✓  (presente)
     "I called / I was calling"   → chiamavo ✓  (imperfetto)
     "I called / I have called"   → ho chiamato ✓ (passato prossimo)
   The second form is a natural English equivalent, not an error.
   Do NOT flag front_text for containing two English translations.

2. IMAGE_TEXT = INFINITIVE
   image_text often contains the verb infinitive rather than the conjugated form.
   This is intentional. Never flag image_text for showing the infinitive.

3. GENDER NOTATION
   Slash notation encodes both genders on one card: andato/a, andati/e ✓
   Never flag /a or /e suffix notation.

4. LEI (FORMAL) IMPERATIVE — USES PRESENT SUBJUNCTIVE
   The Lei imperative is the present subjunctive, not the indicative:
     -are: chiami, paghi, entri, cambi, sposi, sembri, arrivi ✓
     -ere/-ire: veda, scriva, parta, finisca ✓
     irregular: vada, faccia, dica, venga, sia, abbia, sappia, voglia, scelga ✓

5. PRESENT INDICATIVE TU OF -ARE VERBS ENDS IN -I
   chiamare → chiami   pagare → paghi   sposare → sposi ✓

6. ESSERE AUXILIARY FOR INTRANSITIVE/IMPERSONAL VERBS
   bastare, sembrare, piacere, mancare, and similar verbs use essere ✓

━━━ WHAT TO FLAG ━━━

Only flag clear, unambiguous errors in front_text or back_highlight:

  WRONG_ITALIAN     - back_highlight is misspelled or the wrong Italian form
  WRONG_ENGLISH     - front_text has an English spelling/grammar error, or names
                      the wrong word entirely (e.g. "to call" when back is capire)
  WRONG_AUXILIARY   - passato prossimo uses avere/essere incorrectly
  WRONG_CONJUGATION - conjugated form is clearly wrong for the tense+subject
  WRONG_ARTICLE     - article doesn't match the noun's gender/number
  MALFORMED_PHRASE  - structurally broken Italian (e.g. errant space: "l' amico")
  INCONSISTENT      - front and back clearly refer to entirely different words
  MISSING_CONTENT   - back_highlight is blank
  UNNATURAL         - the form is theoretically grammatical but so rare or archaic
                      that no modern native speaker would use it; a learner taught
                      this form would sound bizarre or be misunderstood
  OTHER             - any other unambiguous error

For UNNATURAL, only flag when you are certain a natural alternative exists and is
overwhelmingly preferred. Examples of forms that warrant UNNATURAL:

  WRONG VERB FOR AN IDIOMATIC EXPRESSION
  Italian uses avere (not essere or a standalone noun) for many expressions where
  English uses "to be" or "to need". Teaching the bare noun as the answer is wrong:
    "bisogno" alone for "I need"   → correct: "ho bisogno"   (avere bisogno)
    "fame"    alone for "I'm hungry" → correct: "ho fame"    (avere fame)
    "sete"    alone for "I'm thirsty" → correct: "ho sete"   (avere sete)
    "freddo"  alone for "I'm cold"   → correct: "ho freddo"  (avere freddo)
    "caldo"   alone for "I'm hot"    → correct: "ho caldo"   (avere caldo)
    "paura"   alone for "I'm afraid" → correct: "ho paura"   (avere paura)
    "sonno"   alone for "I'm sleepy" → correct: "ho sonno"   (avere sonno)
    "fretta"  alone for "I'm in a hurry" → correct: "ho fretta" (avere fretta)
    "voglia"  alone for "I feel like" → correct: "ho voglia" (avere voglia)
    "ragione" alone for "I'm right"  → correct: "ho ragione" (avere ragione)
    "torto"   alone for "I'm wrong"  → correct: "ho torto"   (avere torto)
  Flag these with UNNATURAL, field: back_highlight, expected: the avere form.

  RARE OR ARCHAIC IMPERATIVE FORMS
  - "abbi" (tu imperative of avere) — essentially never used naturally
  - "sii" (tu imperative of essere) — vanishingly rare in modern speech
  - "vogliate" as voi imperative of volere — archaic/liturgical; use "volete"

  ARCHAIC VOCABULARY
  - "uopo" (need) — archaic; use "bisogno" or "necessità"
  - "tosto" meaning "soon" — archaic; use "presto" or "subito"
  - "dappoi" / "poscia" as alternatives to "dopo" / "poi" — literary/archaic

  Do NOT flag a form merely because a synonym exists.

For each issue return exactly:
  {
    "card_id":     <integer>,
    "issue_type":  <one of the types above>,
    "severity":    "high" | "medium" | "low",
    "field":       <field name>,
    "found":       <the wrong value>,
    "expected":    <the correct value, or "" if unsure>,
    "explanation": <one sentence>
  }

Respond with a JSON array only. No prose.
"""


# ── Database helpers ──────────────────────────────────────────────────────────

def load_cards(
    connection: sqlite3.Connection,
    deck: str | None,
    source_type: str | None,
    limit: int | None,
) -> list[sqlite3.Row]:
    conditions: list[str] = []
    params: list = []
    if deck:
        conditions.append("deck = ?")
        params.append(deck)
    if source_type:
        conditions.append("source_type = ?")
        params.append(source_type)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"""
        SELECT id, source_type, deck, front_text, front_labels,
               back_highlight, back_text, audio_text, image_text
        FROM card_items
        {where}
        ORDER BY id
    """
    if limit is not None:
        query += f" LIMIT {limit}"
    return connection.execute(query, params).fetchall()


def row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


# ── Already-audited tracking ──────────────────────────────────────────────────

def load_audited_ids(issues_file: Path) -> set[int]:
    """Parse ISSUES.txt to find card IDs already audited."""
    if not issues_file.exists():
        return set()
    ids: set[int] = set()
    for line in issues_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("card_id:"):
            try:
                ids.add(int(line.split(":", 1)[1].strip()))
            except ValueError:
                pass
    return ids


# ── AI call ───────────────────────────────────────────────────────────────────

def evaluate_card(card: dict, api_key: str, model: str = MODEL) -> list[dict]:
    """Send a single card dict to the AI. Returns list of issue dicts (usually [] or [issue])."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps([card], ensure_ascii=False)},
        ],
    }
    request = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Title": "Italian Flashcards Audit",
        },
        method="POST",
    )
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read().decode("utf-8")
            content = json.loads(body)["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            # Model may return {"issues": [...]} or just [...]
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                for key in ("issues", "results", "findings"):
                    if key in parsed and isinstance(parsed[key], list):
                        return parsed[key]
                # If it's a single issue dict, wrap it
                if "card_id" in parsed:
                    return [parsed]
            return []
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            print(f"  [card {card['id']} attempt {attempt}/{MAX_RETRIES}] Request error: {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)
        except (json.JSONDecodeError, KeyError) as exc:
            print(f"  [card {card['id']} attempt {attempt}/{MAX_RETRIES}] Parse error: {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    raise RuntimeError(f"AI request failed after {MAX_RETRIES} retries for card {card['id']}")


# ── Issue writing ─────────────────────────────────────────────────────────────

def append_issues(issues_file: Path, issues: list[dict], card_lookup: dict[int, dict]) -> None:
    """Append issue entries to ISSUES.txt."""
    lines: list[str] = []
    for issue in issues:
        card_id = issue.get("card_id")
        card = card_lookup.get(card_id, {})
        lines.append("")  # blank line before
        lines.append(f"card_id: {card_id}")
        lines.append(f"deck: {card.get('deck', '?')}")
        lines.append(f"source_type: {card.get('source_type', '?')}")
        lines.append(f"front_text: {card.get('front_text', '?')}")
        lines.append(f"back_highlight: {card.get('back_highlight', '?')}")
        lines.append(f"issue_type: {issue.get('issue_type', 'UNKNOWN')}")
        lines.append(f"severity: {issue.get('severity', '?')}")
        lines.append(f"field: {issue.get('field', '?')}")
        lines.append(f"found: {issue.get('found', '')}")
        lines.append(f"expected: {issue.get('expected', '')}")
        lines.append(f"explanation: {issue.get('explanation', '').strip()}")
        lines.append("")  # blank line after

    with issues_file.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ── Banner ────────────────────────────────────────────────────────────────────

def print_banner() -> None:
    title = "90 Audit card_items with AI"
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print_banner()
    parser = argparse.ArgumentParser(
        description="Audit card_items with AI and write issues to ISSUES.txt."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--limit", type=int, default=None,
                        help="Maximum number of cards to audit.")
    parser.add_argument("--deck", type=str, default=None,
                        help="Only audit cards from this deck.")
    parser.add_argument("--source-type", type=str, default=None,
                        help="Only audit cards with this source_type.")
    parser.add_argument("--batch", type=int, default=10,
                        help="Number of cards per AI call (default: 10).")
    parser.add_argument("--resume", action="store_true",
                        help="Skip cards whose IDs already appear in ISSUES.txt.")
    parser.add_argument("--issues-file", type=Path, default=ISSUES_FILE,
                        help="Path to the output ISSUES.txt file.")
    parser.add_argument("--workers", type=int, default=50,
                        help="Number of parallel threads (default: 50).")
    args = parser.parse_args()

    api_key = load_api_key()

    with sqlite3.connect(args.db, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = load_cards(connection, args.deck, args.source_type, args.limit)

    total = len(rows)
    print(f"Loaded {total} card_items.")

    audited_ids: set[int] = set()
    if args.resume:
        audited_ids = load_audited_ids(args.issues_file)
        print(f"Resume mode: {len(audited_ids)} card IDs already in {args.issues_file.name}.")

    cards_to_audit = [row_to_dict(r) for r in rows if r["id"] not in audited_ids]
    random.shuffle(cards_to_audit)
    skipped = total - len(cards_to_audit)
    print(f"Cards to audit: {len(cards_to_audit)}  (skipped: {skipped})")
    print(f"Workers: {args.workers}")

    if not cards_to_audit:
        print("Nothing to audit.")
        return

    write_lock = threading.Lock()
    issues_found = 0
    completed = 0
    errors = 0
    lock = threading.Lock()

    def audit_one(card: dict) -> None:
        nonlocal issues_found, completed, errors
        try:
            issues = evaluate_card(card, api_key, MODEL)
        except RuntimeError as exc:
            print(f"  [error card {card['id']}] {exc}", flush=True)
            with lock:
                errors += 1
                completed += 1
            return

        with write_lock:
            if issues:
                append_issues(args.issues_file, issues, {card["id"]: card})
        with lock:
            issues_found += len(issues)
            completed += 1
            if completed % 100 == 0 or completed == len(cards_to_audit):
                print(
                    f"  progress: {completed}/{len(cards_to_audit)}"
                    f"  issues: {issues_found}  errors: {errors}",
                    flush=True,
                )

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(audit_one, card) for card in cards_to_audit]
        for future in as_completed(futures):
            exc = future.exception()
            if exc:
                print(f"  [unhandled] {exc}", flush=True)

    print(
        f"\nDone."
        f"\n  Cards audited  : {len(cards_to_audit)}"
        f"\n  Cards skipped  : {skipped}"
        f"\n  Errors         : {errors}"
        f"\n  Issues found   : {issues_found}"
        f"\n  Issues file    : {args.issues_file}"
    )


if __name__ == "__main__":
    main()
