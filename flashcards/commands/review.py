"""``review`` extra — what Claude flagged, for your information only.

Nothing here holds a card back; there is no human review step.

* words where Claude doubted the dictionary data (``lexemes.issues``);
* notes whose latest audit is warn / fail and still matches the note;
* fun facts generated but hidden for low confidence;
* AI jobs that exhausted their retries.

To change content yourself, edit ``lexicon/lexemes.jsonl`` and mark the
field ``"provenance": {"field": "human"}`` — human edits always win.
"""

from __future__ import annotations

import csv
import json
from contextlib import closing
from datetime import datetime

from ..db import connect
from ..settings import settings
from ..util import print_banner, table
from ..quality import note_hash
from .audit import REPORTS_DIR


def run(*, limit: int = 20) -> int:
    print_banner("review — flagged words, audits, hidden facts, failed jobs (information only)")
    items: list[dict] = []
    with closing(connect()) as conn:
        from ..content_review import grammar_holds

        items.extend(
            {"kind": "ignored drill", "italian": r["key"], "detail": r["reason"]} for r in grammar_holds(conn)
        )
        for r in conn.execute(
            "SELECT lemma, pos, issues FROM lexemes WHERE coalesce(issues,'[]') NOT IN ('[]','') "
            "AND id IN (SELECT lexeme_id FROM list_items) ORDER BY lemma"
        ):
            items.append(
                {
                    "kind": "flagged",
                    "italian": r["lemma"],
                    "detail": "; ".join(json.loads(r["issues"] or "[]")),
                }
            )
        notes = {
            r["key"]: json.loads(r["fields_json"])
            for r in conn.execute("SELECT key, fields_json FROM v4_notes")
        }
        for a in conn.execute(
            "SELECT * FROM audits WHERE direction = 'note' AND verdict IN ('warn', 'fail') ORDER BY severity DESC"
        ):
            f = notes.get(a["natural_key"])
            if f and note_hash(f) == a["card_hash"]:
                items.append(
                    {
                        "kind": f"audit {a['verdict']}",
                        "italian": f["Italian"],
                        "detail": f"{a['issues']} → {a['suggestion']}".strip(" →"),
                    }
                )
        for f in conn.execute(
            "SELECT word, fact, confidence FROM word_facts WHERE has_fact = 1 AND confidence < ?",
            (settings.facts.min_confidence,),
        ):
            items.append(
                {"kind": "hidden fact", "italian": f["word"], "detail": f"{f['confidence']:.2f}: {f['fact']}"}
            )
        for j in conn.execute("SELECT kind, subject, error FROM ai_jobs WHERE status = 'failed'"):
            lx = conn.execute("SELECT lemma FROM lexemes WHERE id = ?", (j["subject"],)).fetchone()
            items.append(
                {
                    "kind": f"failed {j['kind']}",
                    "italian": lx[0] if lx else j["subject"][:12],
                    "detail": (j["error"] or "")[:200],
                }
            )
    if not items:
        print("  Nothing to review.")
        return 0
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / f"review_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["kind", "italian", "detail"])
        w.writeheader()
        w.writerows(items)
    counts: dict[str, int] = {}
    for it in items:
        counts[it["kind"]] = counts.get(it["kind"], 0) + 1
    print(table(["Kind", "Count"], [[k, v] for k, v in counts.items()]))
    print()
    print(
        table(
            ["Kind", "Italian", "Detail"],
            [[i["kind"], i["italian"][:30], i["detail"][:90]] for i in items[:limit]],
        )
    )
    print(f"\n  {len(items)} item(s); full list: {out}")
    return 0
