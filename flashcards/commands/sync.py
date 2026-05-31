"""``sync`` command — push .apkg files to Anki and reconcile state.

Three stages:

  1. Import every .apkg from decks/ via AnkiConnect.
  2. Delete Anki notes whose SortKey is no longer present in the DB.
  3. Reorder new (unseen) cards by SortKey so the new-card queue matches the DB.

Existing review state on cards that survive is untouched.

**Safety**: Stage 2 is destructive (deletes Anki notes, including review
history). It refuses to run when an unsafe number of orphans is detected
unless ``--allow-orphan-delete`` is passed. Thresholds: more than 10% of
notes in any deck, or more than 200 notes in absolute terms.
"""

from __future__ import annotations

import sys

from ..anki import invoke
from ..db import connect, managed_decks
from ..paths import DECKS_DIR
from ..util import chunked, print_banner

# Safeguard thresholds for orphan deletion
ORPHAN_RATIO_LIMIT = 0.10
ORPHAN_ABSOLUTE_LIMIT = 200


def _import_decks(dry_run: bool) -> tuple[int, int]:
    apkgs = sorted(DECKS_DIR.glob("*.apkg"))
    if not apkgs:
        print(f"  no .apkg files in {DECKS_DIR}")
        return 0, 0
    imported = failed = 0
    for apkg in apkgs:
        if dry_run:
            print(f"  would import: {apkg.name}")
            imported += 1
            continue
        try:
            ok = invoke("importPackage", path=str(apkg.resolve()))
            if ok:
                print(f"  imported: {apkg.name}")
                imported += 1
            else:
                print(f"  FAILED:   {apkg.name}")
                failed += 1
        except RuntimeError as exc:
            print(f"  ERROR:    {apkg.name}  — {exc}", file=sys.stderr)
            failed += 1
    if imported and not dry_run:
        try:
            invoke("reloadCollection")
        except RuntimeError:
            pass
    return imported, failed


def _delete_orphans(dry_run: bool, allow_destructive: bool = False) -> int:
    with connect() as conn:
        valid = {str(r[0]) for r in conn.execute("SELECT sort_order FROM cards").fetchall()}
        decks = managed_decks(conn)
    if not valid:
        print("  Local cards table is EMPTY — refusing to delete every Anki note.")
        print("  Run `python -m flashcards build` first.")
        return 0

    # Pass 1: collect orphans per deck, no deletions yet.
    per_deck: list[tuple[str, list[int], int]] = []  # (deck, orphans, total_notes)
    for deck in decks:
        escaped = deck.replace('"', '\\"')
        try:
            note_ids = invoke("findNotes", query=f'deck:"{escaped}"')
        except RuntimeError as exc:
            print(f"  [{deck}] ERROR: {exc}", file=sys.stderr)
            continue
        if not note_ids:
            print(f"  {deck:<48}  no notes")
            continue
        orphans: list[int] = []
        for chunk in chunked(note_ids, 500):
            for note in invoke("notesInfo", notes=chunk):
                sk = note.get("fields", {}).get("SortKey", {}).get("value", "").strip()
                if not sk or sk not in valid:
                    orphans.append(note["noteId"])
        per_deck.append((deck, orphans, len(note_ids)))

    # Pass 2: safeguard check. If any deck exceeds thresholds and the caller
    # hasn't explicitly opted in, abort.
    unsafe = []
    for deck, orphans, total_notes in per_deck:
        if not orphans:
            continue
        ratio = len(orphans) / max(total_notes, 1)
        if len(orphans) > ORPHAN_ABSOLUTE_LIMIT or ratio > ORPHAN_RATIO_LIMIT:
            unsafe.append((deck, len(orphans), total_notes, ratio))

    if unsafe and not (dry_run or allow_destructive):
        print()
        print("  REFUSING TO DELETE: orphan counts exceed safety thresholds.")
        print(f"  Limits: > {ORPHAN_ABSOLUTE_LIMIT} notes absolute, or > {ORPHAN_RATIO_LIMIT:.0%} of deck.")
        for deck, n, total_notes, ratio in unsafe:
            print(f"    {deck:<48}  {n}/{total_notes} ({ratio:.0%}) over threshold")
        print()
        print("  If this is intentional, re-run with `python -m flashcards sync --allow-orphan-delete`.")
        print("  Otherwise: ./run.sh probably ran with a partial input set — fix the input and rebuild.")
        return 0

    # Pass 3: actually delete (or report).
    total = 0
    for deck, orphans, total_notes in per_deck:
        if not orphans:
            print(f"  {deck:<48}  no orphans")
            continue
        total += len(orphans)
        suffix = " (dry-run)" if dry_run else ""
        print(f"  {deck:<48}  {len(orphans)} orphan(s){suffix}")
        if not dry_run:
            for chunk in chunked(orphans, 500):
                invoke("deleteNotes", notes=chunk)
    return total


def _reorder(dry_run: bool) -> tuple[int, int]:
    with connect() as conn:
        decks = managed_decks(conn)
    total_new = total_reordered = 0
    for deck in decks:
        escaped = deck.replace('"', '\\"')
        try:
            card_ids = invoke("findCards", query=f'deck:"{escaped}" is:new')
        except RuntimeError as exc:
            print(f"  [{deck}] ERROR: {exc}", file=sys.stderr)
            continue
        if not card_ids:
            print(f"  {deck:<48}  no new cards")
            continue
        total_new += len(card_ids)
        note_ids = invoke("cardsToNotes", cards=card_ids)
        card_to_note = dict(zip(card_ids, note_ids))
        notes_info: dict[int, dict] = {}
        for chunk in chunked(list(set(note_ids)), 500):
            for note in invoke("notesInfo", notes=chunk):
                notes_info[note["noteId"]] = note
        sortable: list[tuple[int, int]] = []
        for cid in card_ids:
            note = notes_info.get(card_to_note[cid])
            sk = note.get("fields", {}).get("SortKey", {}).get("value", "").strip() if note else ""
            if sk.isdigit():
                sortable.append((int(sk), cid))
        sortable.sort(key=lambda x: x[0])
        total_reordered += len(sortable)
        suffix = " (dry-run)" if dry_run else ""
        print(f"  {deck:<48}  {len(card_ids):>5} new  →  {len(sortable)} reordered{suffix}")
        if dry_run:
            continue
        actions = [
            {
                "action": "setSpecificValueOfCard",
                "params": {"card": cid, "keys": ["due"], "newValues": [pos]},
            }
            for pos, (_, cid) in enumerate(sortable, start=1)
        ]
        for chunk in chunked(actions, 500):
            invoke("multi", actions=chunk)
    return total_new, total_reordered


def run(dry_run: bool = False, allow_orphan_delete: bool = False) -> dict:
    print_banner("sync — import decks → orphans → reorder")
    imp, imp_failed = _import_decks(dry_run)
    print()
    orphans = _delete_orphans(dry_run, allow_destructive=allow_orphan_delete)
    print()
    new, reordered = _reorder(dry_run)
    print(
        f"\nDone. imported={imp} failed={imp_failed} orphans={orphans} "
        f"new_cards={new} reordered={reordered}"
    )
    return {
        "imported": imp,
        "import_failed": imp_failed,
        "orphans": orphans,
        "new_cards": new,
        "reordered": reordered,
    }
