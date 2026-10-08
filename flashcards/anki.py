"""AnkiConnect client + helpers for the pipeline's notes inside Anki."""

from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.request
from contextlib import closing
from typing import Any

from .cards import AFMT, CSS, MODEL_FIELDS, MODEL_NAME, QFMT
from .settings import settings

ANKI_CONNECT_VERSION = 6


def invoke(action: str, *, timeout: int = 60, **params: Any) -> Any:
    payload = json.dumps(
        {"action": action, "version": ANKI_CONNECT_VERSION, "params": params}
    ).encode("utf-8")
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
        yield items[i:i + size]


def quote(term: str) -> str:
    """One quoted Anki search term, e.g. ``quote('deck:Italian - X')``."""
    return '"' + term.replace('"', '\\"') + '"'


# ── The pipeline's note model inside Anki ───────────────────────────────────


def pipeline_models() -> list[str]:
    """Anki notetypes holding pipeline notes, whatever Anki named them.

    Anki may import the model under a renamed copy (``Italian Card
    Model-4ce4e``, ``…+++``) when an older notetype with the same id had
    other fields, so match on the name prefix **and** the exact field list.
    """
    return [
        m for m in invoke("modelNames")
        if m.startswith(MODEL_NAME) and invoke("modelFieldNames", modelName=m) == MODEL_FIELDS
    ]


def pipeline_note_query() -> str:
    names = pipeline_models()
    if not names:
        raise RuntimeError(f"No Anki note model named {MODEL_NAME!r}* with the pipeline's fields.")
    return "(" + " OR ".join(quote(f"note:{n}") for n in names) + ")"


def ensure_note_model(dry_run: bool = False) -> list[str]:
    """Make every pipeline notetype use the current template + CSS.

    The .apkg importer doesn't reliably update an existing notetype's
    template, so sync sets it explicitly — every card (including older notes
    that share the notetype) then renders the same way. Returns the names of
    notetypes that were (or, in a dry run, would be) changed.
    """
    changed = []
    for name in pipeline_models():
        templates = invoke("modelTemplates", modelName=name)
        styling = invoke("modelStyling", modelName=name).get("css", "")
        want = {"Front": QFMT, "Back": AFMT}
        stale_tmpl = [t for t, sides in templates.items() if sides != want]
        stale_css = styling != CSS
        if not (stale_tmpl or stale_css):
            continue
        changed.append(name)
        if dry_run:
            continue
        if stale_tmpl:
            invoke("updateModelTemplates", model={"name": name, "templates": {t: want for t in stale_tmpl}})
        if stale_css:
            invoke("updateModelStyling", model={"name": name, "css": CSS})
    return changed


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
    query = f"{pipeline_note_query()} is:review -is:suspended"
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
                        r[0] for r in conn.execute(
                            "SELECT back_highlight FROM cards WHERE direction = 'en_to_it'"
                        )
                    }
            italian_first = front in italian_texts
        pair = (front, back) if italian_first else (back, front)
        if pair[0]:
            out.setdefault(c.get("deckName", ""), set()).add(pair)
    return {d: sorted(p, key=lambda x: x[0].lower()) for d, p in sorted(out.items())}
