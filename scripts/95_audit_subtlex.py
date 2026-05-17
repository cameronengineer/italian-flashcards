#!/usr/bin/env python3
"""Audit and clean SUBTLEX-IT CSV rows using AI.

Rows with dom_pos in {PON, SENT, NUM, NPR} are excluded from the output
without being sent to the AI (punctuation, sentence markers, numbers, and
proper nouns are not useful for an Italian vocabulary list).

For all remaining rows, sends the core fields (wordform, freq_count, zipf,
cd_count, dom_pos, dom_lemma, dom_lemma_freq, id) to an AI model.
The 'all_*' columns are never sent to the AI and are omitted from the output.

The AI returns one of three actions:
  ok   — row is correct; keep as-is
  bad  — row is garbage, nonsensical, or a foreign word unused in Italian;
         freq_count is set to 0
  fix  — row has correctable errors in dom_pos / dom_lemma; patch in place

Results are written to --output (default: freqdic/subtlex-it.cleaned.csv).
Use --in-place to overwrite the source file.
Use --resume to skip rows whose IDs are recorded in the progress sidecar.

Usage:
    python scripts/95_audit_subtlex.py
    python scripts/95_audit_subtlex.py --limit 500
    python scripts/95_audit_subtlex.py --workers 100
    python scripts/95_audit_subtlex.py --in-place
    python scripts/95_audit_subtlex.py --resume
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from common import load_api_key

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CSV_PATH = PROJECT_ROOT / "freqdic" / "subtlex-it.csv"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "freqdic" / "subtlex-it.cleaned.csv"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

MODEL = "openai/gpt-5-nano"

MAX_RETRIES = 3
RETRY_DELAY = 5

CSV_ENCODING = "cp1252"
OUTPUT_ENCODING = "utf-8"
CSV_DELIMITER = ";"

# Columns sent to the AI and written to the output
CORE_FIELDS = ["wordform", "freq_count", "zipf", "cd_count",
               "dom_pos", "dom_lemma", "id"]

# Rows with these dom_pos values are excluded from the output without AI review
EXCLUDED_POS = {"PON", "SENT", "NUM", "NPR"}

# ── Prompt ────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are auditing a row from SUBTLEX-IT, an Italian word frequency dictionary
derived from film and TV subtitles. The goal is a clean Italian vocabulary list,
so rows that are not useful Italian vocabulary must be zeroed out.

You will receive a JSON object with these fields:
  wordform       - the Italian token as it appears in text
  freq_count     - how many times it appears in the corpus
  zipf           - Zipf frequency score (log scale, higher = more frequent)
  cd_count       - contextual diversity (number of films/shows containing it)
  dom_pos        - dominant part-of-speech tag
  dom_lemma      - dominant lemma (base/dictionary form)
  id             - row identifier

POS tags used in SUBTLEX-IT:
  NOM  noun             VER  verb            ADJ  adjective
  ADV  adverb           PRE  preposition     DET  determiner / article
  PRO  pronoun          CON  conjunction     NUM  numeral / cardinal
  NPR  proper noun      ABR  abbreviation    INT  interjection
  PON  punctuation      SENT sentence mark   SYM  symbol
  FW   foreign word

Special lemma values:
  @card@     standard lemma for numeric cardinals (e.g. "1", "42")
  <unknown>  tagger could not identify the lemma — not an error by itself

━━━ MANDATORY EXCLUSIONS — always return "bad" for these ━━━

The following categories are not useful Italian vocabulary and must be zeroed:

1. PUNCTUATION & SENTENCE MARKERS (dom_pos = PON or SENT)
   e.g. ".", ",", "!", "?", "..." → bad

2. NUMBERS (dom_pos = NUM, or wordform is purely digits / digit strings)
   e.g. "1", "42", "2019", "1.000" → bad

3. PROPER NOUNS / NAMES (dom_pos = NPR)
   e.g. "Roma", "Mario", "Ferrari", "Netflix" → bad

4. FOREIGN WORDS NOT ADOPTED INTO ITALIAN (dom_pos = FW, or clearly foreign)
   Mark as bad if the word is a foreign word that Italians do not actually use
   in everyday speech or writing. Examples that are bad:
     "the", "of", "and", "yes", "hello", "sorry" (English words with Italian
      equivalents that would always be used instead)
     "bonjour", "merci", "danke", "hola" (other foreign languages)
   Mark as OK if the foreign word is genuinely integrated into Italian usage:
     "ok", "no", "computer", "internet", "email", "sport", "film", "hotel",
     "stress", "manager", "week-end", "gay", "chat", "web", "tablet" ✓

━━━ WHAT ELSE TO CHECK ━━━

5. Is the wordform otherwise useless?
   Mark bad if:
   - empty or blank
   - encoding artefact (garbled characters, replacement character □)
   - nonsensical hyphenated string (e.g. "pa-pa-bap-pa-parababap-bap")
   - random ASCII noise with no plausible linguistic identity

6. Does dom_pos correctly describe the wordform?
   If wrong and you are certain of the correct tag, return "fix".

7. Does dom_lemma correctly give the base/dictionary form?
   For verbs: infinitive. For nouns/adjectives: masculine singular.
   If wrong and you are certain, return "fix".

━━━ RESPONSE FORMAT ━━━

Respond with a single JSON object — no prose.

If the row is correct:
  {"action": "ok"}

If the row must be zeroed out:
  {"action": "bad", "reason": "<one short sentence>"}

If the row has fixable metadata errors:
  {
    "action": "fix",
    "dom_pos": "<corrected tag or null if unchanged>",
    "dom_lemma": "<corrected lemma or null if unchanged>",
    "reason": "<one short sentence>"
  }

Only return "fix" if you are certain. When in doubt, return "ok".
Never return "fix" with both dom_pos and dom_lemma null.
"""

