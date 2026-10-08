"""``audit`` command — AI review of the generated cards.

Each card (both directions) goes to the ``card_audit`` task, which checks
correctness, grammar, naturalness, consistency and — if present — the
accuracy of the fun fact. Verdicts are stored in the ``audits`` table
(with a hash of the audited content, so a later change to the card makes
the verdict stale) and written to ``audit_reports/audit_report_<ts>.csv``.
Answers are cached: re-auditing an unchanged card is free.
"""

from __future__ import annotations

import csv
import os
from contextlib import closing
from datetime import datetime
from pathlib import Path

from ..ai import AI
from ..db import connect
from ..paths import PROJECT_ROOT
from ..settings import settings
from ..tasks import CARD_AUDIT
from ..util import md5_hex, print_banner

REPORTS_DIR = PROJECT_ROOT / "audit_reports"
FIELDS = ("front_text", "front_labels", "back_highlight", "back_text", "fact", "audio_text")
DIRECTION_DESC = {
    "en_to_it": "English prompt → Italian answer (the learner recalls the Italian)",
    "it_to_en": "Italian prompt → English answer (the learner recalls the English)",
}


def card_hash(card) -> str:
    return md5_hex("\x1f".join(str(card[f] or "") for f in FIELDS))


def _task(card):
    return CARD_AUDIT.task({
        "deck": card["deck"],
        "direction": DIRECTION_DESC.get(card["direction"], card["direction"]),
        **{f: card[f] or "" for f in FIELDS},
    })


def run(*, decks: list[str] | None = None, limit: int | None = None, workers: int | None = None,
        only_problems: bool = False, out: Path | None = None) -> int:
    print_banner("audit — AI review of generated cards")
    workers = workers or settings.audit.workers
    with closing(connect()) as conn:
        where, params = "", []
        if decks:
            where = f"WHERE deck IN ({','.join('?' * len(decks))})"
            params = list(decks)
        sql = f"SELECT * FROM cards {where} ORDER BY deck, sort_order, direction"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        cards = conn.execute(sql, params).fetchall()
        if not cards:
            print("  No cards matched. Nothing to audit.")
            return 0
        model = CARD_AUDIT.task({}).model
        print(f"  Auditing {len(cards)} cards with model '{model}'.")

        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        out = out or REPORTS_DIR / f"audit_report_{datetime.now():%Y%m%d_%H%M%S}.csv"
        columns = ["deck", "natural_key", "direction", "verdict", "severity", "categories",
                   "issues", "suggestion", *FIELDS]
        counts = {"pass": 0, "warn": 0, "fail": 0, "error": 0}
        written = 0
        ai = AI(conn)
        with open(out, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns)
            writer.writeheader()
            for card, res in ai.run_many(
                cards, _task, workers=workers, label="cards",
                describe=lambda c: (c["back_highlight"] or c["front_text"] or "")[:50],
            ):
                base = {k: card[k] or "" for k in ("deck", "natural_key", "direction", *FIELDS)}
                if isinstance(res, Exception):
                    counts["error"] += 1
                    row = {**base, "verdict": "error", "issues": str(res)}
                else:
                    verdict = res.get("verdict", "pass")
                    counts[verdict] = counts.get(verdict, 0) + 1
                    row = {**base, "verdict": verdict, "severity": res.get("severity", 0),
                           "categories": ", ".join(res.get("categories") or []),
                           "issues": res.get("issues") or "", "suggestion": res.get("suggestion") or ""}
                    with ai.lock:
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO audits (natural_key, direction, card_hash, verdict,
                                severity, categories, issues, suggestion, model)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (card["natural_key"], card["direction"], card_hash(card), verdict,
                             row["severity"], row["categories"], row["issues"], row["suggestion"], model),
                        )
                        conn.commit()
                if only_problems and row["verdict"] == "pass":
                    continue
                # Flush per row so an interrupted run keeps every result so far.
                writer.writerow(row)
                fh.flush()
                os.fsync(fh.fileno())
                written += 1
    print(f"\n  Done. {len(cards)} audited — pass={counts['pass']}, warn={counts['warn']}, "
          f"fail={counts['fail']}, error={counts['error']}.")
    print(f"  Report: {out}  ({written} rows). Verdicts saved; see `flashcards review`.")
    return 0
