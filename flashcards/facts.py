"""Fun facts ("did you know?") for words — etymology, links to English,
false friends, cultural notes.

One fact per root word, stored in ``word_facts`` and shared by every list
and deck that uses it. Facts are written by the ``lexeme_enrich`` task from
the Wiktionary etymology (and its source-language entry); every answer,
"no fact" included, is stored. Facts below ``[facts] min_confidence`` are
kept but not shown. Lists opt out with ``facts = false`` in lists.toml.
"""

from __future__ import annotations

import sqlite3

from .settings import settings


_ARTICLES = ("il ", "lo ", "la ", "i ", "gli ", "le ", "un ", "uno ", "una ", "l'", "un'")


def normalise(word: str) -> str:
    """Fact key: lowercase, single spaces, leading article dropped — so
    'La finestra' (CILS) and 'finestra' (SUBTLEX) share one fact."""
    w = " ".join(word.strip().lower().replace("’", "'").split())
    for article in _ARTICLES:
        if w.startswith(article) and len(w) - len(article) >= 4:
            return w[len(article):].lstrip()
    return w


def shown(conn: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    """word → (fact, kind) for every fact confident enough to display."""
    rows = conn.execute(
        "SELECT word, fact, kind FROM word_facts WHERE has_fact = 1 AND confidence >= ?",
        (settings.facts.min_confidence,),
    )
    return {r[0]: (r[1], r[2]) for r in rows}
