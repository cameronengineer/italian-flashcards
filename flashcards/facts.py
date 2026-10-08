"""Fun facts ("did you know?") for words — etymology, links to English,
false friends, cultural notes.

One fact per distinct word, stored in ``word_facts`` and shared by every
source and deck that uses the word. The model is told to be selective and
to decline when unsure; every answer (including "no fact") is stored so a
word is asked about once. Facts below ``settings.facts.min_confidence`` are
kept but not shown. Cards opt in by setting ``Card.fact_word``; sources opt
out with ``"facts": false`` in sources.json.
"""

from __future__ import annotations

import sqlite3

from .settings import settings
from .tasks import WORD_FACTS
from .util import print_banner


_ARTICLES = ("il ", "lo ", "la ", "i ", "gli ", "le ", "un ", "uno ", "una ", "l'", "un'")


def normalise(word: str) -> str:
    """Fact key: lowercase, single spaces, leading article dropped — so
    'La finestra' (CILS) and 'finestra' (SUBTLEX) share one fact."""
    w = " ".join(word.strip().lower().replace("’", "'").split())
    for article in _ARTICLES:
        if w.startswith(article) and len(w) - len(article) >= 4:
            return w[len(article):].lstrip()
    return w


def generate(ctx, words: dict[str, str]) -> dict[str, int]:
    """Ask for facts about ``words`` (word → English meaning) not yet stored.

    Words go to the AI in batches of ``[facts] batch_size``; every answer,
    "no fact" included, is stored so a word is only ever asked about once.
    Words the model skipped in a batch are simply asked again next build.
    """
    have = {r[0] for r in ctx.conn.execute("SELECT word FROM word_facts")}
    todo: dict[str, str] = {}
    for w, meaning in sorted(words.items()):
        key = normalise(w)
        if key and key not in have:
            todo.setdefault(key, meaning)
    items = list(todo.items())
    size = max(1, settings.facts.batch_size)
    batches = [items[i:i + size] for i in range(0, len(items), size)]
    print_banner(f"facts — {len(items)} new word(s) to consider in {len(batches)} "
                 f"request(s) ({len(have)} already stored)")
    counts = {"facts": 0, "none": 0, "failed": 0}
    if not batches:
        return counts
    model = WORD_FACTS.task({}).model
    for batch, result in ctx.ai.run_many(
        batches,
        lambda b: WORD_FACTS.task({"words": [{"italian": w, "meaning": m} for w, m in b]}),
        workers=ctx.workers,
        label="fact batches",
        progress_every=10,
        describe=lambda b: f"{b[0][0]} … {b[-1][0]}",
    ):
        if isinstance(result, Exception):
            counts["failed"] += len(batch)
            continue
        wanted = {w for w, _ in batch}
        rows = []
        for item in result.get("facts", []):
            word = normalise(item.get("italian") or "")
            if word not in wanted:
                continue
            wanted.discard(word)
            fact = (item.get("fact") or "").strip()
            has = bool(item.get("has_fact")) and bool(fact)
            counts["facts" if has else "none"] += 1
            rows.append((word, 1 if has else 0, item.get("kind") if has else None,
                         fact if has else None, float(item.get("confidence", 0.0)), model))
        counts["failed"] += len(wanted)
        with ctx.db_lock:
            ctx.conn.executemany(
                """
                INSERT OR REPLACE INTO word_facts (word, has_fact, kind, fact, confidence, model)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            ctx.conn.commit()
    print(f"  done: {counts['facts']} fact(s), {counts['none']} without, "
          f"{counts['failed']} not answered (asked again next build)")
    return counts


def shown(conn: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    """word → (fact, kind) for every fact confident enough to display."""
    rows = conn.execute(
        "SELECT word, fact, kind FROM word_facts WHERE has_fact = 1 AND confidence >= ?",
        (settings.facts.min_confidence,),
    )
    return {r[0]: (r[1], r[2]) for r in rows}
