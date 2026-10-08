"""``movie`` command — coverage report + word dataset for a film list.

Coverage = the share of the film's running words (tokens) whose root you
already know (``known``: an Anki interval ≥ 21 days) or are learning. The
report shows how many more roots — taken in order of how often the film
uses them — get you to 90 / 95 / 98 % coverage, and when you'd get there at
your plan's new-cards-per-day.

The dataset (``datasets/<list>.csv``) has one row per root: lemma, part of
speech, count, first appearance, frequency (SUBTLEX zipf), forms heard and
the example line. It stays out of git (it contains subtitle text).
"""

from __future__ import annotations

import csv
import json
import math
from collections import Counter
from contextlib import closing
from datetime import date, timedelta

from .. import srt
from ..db import connect, init_schema
from ..lexicon import lexeme_id
from ..lists import load, load_plan
from ..paths import DATASETS_DIR
from ..util import print_banner, table


def _list(list_id: str | None):
    movies = [l for l in load() if l.kind == "movie"]
    if not movies:
        raise SystemExit("No movie lists in lists.toml.")
    if list_id is None:
        return movies[0]
    for l in movies:
        if l.id == list_id:
            return l
    raise SystemExit(f"No movie list {list_id!r}; have {[l.id for l in movies]}")


def run(list_id: str | None = None, *, export: bool = True) -> int:
    l = _list(list_id)
    print_banner(f"movie — {l.title}")
    with closing(connect()) as conn:
        init_schema(conn)
        state = {}
        for r in conn.execute("SELECT lexeme_id, state FROM knowledge"):
            rank = {"unknown": 0, "learning": 1, "known": 2}
            if rank[r["state"]] > rank.get(state.get(r["lexeme_id"], "unknown"), 0):
                state[r["lexeme_id"]] = r["state"]
        stream = srt.token_stream(l.path)
        tokens = [t for t in stream if t]
        counts = Counter(lexeme_id(*t) for t in tokens)
        total = len(tokens)
        known = sum(n for lid, n in counts.items() if state.get(lid) == "known")
        learning = sum(n for lid, n in counts.items() if state.get(lid) == "learning")
        print(f"  {total} words in the film, {len(counts)} distinct roots")
        print(f"  you know {known / total:.1%} of the running words; "
              f"{(known + learning) / total:.1%} including words you're learning")
        unknown = sorted(((n, lid) for lid, n in counts.items() if state.get(lid) not in ("known",)), reverse=True)
        per_day = int(load_plan().get("new_cards_per_day", 20)) or 20
        rows = []
        covered = known
        targets = [0.90, 0.95, 0.98, 1.0]
        need = 0
        for n, _lid in unknown:
            if not targets:
                break
            covered += n
            need += 1
            while targets and covered / total >= targets[0]:
                days = math.ceil(need / per_day)
                rows.append([f"{targets[0]:.0%}", need, f"~{days} days", (date.today() + timedelta(days=days)).isoformat()])
                targets.pop(0)
        if rows:
            print()
            print(table(["Coverage", "Roots to learn", f"At {per_day}/day", "Done by"], rows))
        deadline = next((p.get("by") for p in load_plan().get("priority", []) if p.get("list") == l.id), None)
        if deadline:
            days_left = (date.fromisoformat(str(deadline)) - date.today()).days
            print(f"\n  watch date {deadline}: {days_left} days → ~{days_left * per_day} new roots at {per_day}/day")
        if export:
            DATASETS_DIR.mkdir(exist_ok=True)
            out = DATASETS_DIR / f"{l.id}.csv"
            items = {lexeme_id(i.lemma, i.pos): i for i in srt.word_items(l.path)}
            with out.open("w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["lemma", "pos", "count", "first_seen", "zipf", "known", "forms", "example", "example_at"])
                for lid, n in counts.most_common():
                    it = items.get(lid)
                    lx = conn.execute("SELECT lemma, pos, zipf FROM lexemes WHERE id = ?", (lid,)).fetchone()
                    if not it:
                        continue
                    w.writerow([it.lemma, it.pos, n, it.context.get("first_seen"), lx["zipf"] if lx else "",
                                state.get(lid, "unknown"), " ".join(it.context.get("forms", [])),
                                it.context.get("example", ""), it.context.get("example_at", "")])
            print(f"\n  dataset: {out}  (gitignored — contains subtitle lines)")
    return 0


__all__ = ["run", "json"]