# ── CSV helpers ───────────────────────────────────────────────────────────────

def detect_encoding(csv_path: Path) -> str:
    """Return 'utf-8' if the file is valid UTF-8, otherwise fall back to cp1252."""
    try:
        csv_path.read_bytes().decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return CSV_ENCODING


def load_csv(csv_path: Path) -> tuple[list[str], list[dict], str]:
    """Return (fieldnames, rows, encoding) from the CSV. Rows are plain dicts."""
    encoding = detect_encoding(csv_path)
    with csv_path.open(newline="", encoding=encoding) as f:
        reader = csv.DictReader(f, delimiter=CSV_DELIMITER)
        fieldnames = list(reader.fieldnames or [])
        rows = [dict(r) for r in reader]
    return fieldnames, rows, encoding


def output_fieldnames(fieldnames: list[str]) -> list[str]:
    """Strip all_* columns from fieldnames for the output file."""
    return [f for f in fieldnames if not f.startswith("all_")]


def write_csv(output_path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    """Write all rows to the output CSV, stripping all_* columns."""
    out_fields = output_fieldnames(fieldnames)
    with output_path.open("w", newline="", encoding=OUTPUT_ENCODING) as f:
        writer = csv.DictWriter(f, fieldnames=out_fields, delimiter=CSV_DELIMITER,
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def load_progress(progress_path: Path) -> set[str]:
    """Return the set of row IDs already processed, from the sidecar JSON."""
    if not progress_path.exists():
        return set()
    try:
        return set(json.loads(progress_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return set()


def save_progress(progress_path: Path, done_ids: set[str]) -> None:
    """Persist the current set of done IDs to the sidecar JSON."""
    progress_path.write_text(
        json.dumps(sorted(done_ids), ensure_ascii=False),
        encoding="utf-8",
    )


# ── AI call ───────────────────────────────────────────────────────────────────

def evaluate_row(row: dict, api_key: str) -> dict:
    """Send one row's core fields to the AI. Returns the parsed action dict."""
    core = {k: row[k] for k in CORE_FIELDS if k in row}
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(core, ensure_ascii=False)},
        ],
    }
    request = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Title": "SUBTLEX-IT Audit",
        },
        method="POST",
    )
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read().decode("utf-8")
            content = json.loads(body)["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            if isinstance(parsed, dict) and "action" in parsed:
                return parsed
            return {"action": "ok"}
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            print(f"  [row {row.get('id')} attempt {attempt}/{MAX_RETRIES}] Request error: {exc}",
                  flush=True)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)
        except (json.JSONDecodeError, KeyError) as exc:
            print(f"  [row {row.get('id')} attempt {attempt}/{MAX_RETRIES}] Parse error: {exc}",
                  flush=True)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    raise RuntimeError(f"AI request failed after {MAX_RETRIES} retries for row {row.get('id')}")


# ── Banner ────────────────────────────────────────────────────────────────────

