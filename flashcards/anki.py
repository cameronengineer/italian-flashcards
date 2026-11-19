"""AnkiConnect client + helpers for the pipeline's notes inside Anki."""

from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.request
from typing import Any

from .settings import settings

ANKI_CONNECT_VERSION = 6

#: Every AnkiConnect action this program calls (a test checks the code against it).
ACTIONS = frozenset(
    {
        "addNotes", "addTags", "cardReviews", "cardsInfo", "changeDeck", "cloneDeckConfigId",
        "createDeck", "createModel", "deckNames", "deleteNotes", "findCards", "findNotes",
        "getActiveProfile", "getDeckConfig", "getDecks", "getMediaFilesNames", "modelFieldAdd",
        "modelFieldNames", "modelNames", "modelStyling", "modelTemplates", "multi", "notesInfo",
        "removeTags", "saveDeckConfig", "setDeckConfigId", "storeMediaFile", "suspend", "unsuspend",
        "updateModelStyling", "updateModelTemplates", "updateNoteFields", "version",
    }
)  # fmt: skip


def invoke(action: str, *, timeout: int = 60, **params: Any) -> Any:
    payload = json.dumps({"action": action, "version": ANKI_CONNECT_VERSION, "params": params}).encode(
        "utf-8"
    )
    try:
        with urllib.request.urlopen(settings.anki.url, payload, timeout=timeout) as resp:
            result = json.load(resp)
    except urllib.error.URLError as exc:
        raise RuntimeError(
            "Could not reach AnkiConnect. Make sure Anki is running with "
            f"the AnkiConnect add-on enabled ({settings.anki.url})."
        ) from exc
    if "error" not in result or "result" not in result:
        raise RuntimeError(f"Unexpected AnkiConnect response: {result!r}")
    if result["error"] is not None:
        raise RuntimeError(f"AnkiConnect error: {result['error']}")
    return result["result"]


#: Anki's deck options (``newGatherPriority`` / ``newSortOrder``) as the options screen names them.
GATHER = {
    0: "Deck",
    1: "Ascending position",
    2: "Descending position",
    3: "Random notes",
    4: "Random cards",
    5: "Deck, then random notes",
}
SORT = {
    0: "Card type, then order gathered",
    1: "Order gathered",
    2: "Card type, then random",
    3: "Random note, then card type",
    4: "Random",
}
DIRECTIONS = {"Recognition": "Italian → English", "Production": "English → Italian"}


def new_card_queue(sample: int = 100) -> dict:
    """How Anki will introduce the pipeline's new cards (read-only).

    New cards waiting in each direction, per deck; the next ``sample`` by
    position (the order "Ascending position" gathers them in); and the display
    order and sibling burying of the ``Italian`` deck, which decide what you
    actually see first.
    """
    from .notes import MODEL_NAME

    base = f"{quote('note:' + MODEL_NAME)} is:new -is:suspended"
    waiting, cards = {}, []
    for template, label in DIRECTIONS.items():
        ids = invoke("findCards", query=f"{base} card:{template}")
        waiting[label] = len(ids)
        # Card ids follow the order cards were added, so the earliest few hold the next positions.
        for group in chunks(sorted(ids)[: sample * 3]):
            cards += [
                (c.get("due", 0), c.get("deckName", ""), label) for c in invoke("cardsInfo", cards=group)
            ]
    upcoming = sorted(cards)[:sample]
    decks: dict[str, dict[str, int]] = {}
    for _due, deck, label in upcoming:
        decks.setdefault(deck, dict.fromkeys(DIRECTIONS.values(), 0))[label] += 1
    config = invoke("getDeckConfig", deck="Italian") or {}
    return {
        "waiting": waiting,
        f"next_{sample}_by_position": {
            label: sum(1 for *_x, lab in upcoming if lab == label) for label in DIRECTIONS.values()
        },
        "next_by_deck": dict(sorted(decks.items(), key=lambda kv: -sum(kv[1].values()))),
        "italian_deck_options": {
            "preset": config.get("name"),
            "new_card_gather_order": GATHER.get(
                config.get("newGatherPriority"), config.get("newGatherPriority")
            ),
            "new_card_sort_order": SORT.get(config.get("newSortOrder"), config.get("newSortOrder")),
            "bury_new_siblings": config.get("new", {}).get("bury"),
            "bury_review_siblings": config.get("rev", {}).get("bury"),
        },
    }


def capabilities() -> dict:
    """Is AnkiConnect reachable, and does it still offer every action used here?

    Read-only (``apiReflect``). AnkiConnect has dropped behaviour before (October
    2026: moving cards), so this is worth checking after updating it.
    """
    try:
        version = invoke("version", timeout=5)
    except RuntimeError as exc:
        return {"reachable": False, "error": str(exc)}
    offered = invoke("apiReflect", scopes=["actions"], actions=sorted(ACTIONS)).get("actions", [])
    return {
        "reachable": True,
        "version": version,
        "profile": invoke("getActiveProfile"),
        "unsupported_actions": sorted(ACTIONS - set(offered)),
    }


def chunks(items: list, size: int = 500):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def quote(term: str) -> str:
    """One quoted Anki search term, e.g. ``quote('deck:Italian - X')``."""
    return '"' + term.replace('"', '\\"') + '"'


# ── Reading notes back out ──────────────────────────────────────────────────


def _strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def learnt_pairs(deck: str | None = None) -> dict[str, list[tuple[str, str]]]:
    """``{deck: [(italian, english), …]}`` for graduated, unsuspended notes."""
    from .notes import MODEL_NAME

    if MODEL_NAME not in invoke("modelNames"):
        return {}
    query = quote(f"note:{MODEL_NAME}") + " is:review -is:suspended"
    if deck:
        query += " " + quote(f"deck:{deck}")
    cards: list[dict] = []
    for chunk in chunks(invoke("findCards", query=query)):
        cards.extend(c for c in invoke("cardsInfo", cards=chunk) if c)
    out: dict[str, set[tuple[str, str]]] = {}
    for c in cards:
        if c.get("type") not in (2, 3):
            continue
        fields = c.get("fields", {})
        pair = (
            _strip_html(fields.get("Italian", {}).get("value", "")),
            _strip_html(fields.get("English", {}).get("value", "")),
        )
        if pair[0] and pair[1]:
            out.setdefault(c.get("deckName", ""), set()).add(pair)
    return {d: sorted(p, key=lambda x: x[0].lower()) for d, p in sorted(out.items())}
