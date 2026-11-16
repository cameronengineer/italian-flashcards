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
