"""``leech`` command — suspend and tag cards that keep failing.

Anki's own leech detection only fires for *review* cards and uses the
cumulative ``lapses`` counter, which never resets. This command looks at
the actual review history instead and counts consecutive "Again" presses at
the tail (one success resets it), so cards stuck in learning are caught too.

Leeches get the ``leech`` tag and are suspended. ``--update-config`` also
sets each deck's config so Anki suspends future review leeches at the same
threshold.
"""

from __future__ import annotations

from ..anki import chunks, invoke, quote
from ..settings import settings
from ..util import print_banner, table

LEECH_TAG = "leech"
STAT_KEYS = ("learning_leeches", "review_leeches", "newly_suspended",
             "already_suspended", "tagged_notes", "suspended_non_leech")


def _decks(deck: str | None) -> list[str]:
    return [deck] if deck else sorted(d for d in invoke("deckNames") if d != "Default")


def _failures_at_tail(reviews: list[tuple[int, int]]) -> int:
    """Consecutive Again (button 1) presses at the end of the history."""
    count = 0
    for _ts, button in reversed(reviews):
        if button != 1:
            break
        count += 1
    return count


def _process_deck(deck: str, threshold: int, dry_run: bool) -> dict[str, int]:
    stats = dict.fromkeys(STAT_KEYS, 0)
    card_ids = invoke("findCards", query=f"{quote(f'deck:{deck}')} -is:new")
    if not card_ids:
        return stats
    cards = [c for chunk in chunks(card_ids) for c in invoke("cardsInfo", cards=chunk) if c]
    history: dict[int, list[tuple[int, int]]] = {}
    for review in invoke("cardReviews", deck=deck, startID=0):
        history.setdefault(review[1], []).append((review[0], review[3]))

    leeches = []
    for card in cards:
        if _failures_at_tail(sorted(history.get(card["cardId"], []))) < threshold:
            if card.get("queue") == -1:
                stats["suspended_non_leech"] += 1
            continue
        leeches.append(card)
        stats["learning_leeches" if card.get("type") == 1 else "review_leeches"] += 1
    if not leeches:
        return stats

    needs_suspend = [c["cardId"] for c in leeches if c.get("queue") != -1]
    notes = sorted({c["note"] for c in leeches})
    stats["already_suspended"] = len(leeches) - len(needs_suspend)
    stats["newly_suspended"] = len(needs_suspend)
    stats["tagged_notes"] = len(notes)
    if not dry_run:
        for chunk in chunks(needs_suspend):
            invoke("suspend", cards=chunk)
        for chunk in chunks(notes):
            invoke("addTags", notes=chunk, tags=LEECH_TAG)
    return stats


def _update_config(deck: str, threshold: int, dry_run: bool) -> bool:
    config = invoke("getDeckConfig", deck=deck)
    lapse = config.get("lapse", {})
    steps = list(settings.leech.relearn_steps)
    if (lapse.get("leechFails"), lapse.get("leechAction"), lapse.get("delays")) == (threshold, 0, steps):
        return False
    if not dry_run:
        lapse.update(leechFails=threshold, leechAction=0, delays=steps)  # 0 = suspend
        config["lapse"] = lapse
        invoke("saveDeckConfig", config=config)
    return True


def run(*, deck: str | None = None, threshold: int | None = None,
        update_config: bool = False, dry_run: bool = False) -> int:
    print_banner("leech — suspend learning + review leeches")
    threshold = threshold or settings.leech.threshold
    if dry_run:
        print("DRY RUN — no changes will be made to Anki.\n")
    rows, totals = [], dict.fromkeys(STAT_KEYS, 0)
    for name in _decks(deck):
        try:
            if update_config and _update_config(name, threshold, dry_run):
                print(f"  config updated{' (dry run)' if dry_run else ''}: {name}")
            stats = _process_deck(name, threshold, dry_run)
        except RuntimeError as exc:
            rows.append([name, "ERROR", str(exc), "", "", "", ""])
            continue
        for k in STAT_KEYS:
            totals[k] += stats[k]
        rows.append([name, *(stats[k] for k in STAT_KEYS)])
    print()
    print(table(
        ["Deck", "Learning", "Review", "Newly Susp.", "Alr. Susp.", "Tagged", "Susp. (non-leech)"],
        rows, total=["TOTAL", *(totals[k] for k in STAT_KEYS)],
    ))
    found = totals["learning_leeches"] + totals["review_leeches"]
    print(f"\nDone{' (dry run)' if dry_run else ''}. {found} leeches found, "
          f"{totals['newly_suspended']} newly suspended.")
    return 0


# ── Leech doctor ───────────────────────────────────────────────────────────


def doctor(*, unsuspend: bool = False, limit: int = 200) -> int:
    """Write a memory aid for every leech-tagged pipeline note.

    Mnemonics are stored per note key and shown in the Note field from the
    next ``flashcards apply`` on. ``--unsuspend`` puts the cards back into
    rotation once their mnemonic exists.
    """
    import html as _html
    import re as _re
    from contextlib import closing

    from ..ai import AI
    from ..db import connect
    from ..reconcile import legacy_target
    from ..tasks import LEECH_HELP

    print_banner("leech doctor — memory aids for cards you keep failing")
    strip = lambda v: _html.unescape(_re.sub(r"<[^>]+>", " ", v or "")).strip()  # noqa: E731
    notes = invoke("notesInfo", notes=invoke("findNotes", query="tag:leech")[:limit])
    with closing(connect()) as conn:
        cards = []
        for n in notes:
            f = {k: v["value"] for k, v in n.get("fields", {}).items()}
            if "Key" in f:
                key, it, en = f["Key"], strip(f.get("Italian")), strip(f.get("English"))
            elif "SortKey" in f and "|" in f["SortKey"]:
                tgt = legacy_target(conn, f["SortKey"])
                key = tgt[0] if tgt else f["SortKey"]
                front, back = strip(f.get("FrontText")), strip(f.get("BackHighlight"))
                it, en = (front, back) if f["SortKey"].endswith("|it_to_en") else (back, front)
            else:
                continue
            if conn.execute("SELECT 1 FROM mnemonics WHERE key = ?", (key,)).fetchone():
                continue
            cards.append({"id": key, "italian": it, "english": en, "_cards": n.get("cards", [])})
        if not cards:
            print("  No new leeches need a memory aid.")
            return 0
        ai = AI(conn)
        done_cards: list[int] = []
        for i in range(0, len(cards), 20):
            batch = cards[i:i + 20]
            res = ai.run(LEECH_HELP.task({"cards": [{k: v for k, v in c.items() if not k.startswith("_")} for c in batch]}))
            by_id = {c["id"]: c["mnemonic"] for c in res.get("cards", []) if c.get("mnemonic")}
            for c in batch:
                if c["id"] in by_id:
                    conn.execute("INSERT OR REPLACE INTO mnemonics (key, mnemonic) VALUES (?, ?)", (c["id"], by_id[c["id"]]))
                    done_cards += c["_cards"]
            conn.commit()
        print(f"  wrote {len(done_cards) and len({c['id'] for c in cards})} memory aid(s); they appear on the cards after the next `flashcards apply`.")
        if unsuspend and done_cards:
            for chunk in chunks(done_cards, 500):
                invoke("unsuspend", cards=chunk)
            print(f"  unsuspended {len(done_cards)} card(s).")
    return 0
