"""``movie`` command — coverage report + word dataset for a film list.

Coverage = the share of the film's running words (tokens) whose root you
already know (``known``: an Anki interval ≥ 21 days) or are learning. The
report shows how many more roots — taken in order of how often the film
uses them — get you to 90 / 95 / 98 % coverage, and the current card workload at your daily introduction limit. This is not
a prediction of learning time or comprehension.

The dataset (``datasets/<list>.csv``) has one row per root: lemma, part of
speech, count, first appearance, frequency (SUBTLEX zipf), forms heard and
the example line. It stays out of git (it contains subtitle text).
"""

from __future__ import annotations

import csv
import math
from collections import Counter
from contextlib import closing
from datetime import date

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


def coverage(counts, known_ids, unresolved, per_day, costs=None):
    """Exact denominator, including unresolved words; zero-cost attained targets."""
    total = sum(counts.values()) + unresolved
    known = sum(n for lid, n in counts.items() if lid in known_ids)
    pending = sorted(((n, lid) for lid, n in counts.items() if lid not in known_ids), reverse=True)
    rows = []
    for target in (0.9, 0.95, 0.98, 1.0):
        covered, need = known, 0
        while total and covered / total < target and need < len(pending):
            covered += pending[need][0]
            need += 1
        reached = bool(total and covered / total >= target)
        new_cards = (
            sum(costs[lid] for _n, lid in pending[:need])
            if reached and costs is not None and all(lid in costs for _n, lid in pending[:need])
            else (0 if reached and need == 0 else None)
        )
        rows.append(
            {
                "target": target,
                "roots": need if reached else None,
                "new_cards": new_cards,
                "days": math.ceil(new_cards / per_day) if new_cards is not None else None,
            }
        )
    return {"total": total, "known": known, "unresolved": unresolved, "targets": rows}


def source_mappings(conn, list_id):
    """Every pre-resolution root spelling, including collapsed memberships."""
    import json

    mappings = {
        (r["raw"], r["pos"]): r["lexeme_id"]
        for r in conn.execute(
            "SELECT li.raw,li.lexeme_id,l.pos FROM list_items li JOIN lexemes l ON l.id=li.lexeme_id WHERE li.list_id=?",
            (list_id,),
        )
    }
    for row in conn.execute("SELECT payload,lexeme_id FROM source_observations WHERE list_id=?", (list_id,)):
        item = json.loads(row["payload"])
        if item.get("lemma") and item.get("pos"):
            mappings[(item["lemma"], item["pos"])] = row["lexeme_id"]
    return mappings


def workload(conn):
    from ..notes import candidates
    from ..planning import card_plan

    rows = candidates(conn).notes
    planned = card_plan(conn, rows)
    costs = Counter()
    for row in rows:
        if row["lexeme_id"]:
            costs[row["lexeme_id"]] += planned["costs"][row["key"]]
    return costs


def headlines(conn) -> list[str]:
    """One line per installed film: how much of it you know, and what it takes to reach 90%."""
    from ..planning import known_roots

    known = known_roots(conn)
    per_day = load_plan().get("new_cards_per_day", 25)
    deadlines = {p["list"]: p.get("by") for p in load_plan().get("priority", [])}
    out = []
    for l in load():
        if l.kind != "movie" or not l.path or not l.path.exists():
            continue
        _cues, stream = srt.analyze(l.path)
        mappings = source_mappings(conn, l.id)
        counts = Counter(
            mappings.get((t.lemma, t.pos), lexeme_id(t.lemma, t.pos))
            for t in stream
            if t.status == "resolved"
        )
        cover = coverage(
            counts, known, sum(t.status == "unresolved" for t in stream), per_day, workload(conn)
        )
        if not cover["total"]:
            continue
        text = f"{l.title}: estimated vocabulary coverage {cover['known'] / cover['total']:.0%}"
        ninety = next(t for t in cover["targets"] if t["target"] == 0.9)
        if ninety["roots"]:
            text += f" · {ninety['roots']:,} more roots → 90%"
            if ninety["new_cards"] is not None:
                text += f" · {ninety['new_cards']:,} currently planned new directions ({ninety['days']} introduction days at {per_day}/day; learning time varies)"
        if deadlines.get(l.id):
            left = (date.fromisoformat(str(deadlines[l.id])) - date.today()).days
            text += f" · watching in {left} days" if left >= 0 else " · watch date passed"
        out.append(text)
    return out


