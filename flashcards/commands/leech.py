"""``leech`` command — suspend and tag cards that keep failing.

Anki's own leech detection only fires for *review* cards and uses the
cumulative ``lapses`` counter, which never resets. This command looks at
the actual review history instead and counts consecutive "Again" presses at
the tail (one success resets it), so cards stuck in learning are caught too.

Leeches get the ``leech`` tag and are suspended. ``--update-config`` also
sets each deck's config so Anki suspends future review leeches at the same
threshold.

``--release`` undoes it: every leech is unsuspended and loses the tag. A
released card is judged only on reviews after its release, so it becomes a
leech again only by failing again, not because of its old history.
"""

from __future__ import annotations

import json
import time

from ..anki import chunks, invoke, quote
from ..settings import settings
from ..reconcile import owned_notes
from ..db import connect
from contextlib import closing
from ..util import print_banner, table

LEECH_TAG = "leech"
RELEASED = "leech_released"  # metadata: {card id: when it was released, epoch ms}
STAT_KEYS = (
    "learning_leeches",
    "review_leeches",
    "newly_suspended",
    "already_suspended",
    "tagged_notes",
    "suspended_non_leech",
)


def _owned():
    with closing(connect()) as conn:
        return owned_notes(conn)


def _released(conn) -> dict[int, int]:
    row = conn.execute("SELECT value FROM metadata WHERE key=?", (RELEASED,)).fetchone()
    return {int(card): at for card, at in json.loads(row[0] if row else "{}").items()}


def _mark_released(conn, cards) -> None:
    released = _released(conn)
    now = int(time.time() * 1000)
    released.update(dict.fromkeys(cards, now))
    conn.execute(
        "INSERT OR REPLACE INTO metadata VALUES(?,?)",
        (RELEASED, json.dumps({str(c): at for c, at in sorted(released.items())})),
    )
    conn.commit()


def _decks(deck: str | None, owned) -> list[str]:
    decks = {d for n in owned for d in n.decks}
    return sorted(d for d in decks if deck is None or d == deck or d.startswith(deck + "::"))


def _failures_at_tail(reviews: list[tuple[int, int]]) -> int:
    """Consecutive Again (button 1) presses at the end of the history."""
    count = 0
    for _ts, button in reversed(reviews):
        if button != 1:
            break
        count += 1
    return count


def _process_deck(
    deck: str, threshold: int, dry_run: bool, owned: set[int], released: dict[int, int] | None = None
) -> dict[str, int]:
    stats = dict.fromkeys(STAT_KEYS, 0)
    card_ids = [c for c in invoke("findCards", query=f"{quote(f'deck:{deck}')} -is:new") if c in owned]
    if not card_ids:
        return stats
    cards = [c for chunk in chunks(card_ids) for c in invoke("cardsInfo", cards=chunk) if c]
    history: dict[int, list[tuple[int, int]]] = {}
    released = released or {}
    for review in invoke("cardReviews", deck=deck, startID=0):
        if review[0] > released.get(review[1], 0):  # a released card starts over
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


def _update_config(deck: str, threshold: int, dry_run: bool, owned: set[int]) -> bool:
    config = invoke("getDeckConfig", deck=deck)
    lapse = config.get("lapse", {})
    steps = list(settings.leech.relearn_steps)
    if (lapse.get("leechFails"), lapse.get("leechAction"), lapse.get("delays")) == (threshold, 0, steps):
        return False
    if not dry_run:
        if set(invoke("findCards", query=quote(f"deck:{deck}"))) - owned:
            raise RuntimeError("Deck contains unowned cards; refusing preset changes")
        others = [
            d
            for d in invoke("deckNames")
            if d != deck and invoke("getDeckConfig", deck=d).get("id") == config.get("id")
        ]
        if others:
            clone = invoke("cloneDeckConfigId", name="Flashcards " + deck, cloneFrom=str(config["id"]))
            if not clone or not invoke("setDeckConfigId", decks=[deck], configId=clone):
                raise RuntimeError("Could not isolate shared deck preset")
            config = invoke("getDeckConfig", deck=deck)
            lapse = config.get("lapse", {})
        lapse.update(leechFails=threshold, leechAction=0, delays=steps)  # 0 = suspend
        config["lapse"] = lapse
        invoke("saveDeckConfig", config=config)
    return True


def run(
    *,
    deck: str | None = None,
    threshold: int | None = None,
    update_config: bool = False,
    dry_run: bool = False,
) -> int:
    print_banner("leech — suspend learning + review leeches")
    threshold = threshold or settings.leech.threshold
    if dry_run:
        print("DRY RUN — no changes will be made to Anki.\n")
    rows, totals = [], dict.fromkeys(STAT_KEYS, 0)
    errors = 0
    owned = _owned()
    owned_cards = {c for n in owned for c in n.cards}
    with closing(connect()) as conn:
        released = _released(conn)
    for name in _decks(deck, owned):
        try:
            if update_config and _update_config(name, threshold, dry_run, owned_cards):
                print(f"  config updated{' (dry run)' if dry_run else ''}: {name}")
            stats = _process_deck(name, threshold, dry_run, owned_cards, released)
        except RuntimeError as exc:
            errors += 1
            rows.append([name, "ERROR", str(exc), "", "", "", ""])
            continue
        for k in STAT_KEYS:
            totals[k] += stats[k]
        rows.append([name, *(stats[k] for k in STAT_KEYS)])
    print()
    print(
        table(
            ["Deck", "Learning", "Review", "Newly Susp.", "Alr. Susp.", "Tagged", "Susp. (non-leech)"],
            rows,
            total=["TOTAL", *(totals[k] for k in STAT_KEYS)],
        )
    )
    found = totals["learning_leeches"] + totals["review_leeches"]
    print(
        f"\nDone{' (dry run)' if dry_run else ''}. {found} leeches found, "
        f"{totals['newly_suspended']} newly suspended."
    )
    return 1 if errors else 0


