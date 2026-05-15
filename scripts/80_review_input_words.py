#!/usr/bin/env python3
"""Review input_words rows with AI to detect data quality issues, and clean up bad entries.

Two modes of operation:

  --cleanup   Scan the live database for word_entries that were created from
              garbage input_words rows (single-letter lemmas, NPR-sourced nouns,
              non-letter characters in dom_lemma) and delete them together with
              all cascading noun_phrases, card_items, and anki_cards.
              Run this first to purge already-deployed bad data.

  (default)   Read every row from input_words (ignoring columns that start with
              "all"), apply local heuristics to catch obvious problems instantly,
              then send ambiguous rows to an AI model for deeper evaluation.
              Detected issues are appended to ISSUES.md immediately after each
              evaluation so progress is preserved if the run is interrupted.

Typical issues caught:
- Single letters or symbols classified as NOM (e.g. "c", "x", "|")
- Truncated / garbage lemmas (e.g. "<unknown>", "dell", "ragazza|ragazzo")
- Italian interjections classified as nouns (e.g. "salve", "beh", "mah")
- Proper names classified as NOM instead of NPR
- Foreign words / abbreviations mis-tagged as nouns
- dom_pos that is clearly wrong for the wordform
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

from common import DEFAULT_DB_PATH, load_api_key

PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_KEY_FILE = PROJECT_ROOT / ".openrouter"
ISSUES_FILE = PROJECT_ROOT / "ISSUES.md"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "~google/gemini-flash-latest"

MAX_RETRIES = 3
RETRY_DELAY = 5

# Only these pos tags can produce flashcard content — reviewing others is lower value
FLASHCARD_POS = {"NOM", "NPR", "VER", "ADJ", "ADV", "INT"}

# Columns to exclude when sending rows to the AI (start with "all" plus metadata)
EXCLUDED_COLUMNS = {"created_at"}

# ── Local heuristic patterns ──────────────────────────────────────────────────

# Italian interjections that SUBTLEX-IT sometimes misclassifies as NOM
ITALIAN_INTERJECTIONS: set[str] = {
    "ah", "ahi", "ahimè", "bah", "beh", "boh", "caspita", "cavolo", "che",
    "ci", "ciao", "cribbio", "dai", "diamine", "ecco", "eh", "ehi", "embè",
    "grazie", "guarda", "guardi", "insomma", "ma", "mah", "mamma", "meno",
    "mh", "mmh", "no", "oh", "ohi", "ohimè", "ok", "okay", "ops", "perbacco",
    "perdiana", "porco", "prego", "presto", "pure", "salve", "sì", "su",
    "toh", "uff", "uffa", "va", "vabbè", "vabbene", "via", "wow",
}

# Known Italian abbreviations that sometimes land in NOM
ITALIAN_ABBREVIATIONS: set[str] = {
    "cc", "cd", "cm", "dna", "doc", "dvd", "ecc", "etc", "gb", "gps",
    "km", "mb", "ml", "mm", "mp", "pc", "pdf", "rna", "sec", "tel",
    "tv", "url", "usb", "vhs",
}


def _all_letters(text: str) -> bool:
    """Return True if every character in *text* is a Unicode letter or combining mark."""
    return all(unicodedata.category(ch).startswith(("L", "M")) for ch in text)


# ── Database cleanup ──────────────────────────────────────────────────────────

def _is_garbage_entry(we_lemma: str, we_word_type: str, dom_lemma: str, dom_pos: str) -> tuple[bool, str]:
    """
    Return (True, reason) when a word_entry should be deleted from the database.

    Rules:
    - The resolved word_entries.lemma is a single letter → garbage regardless of source
    - The resolved word_entries.lemma contains non-letter characters (e.g. 'h.', 'parte|parto')
      → the AI failed to resolve a clean lemma from a pipe-separated source
    - The source input_words row has dom_pos=NPR and the entry became a noun
      → proper nouns should not be flashcard nouns (fix #4/#7)

    Note: we do NOT delete entries where only the source dom_lemma was pipe-separated
    but the AI correctly resolved a clean Italian noun lemma (e.g. dom_lemma='ragazza|ragazzo'
    → lemma='ragazza'). Those entries are legitimate.
    """
    if not we_lemma:
        return True, "empty lemma in word_entries"
    # Single letter that became a word_entry lemma
    if len(we_lemma) == 1 and unicodedata.category(we_lemma[0]) in ("Lu", "Ll"):
        return True, f"single-letter lemma {we_lemma!r}"
    # Lemma itself contains non-letter characters — AI didn't clean it up
    if not _all_letters(we_lemma):
        return True, f"non-letter character in resolved lemma {we_lemma!r}"
    # NPR-sourced noun entries
    if dom_pos == "NPR" and we_word_type == "noun":
        return True, "NPR dom_pos — proper noun should not be a flashcard noun"
    return False, ""


def run_cleanup(connection: sqlite3.Connection, dry_run: bool = False) -> None:
    """
    Delete all word_entries (and their cascading rows) that were created from
    garbage input_words rows: single-letter lemmas, non-letter dom_lemma chars,
    or NPR-sourced nouns.

    Deletion order (respecting foreign keys):
      1. anki_cards   — via card_items FK (CASCADE)
      2. card_items   — manually deleted by source_type/source_id
      3. noun_phrases — via word_entries FK (CASCADE)
      4. word_entries — root deletion
    """
    connection.execute("PRAGMA foreign_keys = ON")

    rows = connection.execute(
        """
        SELECT we.id, we.lemma, we.word_type, iw.dom_lemma, iw.dom_pos, iw.frequency_rank
        FROM word_entries we
        JOIN input_words iw ON we.input_word_id = iw.id
        ORDER BY iw.frequency_rank
        """
    ).fetchall()

    to_delete: list[tuple[str, str, str]] = []  # (id, lemma, reason)
    for row in rows:
        bad, reason = _is_garbage_entry(
            row["lemma"] or "", row["word_type"] or "",
            row["dom_lemma"] or "", row["dom_pos"] or "",
        )
        if bad:
            to_delete.append((row["id"], row["lemma"], reason))

    if not to_delete:
        print("Cleanup: nothing to delete.")
        return

    print(f"Cleanup: found {len(to_delete)} word_entr{'y' if len(to_delete)==1 else 'ies'} to remove:")

    total_np = total_ci = total_ac = 0
    for we_id, lemma, reason in to_delete:
        np_ids = [
            r["id"] for r in connection.execute(
                "SELECT id FROM noun_phrases WHERE word_entry_id = ?", (we_id,)
            ).fetchall()
        ]
        ci_ids = [
            r["id"] for r in connection.execute(
                "SELECT id FROM card_items WHERE source_type = 'noun_phrase' AND source_id IN ({})".format(
                    ",".join("?" * len(np_ids))
                ),
                np_ids,
            ).fetchall()
        ] if np_ids else []
        # also infinitive_verb card_items directly referencing word_entry
        ci_ids += [
            r["id"] for r in connection.execute(
                "SELECT id FROM card_items WHERE source_type = 'infinitive_verb' AND source_id = ?",
                (we_id,),
            ).fetchall()
        ]
        ac_count = 0
        if ci_ids:
            ac_count = connection.execute(
                "SELECT COUNT(*) as n FROM anki_cards WHERE card_item_id IN ({})".format(
                    ",".join("?" * len(ci_ids))
                ),
                ci_ids,
            ).fetchone()["n"]

        total_np += len(np_ids)
        total_ci += len(ci_ids)
        total_ac += ac_count

        print(
            f"  {'[dry]' if dry_run else '[del]'} lemma={lemma!r:<20} "
            f"noun_phrases={len(np_ids):>3}  card_items={len(ci_ids):>3}  "
            f"anki_cards={ac_count:>3}  — {reason}"
        )

        if not dry_run:
            # Delete anki_cards via cascade when card_items are deleted
            if ci_ids:
                connection.execute(
                    "DELETE FROM card_items WHERE id IN ({})".format(
                        ",".join("?" * len(ci_ids))
                    ),
                    ci_ids,
                )
            # noun_phrases cascade from word_entries; delete word_entry last
            connection.execute("DELETE FROM word_entries WHERE id = ?", (we_id,))

    if not dry_run:
        connection.commit()
        print(
            f"\nCleanup complete."
            f"\n  word_entries deleted : {len(to_delete)}"
            f"\n  noun_phrases deleted : {total_np}  (via CASCADE)"
            f"\n  card_items deleted   : {total_ci}"
            f"\n  anki_cards deleted   : {total_ac}  (via CASCADE)"
        )
    else:
        print(
            f"\nDry-run summary — would delete:"
            f"\n  word_entries : {len(to_delete)}"
            f"\n  noun_phrases : {total_np}"
            f"\n  card_items   : {total_ci}"
            f"\n  anki_cards   : {total_ac}"
        )


def local_heuristic(row_data: dict) -> dict | None:
    """
    Apply fast local checks to a row.

    Returns a result dict (same shape as the AI response) if an issue is
    confidently detected, or None if the row should be sent to the AI.
    """
    wordform: str = (row_data.get("wordform") or "").strip()
    dom_lemma: str = (row_data.get("dom_lemma") or "").strip()
    dom_pos: str = (row_data.get("dom_pos") or "").strip()

    def issue(issue_type: str, severity: str, explanation: str) -> dict:
        return {
            "has_issue": True,
            "issue_type": issue_type,
            "severity": severity,
            "explanation": explanation,
            "source": "heuristic",
        }

    # ── 1. Single-character letter lemma tagged as NOM/NPR ────────────────
    if dom_pos in ("NOM", "NPR") and len(dom_lemma) == 1 and _all_letters(dom_lemma):
        return issue(
            "SINGLE_LETTER",
            "high",
            f"dom_lemma {dom_lemma!r} is a single letter, not a noun.",
        )

    # ── 2. Garbage / unknown lemma tagged as NOM/NPR ─────────────────────
    if dom_pos in ("NOM", "NPR") and (
        dom_lemma in ("<unknown>", "") or not _all_letters(dom_lemma)
    ):
        return issue(
            "GARBAGE_LEMMA",
            "high",
            f"dom_lemma {dom_lemma!r} contains non-letter characters or is unknown — not a usable noun.",
        )

    # ── 3. Known Italian interjection tagged as NOM ───────────────────────
    if dom_pos == "NOM" and dom_lemma.lower() in ITALIAN_INTERJECTIONS:
        return issue(
            "INTERJECTION",
            "medium",
            f"{dom_lemma!r} is an Italian interjection but is tagged as NOM.",
        )

    # ── 4. Known abbreviation tagged as NOM ──────────────────────────────
    if dom_pos == "NOM" and dom_lemma.lower() in ITALIAN_ABBREVIATIONS:
        return issue(
            "ABBREVIATION",
            "medium",
            f"{dom_lemma!r} is an abbreviation but is tagged as NOM instead of ABR.",
        )

    # Row is not obviously broken — defer to AI
    return None


# ── AI evaluation ─────────────────────────────────────────────────────────────

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "has_issue": {
            "type": "boolean",
            "description": "True if a data quality issue is detected.",
        },
        "issue_type": {
            "type": "string",
            "description": (
                "Short category label, e.g. WRONG_POS, PROPER_NOUN, SINGLE_LETTER, "
                "FOREIGN_WORD, INTERJECTION, ABBREVIATION, GARBAGE_LEMMA, "
                "NON_ITALIAN, or NONE if no issue."
            ),
        },
        "severity": {
            "type": "string",
            "enum": ["high", "medium", "low", "none"],
            "description": "Severity of the issue.",
        },
        "explanation": {
            "type": "string",
            "description": "One-sentence explanation of the issue, or empty string if none.",
        },
    },
    "required": ["has_issue", "issue_type", "severity", "explanation"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You are a data quality reviewer for an Italian language corpus (SUBTLEX-IT).
You will receive one row from the input_words table. Your job is to detect
genuine data quality issues that would cause bad Italian vocabulary flashcards
to be generated.

Known pos tags: NOM (noun), VER (verb), ADJ (adjective), NPR (proper noun),
ADV (adverb), NUM (numeral), PRO (pronoun), PRE (preposition), CON (conjunction),
INT (interjection), ABR (abbreviation), PON (punctuation), DET (determiner),
SYM (symbol), FW (foreign word), SENT (sentence boundary).

Flag a row as having an issue if ANY of these apply:
- An Italian interjection (e.g. salve, beh, mah, eh, oh, ahi, ciao, grazie,
  prego, dai, uff, ehm) is tagged as NOM instead of INT
- A clear proper noun (personal name, city, country, brand) is tagged NOM
- A foreign word or loanword that has no established Italian usage is tagged NOM
- An abbreviation (ecc, etc, km, tv, dna, dvd) is tagged NOM instead of ABR
- dom_pos is clearly wrong for the wordform in any other way
- Any other misclassification that would produce a nonsensical flashcard

Do NOT flag:
- Legitimate Italian common nouns, even short or informal ones (e.g. re, po,
  dì, tè, bar, oro, era, via, zio, gas, ago, ape, zoo)
- Rows where dom_pos is already INT, ABR, FW, SYM, PON, SENT, NUM — these are
  harmless and will not generate flashcards
- Minor ambiguities where the word is genuinely both a noun and another class
- Words with foreign origin that are established Italian nouns (e.g. sport,
  film, computer, stress, hotel, manager)

Respond with JSON only.\
"""


def row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a Row to a dict, dropping columns that start with 'all' or are excluded."""
    return {
        k: row[k]
        for k in row.keys()
        if not k.startswith("all") and k not in EXCLUDED_COLUMNS
    }


def evaluate_row_ai(row_data: dict, api_key: str) -> dict:
    """Send a single input_words row to the AI for evaluation. Returns parsed JSON."""
    user_content = json.dumps(row_data, ensure_ascii=False)
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "input_word_review",
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
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
            content = json.loads(body)["choices"][0]["message"]["content"]
            return json.loads(content)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            print(f"  [attempt {attempt}/{MAX_RETRIES}] Request error: {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)
        except (json.JSONDecodeError, KeyError) as exc:
            print(f"  [attempt {attempt}/{MAX_RETRIES}] Parse error: {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    raise RuntimeError(f"AI request failed after {MAX_RETRIES} retries")


def append_issue(issues_file: Path, row_data: dict, result: dict) -> None:
    """Append a single issue entry to ISSUES.md."""
    wordform = row_data.get("wordform", "?")
    dom_pos = row_data.get("dom_pos", "?")
    dom_lemma = row_data.get("dom_lemma", "?")
    frequency_rank = row_data.get("frequency_rank", "?")
    row_id = row_data.get("id", "?")
    issue_type = result.get("issue_type", "UNKNOWN")
    severity = result.get("severity", "unknown")
    explanation = result.get("explanation", "").strip()
    source = result.get("source", "ai")

    entry = (
        f"## `{wordform}` (rank {frequency_rank})\n\n"
        f"- **ID:** `{row_id}`\n"
        f"- **dom_pos:** `{dom_pos}`\n"
        f"- **dom_lemma:** `{dom_lemma}`\n"
        f"- **Issue type:** `{issue_type}`\n"
        f"- **Severity:** {severity}\n"
        f"- **Detected by:** {source}\n"
        f"- **Explanation:** {explanation}\n\n"
        f"---\n\n"
    )

    # Write header if file doesn't exist yet
    if not issues_file.exists():
        issues_file.write_text(
            "# Input Words Data Quality Issues\n\n"
            "Generated by `scripts/88_review_input_words.py`.\n\n"
            "---\n\n",
            encoding="utf-8",
        )

    with issues_file.open("a", encoding="utf-8") as f:
        f.write(entry)


def load_reviewed_ids(issues_file: Path) -> set[str]:
    """Parse ISSUES.md to find row IDs already reviewed (to support resume)."""
    if not issues_file.exists():
        return set()
    ids: set[str] = set()
    for line in issues_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("- **ID:** `"):
            row_id = line.removeprefix("- **ID:** `").removesuffix("`").strip()
            if row_id:
                ids.add(row_id)
    return ids


def load_rows(connection: sqlite3.Connection, limit: int | None) -> list[sqlite3.Row]:
    query = """
        SELECT *
        FROM input_words
        ORDER BY frequency_rank, id
    """
    if limit is not None:
        query += f" LIMIT {limit}"
    return connection.execute(query).fetchall()


def print_banner() -> None:
    title = "89 Review input_words"
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


def main() -> None:
    print_banner()
    parser = argparse.ArgumentParser(
        description="Review input_words rows and write data quality issues to ISSUES.md."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of rows to review (default: all).",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip rows whose IDs already appear in ISSUES.md.",
    )
    parser.add_argument(
        "--issues-file",
        type=Path,
        default=ISSUES_FILE,
        help="Path to the output ISSUES.md file.",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help=(
            "Delete word_entries (and all cascading rows) that were created from "
            "garbage input_words rows: single-letter lemmas, non-letter characters "
            "in dom_lemma, or NPR-sourced nouns. Use --dry-run to preview first."
        ),
    )
    parser.add_argument(
        "--heuristic-only",
        action="store_true",
        help="Run local heuristics only — do not call the AI.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Preview what would be deleted (with --cleanup) or reviewed "
            "(without --cleanup) without writing anything."
        ),
    )
    args = parser.parse_args()

    # ── Cleanup mode ─────────────────────────────────────────────────────────
    if args.cleanup:
        with sqlite3.connect(args.db, timeout=30) as connection:
            connection.row_factory = sqlite3.Row
            run_cleanup(connection, dry_run=args.dry_run)
        return

    # ── Review mode ──────────────────────────────────────────────────────────
    if not args.heuristic_only:
        api_key = load_api_key(API_KEY_FILE)
    else:
        api_key = ""

    with sqlite3.connect(args.db, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = load_rows(connection, args.limit)

    total = len(rows)
    print(f"Loaded {total} rows from input_words.")

    reviewed_ids: set[str] = set()
    if args.resume:
        reviewed_ids = load_reviewed_ids(args.issues_file)
        print(f"Resume mode: {len(reviewed_ids)} rows already have entries in {args.issues_file.name}.")

    if args.dry_run:
        print("\n[dry-run] Rows that would be reviewed:")
        shown = 0
        for row in rows:
            row_data = row_to_dict(row)
            if args.resume and row_data["id"] in reviewed_ids:
                continue
            dom_pos = row_data.get("dom_pos", "")
            if dom_pos not in FLASHCARD_POS:
                continue
            h = local_heuristic(row_data)
            tag = f"[heuristic: {h['issue_type']}]" if h else "[→ ai]"
            print(
                f"  rank={row_data.get('frequency_rank'):>6}  "
                f"wordform={row_data.get('wordform')!r:<20}  "
                f"dom_pos={dom_pos:<5}  {tag}"
            )
            shown += 1
        print(f"\n{shown} rows would be reviewed.")
        return

    issues_found = 0
    heuristic_hits = 0
    ai_calls = 0
    skipped = 0
    errors = 0

    for idx, row in enumerate(rows, start=1):
        row_data = row_to_dict(row)
        row_id = row_data["id"]
        wordform = row_data.get("wordform", "?")
        dom_pos = row_data.get("dom_pos", "?")

        if args.resume and row_id in reviewed_ids:
            skipped += 1
            continue

        # Only rows whose dom_pos can produce flashcards are worth examining
        if dom_pos not in FLASHCARD_POS:
            continue

        print(
            f"[{idx}/{total}] rank={row_data.get('frequency_rank'):>6}  "
            f"wordform={wordform!r:<20}  dom_pos={dom_pos}",
            end="  ",
            flush=True,
        )

        # ── Fast local heuristics first ──────────────────────────────────
        result = local_heuristic(row_data)
        if result is not None:
            print(f"[heuristic: {result['issue_type']} / {result['severity']}]")
            append_issue(args.issues_file, row_data, result)
            issues_found += 1
            heuristic_hits += 1
            continue

        # ── Skip AI call if heuristic-only mode ─────────────────────────
        if args.heuristic_only:
            print("[ok]")
            continue

        # ── Send to AI for deeper evaluation ────────────────────────────
        try:
            result = evaluate_row_ai(row_data, api_key)
            ai_calls += 1
        except RuntimeError as exc:
            print(f"[error] {exc}")
            errors += 1
            continue

        if result.get("has_issue"):
            issue_type = result.get("issue_type", "UNKNOWN")
            severity = result.get("severity", "?")
            print(f"[ai: {issue_type} / {severity}]")
            result["source"] = "ai"
            append_issue(args.issues_file, row_data, result)
            issues_found += 1
        else:
            print("[ok]")

    print(
        f"\nDone."
        f"\n  Rows reviewed     : {total - skipped}"
        f"\n  Rows skipped      : {skipped}"
        f"\n  Heuristic hits    : {heuristic_hits}"
        f"\n  AI calls made     : {ai_calls}"
        f"\n  Errors            : {errors}"
        f"\n  Issues found      : {issues_found}"
        f"\n  Issues file       : {args.issues_file}"
    )


if __name__ == "__main__":
    main()
