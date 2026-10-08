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
