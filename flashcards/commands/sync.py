"""``sync`` command — push .apkg files to Anki and reconcile state.

Three stages:

  1. Import the .apkg of every deck the DB manages via AnkiConnect, then make
     the pipeline notetype's template + CSS match ``cards.py``.
  2. Delete Anki notes of the pipeline's note model, in the decks the DB
     manages, whose identity is no longer in the DB. Notes in any other deck
     are never touched, whatever their notetype.
  3. Reorder new (unseen) cards so the new-card queue matches the DB order.

Existing review state on cards that survive is untouched.

Identity: the ``SortKey`` field holds ``<natural_key>|<direction>`` (see
``util.note_key``). Notes exported before that change hold a bare integer
(their old position). The first import rewrites the key of every note that
still exists, so after an import any note still carrying a legacy key was not
in any package — i.e. it's an orphan.

**Safety**: Stage 2 is destructive. Orphans with any review history are
never deleted unless ``--delete-reviewed-orphans`` is passed. It also refuses to run when an unsafe number of orphans is detected
unless ``--allow-orphan-delete`` is passed. Thresholds (``settings.toml``
``[sync]``): by default more than 10% of notes in any deck, or more than 200
notes in absolute terms. If most notes
still carry legacy keys after import (imports didn't update existing notes),
it refuses unconditionally.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass, field

from ..anki import ensure_note_model, invoke, pipeline_note_query, quote
from ..db import connect, managed_decks
from ..paths import DECKS_DIR
from ..settings import settings
from ..util import chunked, note_key, print_banner
from .export import deck_file
#: Above this share of legacy-keyed notes after an import, assume the import
#: didn't update existing notes and never delete.
LEGACY_ABORT_RATIO = 0.5


@dataclass
class AnkiNote:
    note_id: int
    key: str
    cards: list[int]
    deck: str


@dataclass
class Classified:
    live: list[AnkiNote] = field(default_factory=list)
    orphans: list[AnkiNote] = field(default_factory=list)   # new-style key, not in DB
    legacy: list[AnkiNote] = field(default_factory=list)    # integer / empty key
    misplaced: list[tuple[AnkiNote, str]] = field(default_factory=list)


def _db_state() -> tuple[dict[str, tuple[str, int]], list[str]]:
    """note key → (deck, sort_order), plus the managed deck list."""
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT natural_key, direction, deck, sort_order FROM cards"
        ).fetchall()
        decks = managed_decks(conn)
    keys = {
        note_key(r["natural_key"], r["direction"]): (r["deck"], r["sort_order"])
        for r in rows
    }
    return keys, decks


def _import_decks(dry_run: bool, decks: list[str]) -> tuple[int, int]:
    """Import only the packages of decks the DB manages.

    Leftover .apkg files from renamed/removed decks are skipped — re-importing
    them would resurrect notes that sync just deleted.
    """
    wanted = [deck_file(d) for d in decks]
    missing = [p for p in wanted if not p.exists()]
    stale = sorted(set(DECKS_DIR.glob("*.apkg")) - set(wanted))
    for p in missing:
        print(f"  missing package (run export): {p.name}")
    if stale:
        print(f"  skipping {len(stale)} stale package(s) not in the DB: "
              + ", ".join(p.name for p in stale))
    imported = failed = 0
    for apkg in (p for p in wanted if p.exists()):
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


def _scope_query(decks: list[str]) -> str:
    """Pipeline-model notes in the decks the DB manages — and nowhere else.

    Scoping by model alone is not enough: Anki's importer can merge other
    notetypes into the pipeline's (it did, pulling in 18k notes from old,
    hand-managed decks), and those must never become deletion candidates.
    """
    deck_terms = " OR ".join(quote(f"deck:{d}") for d in decks)
    return f"{pipeline_note_query()} ({deck_terms})"


def _anki_notes(scope: str) -> list[AnkiNote]:
    """Every pipeline note inside ``scope``."""
    note_ids = invoke("findNotes", query=scope)
    infos: list[dict] = []
    for chunk in chunked(note_ids, 500):
        infos.extend(n for n in invoke("notesInfo", notes=chunk) if n)
    if infos and "cards" not in infos[0]:
        raise RuntimeError("AnkiConnect is too old (notesInfo has no 'cards'); update the add-on.")
    all_cards = [c for n in infos for c in n["cards"]]
    deck_of: dict[int, str] = {}
    for chunk in chunked(all_cards, 5000):
        for deck, cids in invoke("getDecks", cards=chunk).items():
            for cid in cids:
                deck_of[cid] = deck
    return [
        AnkiNote(
            note_id=n["noteId"],
            key=n.get("fields", {}).get("SortKey", {}).get("value", "").strip(),
            cards=n["cards"],
            deck=deck_of.get(n["cards"][0], "") if n["cards"] else "",
        )
        for n in infos
    ]


def _classify(notes: list[AnkiNote], keys: dict[str, tuple[str, int]]) -> Classified:
    out = Classified()
    for n in notes:
        if n.key in keys:
            out.live.append(n)
            expected = keys[n.key][0]
            if n.deck != expected:
                out.misplaced.append((n, expected))
        elif "|" in n.key:
            out.orphans.append(n)
        else:
            out.legacy.append(n)
    return out


def _reviewed_cards(card_ids: list[int], scope: str) -> set[int]:
    """Cards with any study history: not new, or with review-log entries
    (a card reset with "Forget" is new again but still has history)."""
    reviewed = set(invoke("findCards", query=f"{scope} -is:new"))
    for chunk in chunked(card_ids, 2000):
        for cid, revs in invoke("getReviewsOfCards", cards=chunk).items():
            if revs:
                reviewed.add(int(cid))
    return reviewed


def _delete_orphans(
    c: Classified,
    notes: list[AnkiNote],
    scope: str,
    *,
    dry_run: bool,
    allow_destructive: bool,
    delete_reviewed: bool = False,
) -> int:
    candidates = list(c.orphans)
    if dry_run:
        if c.legacy:
            print(f"  {len(c.legacy)} note(s) carry a legacy SortKey; the import "
                  f"re-keys live ones, so they can't be classified in a dry run.")
    else:
        if notes and len(c.legacy) / len(notes) > LEGACY_ABORT_RATIO:
            print(f"  REFUSING TO DELETE: {len(c.legacy)}/{len(notes)} notes still have a "
                  f"legacy SortKey after import.")
            print("  That means the import didn't update existing notes, so legacy notes "
                  "can't be told apart from orphans. Nothing deleted.")
            return 0
        candidates += c.legacy

    if not candidates:
        print("  no orphans")
        return 0

    # Never delete study history implicitly: orphans with reviews are kept
    # (and listed) unless explicitly requested.
    reviewed = _reviewed_cards([cid for n in candidates for cid in n.cards], scope)
    kept = [n for n in candidates if any(cid in reviewed for cid in n.cards)]
    if kept and not delete_reviewed:
        candidates = [n for n in candidates if n not in kept]
        print(f"  keeping {len(kept)} orphan(s) with review history "
              f"(pass --delete-reviewed-orphans to delete them too)")
        if not candidates:
            return 0

    deck_totals: dict[str, int] = defaultdict(int)
    for n in notes:
        deck_totals[n.deck] += 1
    per_deck: dict[str, list[AnkiNote]] = defaultdict(list)
    for n in candidates:
        per_deck[n.deck].append(n)

    unsafe = []
    for deck, orphans in sorted(per_deck.items()):
        total = deck_totals[deck]
        ratio = len(orphans) / max(total, 1)
        suffix = " (dry-run)" if dry_run else ""
        print(f"  {deck:<48}  {len(orphans)}/{total} orphan(s){suffix}")
        for n in orphans[:3]:
            print(f"      e.g. note {n.note_id}  SortKey={n.key!r}")
        if len(orphans) > settings.sync.orphan_absolute_limit or ratio > settings.sync.orphan_ratio_limit:
            unsafe.append((deck, len(orphans), total, ratio))

    if dry_run:
        return len(candidates)

    if unsafe and not allow_destructive:
        print()
        print("  REFUSING TO DELETE: orphan counts exceed safety thresholds.")
        print(f"  Limits: > {settings.sync.orphan_absolute_limit} notes absolute, "
              f"or > {settings.sync.orphan_ratio_limit:.0%} of deck (settings.toml [sync]).")
        for deck, n, total, ratio in unsafe:
            print(f"    {deck:<48}  {n}/{total} ({ratio:.0%}) over threshold")
        print()
        print("  If this is intentional, re-run with `python -m flashcards sync --allow-orphan-delete`.")
        print("  Otherwise: the DB is probably missing sources — fix the input and rebuild.")
        return 0

    ids = [n.note_id for n in candidates]
    for chunk in chunked(ids, 500):
        invoke("deleteNotes", notes=chunk)
    return len(ids)


def _reorder(
    c: Classified, keys: dict[str, tuple[str, int]], scope: str, dry_run: bool
) -> tuple[int, int]:
    """Set the due position of every new live card from the DB sort order."""
    new_cards = set(invoke("findCards", query=f"{scope} is:new"))
    per_deck: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for n in c.live:
        sort_order = keys[n.key][1]
        for cid in n.cards:
            if cid in new_cards:
                per_deck[n.deck].append((sort_order, cid))
    total = 0
    for deck, items in sorted(per_deck.items()):
        items.sort()
        total += len(items)
        suffix = " (dry-run)" if dry_run else ""
        print(f"  {deck:<48}  {len(items):>5} new cards reordered{suffix}")
        if dry_run:
            continue
        actions = [
            {
                "action": "setSpecificValueOfCard",
                "params": {"card": cid, "keys": ["due"], "newValues": [pos]},
            }
            for pos, (_, cid) in enumerate(items, start=1)
        ]
        for chunk in chunked(actions, 500):
            invoke("multi", actions=chunk)
    return len(new_cards), total


def run(
    dry_run: bool = False,
    allow_orphan_delete: bool = False,
    delete_reviewed_orphans: bool = False,
) -> dict:
    print_banner("sync — import decks → orphans → reorder")
    keys, decks = _db_state()
    if not keys:
        print("  Local cards table is EMPTY — refusing to sync (it would orphan every note).")
        print("  Run `python -m flashcards build` first.")
        return {"imported": 0, "import_failed": 0, "orphans": 0, "new_cards": 0, "reordered": 0}

    imp, imp_failed = _import_decks(dry_run, decks)
    changed = ensure_note_model(dry_run=dry_run)
    if changed:
        verb = "would update" if dry_run else "updated"
        print(f"  {verb} template/CSS of note model(s): {', '.join(changed)}")
    print()
    scope = _scope_query(decks)
    notes = _anki_notes(scope)
    classified = _classify(notes, keys)
    print(f"  {len(notes)} pipeline notes in Anki: {len(classified.live)} live, "
          f"{len(classified.orphans)} orphaned, {len(classified.legacy)} legacy-keyed")
    if classified.misplaced:
        moved_from = defaultdict(int)
        for n, expected in classified.misplaced:
            moved_from[(n.deck, expected)] += 1
        print(f"  {len(classified.misplaced)} note(s) sit in a different deck than the DB says "
              f"(renamed deck? Anki doesn't move existing cards on import):")
        for (actual, expected), count in sorted(moved_from.items()):
            print(f"      {count:>5}  {actual!r} → should be {expected!r}")
        print("  Move them in Anki's browser (Change Deck); review history is kept.")
    print()
    orphans = _delete_orphans(
        classified, notes, scope,
        dry_run=dry_run,
        allow_destructive=allow_orphan_delete,
        delete_reviewed=delete_reviewed_orphans,
    )
    print()
    new, reordered = _reorder(classified, keys, scope, dry_run)
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
