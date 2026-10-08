"""``review`` command — everything that deserves a human look, in one list.

* entries the AI was unsure about (``confidence`` < ``[review]
  min_confidence``);
* cards whose latest audit is ``warn`` / ``fail`` and still matches the
  card's current content;
* fun facts that were generated but hidden for low confidence.

Prints a summary and writes ``audit_reports/review_<ts>.csv``. Fix an item
by editing its CSV row (edits are picked up on the next build) or re-ask the
AI with ``flashcards build --refresh <source>``.
"""

from __future__ import annotations

import csv
from contextlib import closing
from datetime import datetime

from ..db import connect
from ..settings import settings
from ..util import print_banner, table
from .audit import REPORTS_DIR, card_hash


def run(*, limit: int = 20) -> int:
    print_banner("review — low-confidence entries, failed audits, doubtful facts")
    items: list[dict] = []
    with closing(connect()) as conn:
        for r in conn.execute(
            """
            SELECT source_path, mode, italian, english, confidence FROM entries
            WHERE retired = 0 AND confidence IS NOT NULL AND confidence < ?
            ORDER BY confidence
            """,
            (settings.review.min_confidence,),
        ):
            items.append({"kind": "low confidence", "where": f"{r['source_path']} [{r['mode']}]",
                          "italian": r["italian"], "english": r["english"],
                          "note": f"confidence {r['confidence']:.2f}"})
        cards = {(c["natural_key"], c["direction"]): c for c in conn.execute("SELECT * FROM cards")}
        for a in conn.execute(
            "SELECT * FROM audits WHERE verdict IN ('warn', 'fail') ORDER BY severity DESC"
        ):
            card = cards.get((a["natural_key"], a["direction"]))
            if card is None or card_hash(card) != a["card_hash"]:
                continue  # card gone or changed since the audit
            items.append({"kind": f"audit {a['verdict']} ({a['severity']})", "where": card["deck"],
                          "italian": card["back_highlight"] if a["direction"] == "en_to_it" else card["front_text"],
                          "english": card["front_text"] if a["direction"] == "en_to_it" else card["back_highlight"],
                          "note": f"{a['issues']} → {a['suggestion']}".strip(" →")})
        for f in conn.execute(
            "SELECT * FROM word_facts WHERE has_fact = 1 AND confidence < ? ORDER BY confidence",
            (settings.facts.min_confidence,),
        ):
            items.append({"kind": "hidden fact", "where": f["kind"] or "", "italian": f["word"],
                          "english": "", "note": f"{f['confidence']:.2f}: {f['fact']}"})

    if not items:
        print("  Nothing to review.")
        return 0
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / f"review_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with open(out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["kind", "where", "italian", "english", "note"])
        writer.writeheader()
        writer.writerows(items)
    counts: dict[str, int] = {}
    for it in items:
        key = it["kind"].split(" (")[0]
        counts[key] = counts.get(key, 0) + 1
    print(table(["Kind", "Count"], [[k, v] for k, v in counts.items()]))
    print()
    print(table(["Kind", "Italian", "English", "Note"],
                [[it["kind"], it["italian"][:30], it["english"][:30], it["note"][:70]]
                 for it in items[:limit]]))
    print(f"\n  {len(items)} item(s); full list: {out}")
    return 0