def print_banner() -> None:
    title = "95 Audit SUBTLEX-IT rows with AI"
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print_banner()
    parser = argparse.ArgumentParser(
        description="Audit and clean SUBTLEX-IT CSV rows using AI."
    )
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV_PATH,
                        help="Path to the source CSV file.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH,
                        help="Path to the output CSV file.")
    parser.add_argument("--in-place", action="store_true",
                        help="Overwrite the source CSV file.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Maximum number of rows to process.")
    parser.add_argument("--workers", type=int, default=50,
                        help="Number of parallel threads (default: 50).")
    parser.add_argument("--resume", action="store_true",
                        help="Skip rows whose IDs are already in the progress sidecar.")
    args = parser.parse_args()

    output_path = args.csv if args.in_place else args.output
    progress_path = output_path.with_suffix(".progress.json")

    api_key = load_api_key()

    fieldnames, rows, input_encoding = load_csv(args.csv)
    print(f"Loaded {len(rows)} rows from {args.csv} (encoding: {input_encoding}).")

    # Zero out excluded POS rows immediately without sending to AI
    count_excluded = 0
    for row in rows:
        if row.get("dom_pos") in EXCLUDED_POS:
            row["freq_count"] = "0"
            count_excluded += 1
    print(f"Pre-zeroed {count_excluded} rows (PON/SENT/NUM/NPR).")

    # Resume: skip already-done IDs
    done_ids: set[str] = set()
    if args.resume:
        done_ids = load_progress(progress_path)
        print(f"Resume: {len(done_ids)} rows already processed.")

    # Only send non-excluded rows to AI; shuffle for broad coverage
    indices = [
        i for i, r in enumerate(rows)
        if r.get("dom_pos") not in EXCLUDED_POS
        and r.get("freq_count", "0") != "0"
        and r.get("id") not in done_ids
    ]
    random.shuffle(indices)
    if args.limit is not None:
        indices = indices[: args.limit]

    print(f"Rows to audit: {len(indices)}  (skipped/resumed: {len(rows) - len(indices)})")
    print(f"Workers: {args.workers}")

    if not indices:
        print("Nothing to audit.")
        return

    # Counters and shared state — all protected by counters_lock
    count_ok = 0
    count_bad = 0
    count_fix = 0
    count_error = 0
    completed = 0
    counters_lock = threading.Lock()

    def audit_one(idx: int) -> None:
        nonlocal count_ok, count_bad, count_fix, count_error, completed
        row = rows[idx]
        try:
            result = evaluate_row(row, api_key)
        except RuntimeError as exc:
            print(f"  [error] {exc}", flush=True)
            with counters_lock:
                count_error += 1
                completed += 1
            return

        action = result.get("action", "ok")

        if action == "bad":
            rows[idx]["freq_count"] = "0"
        elif action == "fix":
            if result.get("dom_pos"):
                rows[idx]["dom_pos"] = unicodedata.normalize("NFC", result["dom_pos"])
            if result.get("dom_lemma"):
                rows[idx]["dom_lemma"] = unicodedata.normalize("NFC", result["dom_lemma"])

        reason = result.get("reason", "")
        wordform = row.get("wordform", "")

        with counters_lock:
            if action == "bad":
                count_bad += 1
                print(f"  [BAD]  {wordform!r:30s}  {reason}", flush=True)
            elif action == "fix":
                count_fix += 1
                print(f"  [FIX]  {wordform!r:30s}  {reason}", flush=True)
            else:
                count_ok += 1
            done_ids.add(row.get("id", ""))
            completed += 1
            if completed % 200 == 0 or completed == len(indices):
                print(
                    f"  progress: {completed}/{len(indices)}"
                    f"  ok:{count_ok}  fix:{count_fix}  bad:{count_bad}"
                    f"  errors:{count_error}",
                    flush=True,
                )
            save_progress(progress_path, done_ids)

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(audit_one, idx) for idx in indices]
        for future in as_completed(futures):
            exc = future.exception()
            if exc:
                print(f"  [unhandled] {exc}", flush=True)

    print(f"\nWriting output to {output_path} …", flush=True)
    write_csv(output_path, fieldnames, rows)

    print(
        f"\nDone."
        f"\n  Rows total        : {len(rows)}"
        f"\n  Pre-zeroed        : {count_excluded}  (PON/SENT/NUM/NPR)"
        f"\n  Rows audited      : {len(indices)}"
        f"\n  ok                : {count_ok}"
        f"\n  fixed             : {count_fix}"
        f"\n  bad (zeroed by AI): {count_bad}"
        f"\n  errors            : {count_error}"
        f"\n  Output            : {output_path}"
        f"\n  Progress          : {progress_path}"
    )


if __name__ == "__main__":
    main()
