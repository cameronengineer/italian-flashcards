#!/usr/bin/env python3
"""Suspend and tag leech cards in Anki — including cards stuck in learning.

Anki's built-in leech detection only fires for REVIEW cards (cards that have
already graduated from learning).  If you keep failing a card while it is
still in the learning queue, Anki never marks it as a leech, regardless of
how many times you press "Again".

This script fills that gap and also fixes a subtle problem with Anki's own
leech counter: `lapses` is cumulative and never resets, so a card that was
failed many times years ago but has since been re-learned remains flagged
forever.

Instead of using `lapses` or `reps`, this script inspects the **actual review
history** for every card and counts the number of consecutive "Again" presses
at the tail — i.e., failures since the last successful answer.  A card is
only leached if it has failed >= THRESHOLD times IN A ROW without a single
success in between.  One "Good" or "Easy" press resets the counter to zero.

This applies equally to learning cards (type=1) and review/re-learning cards
(type=2/3), so a card stuck in learning is caught just as reliably as a card
that keeps lapsing in review.

Cards meeting the condition are:
  - Tagged with the "leech" tag on their parent note
  - Suspended (queue → -1)

The script is safe to re-run: already-suspended cards are re-tagged if
needed but not double-suspended.

Deck config (--update-config flag)
  lapse.leechFails   = THRESHOLD  (Anki suspends future review leeches)
  lapse.leechAction  = 0          (0 = Suspend Card)
  lapse.delays       = [10]       (10-minute relearning step)

Requires:
  - Anki running with the AnkiConnect add-on enabled (default port 8765)

Usage:
  python scripts/92_leech_cards.py
  python scripts/92_leech_cards.py --deck "Italian - Nouns"
  python scripts/92_leech_cards.py --threshold 8
  python scripts/92_leech_cards.py --update-config
  python scripts/92_leech_cards.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

ANKI_CONNECT_URL = "http://127.0.0.1:8765"
ANKI_CONNECT_VERSION = 6

DEFAULT_THRESHOLD = 5       # failed attempts before a card is a leech
DEFAULT_RELEARN_STEPS = [10]  # minutes
LEECH_TAG = "leech"

# All Italian decks this pipeline manages.
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


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def fetch_cards_info(deck_name: str) -> list[dict]:
    """Return cardsInfo for every non-new card in *deck_name*."""
    escaped = deck_name.replace('"', '\\"')
    # Fetch all cards that are not brand-new (type>0 covers learning,
    # review, relearning, and already-suspended cards).
    card_ids: list[int] = invoke("findCards", query=f'deck:"{escaped}" -is:new')
    if not card_ids:
        return []

    cards_info: list[dict] = []
    for chunk in chunked(card_ids, 500):
        cards_info.extend(invoke("cardsInfo", cards=chunk))

    return [c for c in cards_info if c]  # drop empty dicts (unknown card IDs)


def fetch_review_history(deck_name: str) -> dict[int, list[tuple[int, int]]]:
    """Return per-card review history for *deck_name*.

    Returns {card_id: [(timestamp_ms, button_pressed), ...]} sorted oldest-first.
    button_pressed: 1=Again, 2=Hard, 3=Good, 4=Easy

    Uses AnkiConnect cardReviews which returns rows of:
      [reviewTime, cardID, usn, buttonPressed, newInterval,
       previousInterval, newFactor, reviewDuration, reviewType]
    """
    reviews = invoke("cardReviews", deck=deck_name, startID=0)
    history: dict[int, list[tuple[int, int]]] = {}
    for entry in reviews:
        timestamp, card_id, _usn, button = entry[0], entry[1], entry[2], entry[3]
        history.setdefault(card_id, []).append((timestamp, button))
    for card_id in history:
        history[card_id].sort()
    return history


def failures_since_last_success(reviews: list[tuple[int, int]]) -> int:
    """Count consecutive Again (button=1) presses at the tail of review history.

    Walking backwards through the history, count every "Again" until a
    non-Again press (Good/Hard/Easy) is found.  One success resets the count
    to zero, so a card that was failed 20 times years ago but has since been
    answered correctly will return a low number reflecting only recent failures.
    """
    count = 0
    for _ts, button in reversed(reviews):
        if button == 1:  # Again
            count += 1
        else:
            break
    return count


def process_deck(deck_name: str, threshold: int, dry_run: bool) -> dict:
    """Tag and suspend leech cards in one deck.

    Returns stats dict with keys:
      learning_leeches, review_leeches, already_suspended,
      newly_suspended, tagged_notes, suspended_non_leech.
    """
    stats: dict[str, int] = {
        "learning_leeches": 0,
        "review_leeches": 0,
        "already_suspended": 0,
        "newly_suspended": 0,
        "tagged_notes": 0,
        "suspended_non_leech": 0,
    }

    cards_info = fetch_cards_info(deck_name)
    if not cards_info:
        return stats

    # Fetch the full review history for the deck in one API call so we can
    # count consecutive failures at the tail rather than relying on the
    # cumulative lapses/reps counters (which never reset on success).
    review_history = fetch_review_history(deck_name)

    leech_cards: list[dict] = []
    for card in cards_info:
        card_id = card.get("cardId")
        history = review_history.get(card_id, [])
        fail_count = failures_since_last_success(history)

        if fail_count < threshold:
            if card.get("queue") == -1:
                stats["suspended_non_leech"] += 1
            continue

        leech_cards.append(card)
        card_type = card.get("type", 0)
        if card_type == 1:
            stats["learning_leeches"] += 1
        else:
            stats["review_leeches"] += 1

    if not leech_cards:
        return stats

    already_suspended = [c for c in leech_cards if c.get("queue") == -1]
    needs_suspend = [c for c in leech_cards if c.get("queue") != -1]

    stats["already_suspended"] = len(already_suspended)
    stats["newly_suspended"] = len(needs_suspend)

    unique_notes = list({c["note"] for c in leech_cards})
    stats["tagged_notes"] = len(unique_notes)

    if dry_run:
        return stats

    # Suspend cards not already suspended
    if needs_suspend:
        for chunk in chunked([c["cardId"] for c in needs_suspend], 500):
            invoke("suspend", cards=chunk)

    # Tag parent notes (idempotent — Anki ignores duplicate tags)
    for chunk in chunked(unique_notes, 500):
        invoke("addTags", notes=chunk, tags=LEECH_TAG)

    return stats


def update_deck_config(deck_name: str, threshold: int, dry_run: bool) -> bool:
    """Ensure the deck config enforces leech suspension at *threshold* lapses.

    Returns True if a change was (or would be) made.
    """
    config: dict = invoke("getDeckConfig", deck=deck_name)
    lapse: dict = config.get("lapse", {})

    needs_update = (
        lapse.get("leechFails") != threshold
        or lapse.get("leechAction") != 0          # 0 = Suspend Card
        or lapse.get("delays") != DEFAULT_RELEARN_STEPS
    )

    if not needs_update:
        return False
    if dry_run:
        return True

    lapse["leechFails"] = threshold
    lapse["leechAction"] = 0
    lapse["delays"] = DEFAULT_RELEARN_STEPS
    config["lapse"] = lapse
    invoke("saveDeckConfig", config=config)
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def print_banner() -> None:
    title = "92 Leech Cards — suspend learning + review leeches via AnkiConnect"
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


def main() -> int:
    print_banner()
    parser = argparse.ArgumentParser(
        description=(
            "Suspend and tag cards as 'leech' after too many failed attempts. "
            "Unlike Anki's built-in leech detection, this also handles cards "
            "stuck in the learning queue that have never graduated to review."
        )
    )
    parser.add_argument(
        "--deck",
        metavar="DECK_NAME",
        help="Process only this deck (default: all Italian decks).",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=DEFAULT_THRESHOLD,
        metavar="N",
        help=(
            f"Number of failed attempts before a card is suspended as a leech "
            f"(default: {DEFAULT_THRESHOLD}). "
            "For learning cards this is total reps; for review cards it is lapses."
        ),
    )
    parser.add_argument(
        "--update-config",
        action="store_true",
        help=(
            "Also update each deck's config group so Anki itself suspends "
            "future review leeches mid-session at the same threshold."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be changed without modifying Anki.",
    )
    args = parser.parse_args()

    decks = [args.deck] if args.deck else ALL_DECKS

    if args.dry_run:
        print("DRY RUN — no changes will be made to Anki.\n")

    totals: dict[str, int] = {
        "learning_leeches": 0,
        "review_leeches": 0,
        "already_suspended": 0,
        "newly_suspended": 0,
        "tagged_notes": 0,
        "suspended_non_leech": 0,
    }

    # Collect one row per deck for the summary table
    rows: list[tuple[str, dict, str]] = []  # (deck_name, stats, error_msg)

    for deck_name in decks:
        try:
            if args.update_config:
                changed = update_deck_config(deck_name, args.threshold, dry_run=args.dry_run)
                if changed:
                    suffix = " (dry run)" if args.dry_run else ""
                    print(f"  Config updated{suffix}: {deck_name}")

            stats = process_deck(deck_name, args.threshold, dry_run=args.dry_run)
        except RuntimeError as exc:
            rows.append((deck_name, {}, str(exc)))
            continue

        for k in totals:
            totals[k] += stats[k]

        rows.append((deck_name, stats, ""))

    # ------------------------------------------------------------------
    # Print summary table
    # ------------------------------------------------------------------
    COL_HEADERS = ["Deck", "Learning", "Review", "Newly Susp.", "Alr. Susp.", "Tagged", "Susp. (non-leech)"]

    # Build table data rows
    table_rows: list[list[str]] = []
    for deck_name, stats, err in rows:
        if err:
            table_rows.append([deck_name, "ERROR", err, "", "", "", ""])
        else:
            table_rows.append([
                deck_name,
                str(stats.get("learning_leeches", 0)),
                str(stats.get("review_leeches", 0)),
                str(stats.get("newly_suspended", 0)),
                str(stats.get("already_suspended", 0)),
                str(stats.get("tagged_notes", 0)),
                str(stats.get("suspended_non_leech", 0)),
            ])

    # Totals row
    total_all = totals["learning_leeches"] + totals["review_leeches"]
    table_rows.append([
        "TOTAL",
        str(totals["learning_leeches"]),
        str(totals["review_leeches"]),
        str(totals["newly_suspended"]),
        str(totals["already_suspended"]),
        str(totals["tagged_notes"]),
        str(totals["suspended_non_leech"]),
    ])

    # Calculate column widths
    col_widths = [len(h) for h in COL_HEADERS]
    for row in table_rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(cell))

    def fmt_row(cells: list[str], widths: list[int]) -> str:
        return "| " + " | ".join(c.ljust(w) for c, w in zip(cells, widths)) + " |"

    def divider(widths: list[int], char: str = "-") -> str:
        return "+" + "+".join(char * (w + 2) for w in widths) + "+"

    print()
    print(divider(col_widths))
    print(fmt_row(COL_HEADERS, col_widths))
    print(divider(col_widths, "="))
    for i, row in enumerate(table_rows):
        # Insert a divider before the TOTAL row
        if i == len(table_rows) - 1:
            print(divider(col_widths))
        print(fmt_row(row, col_widths))
    print(divider(col_widths))

    dry = " (dry run)" if args.dry_run else ""
    print(f"\nDone{dry}. {total_all} leeches found, {totals['newly_suspended']} newly suspended.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
