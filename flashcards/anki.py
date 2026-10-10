"""AnkiConnect client + helpers for the pipeline's notes inside Anki."""

from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.request
from contextlib import closing
from typing import Any

from .cards import LEGACY_MODEL_FIELDS, LEGACY_MODEL_NAME
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


# ── The pipeline's legacy (v3) notetype inside Anki ──────────────────────


def pipeline_models() -> list[str]:
    """Anki notetypes holding pipeline notes, whatever Anki named them.

    Anki may import the model under a renamed copy (``Italian Card
    Model-4ce4e``, ``…+++``) when an older notetype with the same id had
    other fields, so match on the name prefix **and** the exact field list.
    """
    return [
        m
        for m in invoke("modelNames")
        if m.startswith(LEGACY_MODEL_NAME) and invoke("modelFieldNames", modelName=m) == LEGACY_MODEL_FIELDS
    ]


# ── Reading notes back out ──────────────────────────────────────────────────


def _strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def learnt_pairs(deck: str | None = None) -> dict[str, list[tuple[str, str]]]:
    """``{deck: [(italian, english), …]}`` for graduated, unsuspended notes.

    Covers every deck holding pipeline notes. Each note is one direction of
    a card pair, so which field holds the Italian depends on the direction
    in ``SortKey`` (``…|it_to_en``). Notes with a legacy integer SortKey fall
    back to the local DB to tell which side is Italian.
    """
    from .notes import MODEL_NAME

    models = [quote(f"note:{m}") for m in pipeline_models()]
    if MODEL_NAME in invoke("modelNames"):
        models.append(quote(f"note:{MODEL_NAME}"))
    if not models:
        return {}
    query = "(" + " OR ".join(models) + ") is:review -is:suspended"
    if deck:
        query += " " + quote(f"deck:{deck}")
    cards: list[dict] = []
    for chunk in chunks(invoke("findCards", query=query)):
        cards.extend(c for c in invoke("cardsInfo", cards=chunk) if c)

    italian_texts: set[str] | None = None
    out: dict[str, set[tuple[str, str]]] = {}
    for c in cards:
        if c.get("type") not in (2, 3):
            continue
        fields = c.get("fields", {})
        if "Italian" in fields and "Key" in fields:  # v4 note: fields say which side is which
            pair = (_strip_html(fields["Italian"]["value"]), _strip_html(fields["English"]["value"]))
            if pair[0] and pair[1]:
                out.setdefault(c.get("deckName", ""), set()).add(pair)
            continue
        front = _strip_html(fields.get("FrontText", {}).get("value", ""))
        back = _strip_html(fields.get("BackHighlight", {}).get("value", ""))
        key = fields.get("SortKey", {}).get("value", "")
        if key.endswith("|it_to_en"):
            italian_first = True
        elif key.endswith("|en_to_it"):
            italian_first = False
        else:
            if italian_texts is None:
                from .db import connect

                with closing(connect()) as conn:
                    italian_texts = {
                        r[0]
                        for r in conn.execute("SELECT back_highlight FROM cards WHERE direction = 'en_to_it'")
                    }
            italian_first = front in italian_texts
        pair = (front, back) if italian_first else (back, front)
        if pair[0]:
            out.setdefault(c.get("deckName", ""), set()).add(pair)
    return {d: sorted(p, key=lambda x: x[0].lower()) for d, p in sorted(out.items())}