def release(*, deck: str | None = None, dry_run: bool = False) -> int:
    """Unleech everything: unsuspend every leech card and remove the ``leech``
    tag, across the whole collection (or one ``deck`` and its subdecks).

    Memory aids stay on the cards. The ``leech`` command then ignores the
    reviews from before the release, so a card is suspended again only if it
    fails ``threshold`` times in a row from here on. Anki's own leech action
    (deck options → Lapses) still applies separately.
    """
    print_banner("leech --release — put every leech back into rotation")
    query = f"tag:{LEECH_TAG}" + (f" {quote(f'deck:{deck}')}" if deck else "")
    notes = invoke("findNotes", query=query)
    cards = invoke("findCards", query=query)
    suspended = invoke("findCards", query=f"{query} is:suspended")
    print(f"  {len(notes)} leech note(s), {len(cards)} card(s), {len(suspended)} suspended")
    if dry_run:
        print("  Dry run: nothing changed.")
        return 0
    if not notes:
        return 0
    with closing(connect()) as conn:
        # Recorded first: if Anki stops part-way, the next run still treats them as released.
        _mark_released(conn, cards)
    for chunk in chunks(suspended):
        invoke("unsuspend", cards=chunk)
    for chunk in chunks(notes):
        invoke("removeTags", notes=chunk, tags=LEECH_TAG)
    left = invoke("findCards", query=f"{query} is:suspended")
    print(f"  unsuspended {len(suspended)} card(s); removed the leech tag from {len(notes)} note(s).")
    if left:
        print(f"  {len(left)} card(s) are still suspended; check them in Anki (search: tag:leech).")
        return 1
    return 0


# ── Leech doctor ───────────────────────────────────────────────────────────


def doctor(
    *, unsuspend: bool = False, limit: int = 200, deck: str | None = None, dry_run: bool = False
) -> int:
    """Write a memory aid for every leech-tagged pipeline note.

    Mnemonics are stored per note key and shown in the Note field from the
    next ``./run.sh`` on. ``--unsuspend`` puts the cards back into rotation
    once their mnemonic exists (including leeches helped on an earlier run).
    """
    import html as _html
    import re as _re

    from ..ai import AI
    from ..tasks import LEECH_HELP

    print_banner("leech doctor — memory aids for cards you keep failing")
    strip = lambda v: _html.unescape(_re.sub(r"<[^>]+>", " ", v or "")).strip()  # noqa: E731
    owned = [
        n for n in _owned() if deck is None or any(d == deck or d.startswith(deck + "::") for d in n.decks)
    ]
    ids = {n.note_id for n in owned}
    chosen = [n for n in invoke("findNotes", query="tag:leech") if n in ids][:limit]
    if dry_run:
        print(f"  Would inspect {len(chosen)} owned leech notes")
        return 0
    notes = invoke("notesInfo", notes=chosen)
    with closing(connect()) as conn:
        cards = []
        done_cards: list[int] = []  # cards whose note has a memory aid
        for n in notes:
            f = {k: v["value"] for k, v in n.get("fields", {}).items()}
            if "Key" not in f:
                continue
            key, it, en = f["Key"], strip(f.get("Italian")), strip(f.get("English"))
            if conn.execute("SELECT 1 FROM mnemonics WHERE key = ?", (key,)).fetchone():
                done_cards += n.get("cards", [])
                continue
            cards.append({"id": key, "italian": it, "english": en, "_cards": n.get("cards", [])})
        if not cards:
            print("  No new leeches need a memory aid.")
            if unsuspend and done_cards:
                _unsuspend_aided(conn, done_cards)
            return 0
        ai = AI(conn)
        written = 0
        for i in range(0, len(cards), 20):
            batch = cards[i : i + 20]
            res = ai.run(
                LEECH_HELP.task(
                    {"cards": [{k: v for k, v in c.items() if not k.startswith("_")} for c in batch]}
                )
            )
            by_id = {c["id"]: c["mnemonic"] for c in res.get("cards", []) if c.get("mnemonic")}
            for c in batch:
                if c["id"] in by_id:
                    conn.execute(
                        "INSERT OR REPLACE INTO mnemonics (key, mnemonic) VALUES (?, ?)",
                        (c["id"], by_id[c["id"]]),
                    )
                    done_cards += c["_cards"]
                    written += 1
            conn.commit()
        print(f"  wrote {written} memory aid(s); they appear on the cards after the next ./run.sh.")
        from ..notes import build

        build(conn)
        # Publish the aid before unsuspending; failed publication leaves cards suspended.
        if unsuspend and done_cards:
            from ..reconcile import run as apply

            apply(conn)
            _unsuspend_aided(conn, done_cards)
    return 0


def _unsuspend_aided(conn, cards: list[int]) -> None:
    """Back into rotation, judged from here on (see ``release``); the tag stays."""
    _mark_released(conn, cards)
    for chunk in chunks(cards, 500):
        invoke("unsuspend", cards=chunk)
    print(f"  unsuspended {len(cards)} card(s).")