def run(list_id: str | None = None, *, export: bool = True) -> int:
    from ..planning import known_roots

    l = _list(list_id)
    if not l.path or not l.path.exists():
        print(f"Movie source is optional and not installed: {l.path}")
        return 2
    print_banner(f"movie — {l.title}")
    with closing(connect()) as conn:
        init_schema(conn)
        _cues, stream = srt.analyze(l.path)
        buckets = Counter(t.status for t in stream)
        # Use persisted membership resolution so reports and actual cards have identical IDs.
        mappings = source_mappings(conn, l.id)
        counts = Counter(
            mappings.get((t.lemma, t.pos), lexeme_id(t.lemma, t.pos))
            for t in stream
            if t.status == "resolved"
        )
        known = known_roots(conn)
        per_day = load_plan().get("new_cards_per_day", 25)
        report = coverage(counts, known, buckets["unresolved"], per_day, workload(conn))
        stamp = conn.execute("SELECT value FROM metadata WHERE key='knowledge_synced_at'").fetchone()
        print(f"  token buckets: {dict(buckets)}; knowledge updated: {stamp[0] if stamp else 'never'}")
        if not report["total"]:
            print("  No study tokens in this subtitle file.")
            return 0
        print(
            f"  recognition coverage: {report['known'] / report['total']:.1%}; unresolved: {report['unresolved'] / report['total']:.1%}"
        )
        rows = []
        for t in report["targets"]:
            rows.append(
                [
                    f"{t['target']:.0%}",
                    t["roots"]
                    if t["roots"] is not None
                    else "unreachable until unresolved tokens are resolved",
                    t["new_cards"] if t["new_cards"] is not None else "not yet planned",
                    t["days"] if t["days"] is not None else "—",
                ]
            )
        print(
            table(
                ["Vocabulary coverage", "Additional roots", "New card directions", "Introduction days"], rows
            )
        )
        print(
            "  Workload includes currently planned recognition, production and drills, excluding directions already in Anki. Enrichment can expand it. Introduction days allocate the whole daily budget; they are not learning time or a comprehension forecast."
        )
        deadline = next((p.get("by") for p in load_plan().get("priority", []) if p["list"] == l.id), None)
        if deadline:
            left = (date.fromisoformat(str(deadline)) - date.today()).days
            print(
                f"  watch date {deadline}: {max(0, left)} days available" + (" (overdue)" if left < 0 else "")
            )
        if export:
            DATASETS_DIR.mkdir(exist_ok=True)
            out = DATASETS_DIR / f"{l.id}.csv"
            with out.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(
                    [
                        "status",
                        "lemma",
                        "pos",
                        "count",
                        "first_seen",
                        "known",
                        "forms",
                        "example",
                        "example_at",
                    ]
                )
                for item in srt.word_items(l.path):
                    lid = mappings.get((item.lemma, item.pos), lexeme_id(item.lemma, item.pos))
                    writer.writerow(
                        [
                            "resolved",
                            item.lemma,
                            item.pos,
                            item.context["count"],
                            item.context["first_seen"],
                            lid in known,
                            " ".join(item.context["forms"]),
                            item.context["example"],
                            item.context["example_at"],
                        ]
                    )
                other = Counter((t.status, t.text.lower()) for t in stream if t.status != "resolved")
                for (status, token), count in sorted(other.items()):
                    writer.writerow([status, token, "", count, "", "", "", "", ""])
            print(f"  local dataset: {out}")
    return 0
