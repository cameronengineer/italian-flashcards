"""``audit`` command — AI review of the v4 notes, ~20 per Claude call.

Checks correctness, grammar, naturalness, consistency and — where present —
the accuracy of the fun fact. Verdicts are stored in ``audits`` with a hash
of the audited content (a later change makes the verdict stale) and written
to ``audit_reports/audit_report_<ts>.csv``. Cached: unchanged notes are free.
"""

from __future__ import annotations

import csv
import html
import json
import re
from contextlib import closing
from datetime import datetime
from pathlib import Path

from ..ai import AI
from ..db import connect
from ..paths import PROJECT_ROOT
from ..settings import settings
from ..tasks import CARD_AUDITS
from ..util import md5_hex, print_banner

REPORTS_DIR = PROJECT_ROOT / "audit_reports"
AUDITED = ("Italian", "English", "Hint", "Details", "Forms", "Note", "Fact")
BATCH = 20


def _text(v: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", v or "")).strip()


def note_hash(fields: dict) -> str:
    return md5_hex("\x1f".join(_text(fields.get(f, "")) for f in AUDITED))


def run(*, decks: list[str] | None = None, limit: int | None = None, workers: int | None = None,
        only_problems: bool = False, out: Path | None = None) -> int:
    print_banner("audit — AI review of v4 notes")
    workers = workers or settings.ai.concurrency
    with closing(connect()) as conn:
        sql = "SELECT key, deck, fields_json FROM v4_notes WHERE ready = 1"
        params: list = []
        if decks:
            sql += f" AND deck IN ({','.join('?' * len(decks))})"
            params = list(decks)
        sql += " ORDER BY sort_order"
        if limit:
            sql += f" LIMIT {int(limit)}"
        notes = [(r["key"], r["deck"], json.loads(r["fields_json"])) for r in conn.execute(sql, params)]
        done = {r["natural_key"]: r["card_hash"] for r in conn.execute("SELECT natural_key, card_hash FROM audits")}
        notes = [n for n in notes if done.get(n[0]) != note_hash(n[2])]
        if not notes:
            print("  Nothing new to audit.")
            return 0
        model = CARD_AUDITS.task({}).model
        print(f"  Auditing {len(notes)} notes with '{model}' in batches of {BATCH}.")
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        out = out or REPORTS_DIR / f"audit_report_{datetime.now():%Y%m%d_%H%M%S}.csv"
        ai = AI(conn)
        counts = {"pass": 0, "warn": 0, "fail": 0, "error": 0}
        batches = [notes[i:i + BATCH] for i in range(0, len(notes), BATCH)]
        with open(out, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["deck", "key", "italian", "english", "verdict", "severity", "categories", "issues", "suggestion"])
            w.writeheader()
            make = lambda b: CARD_AUDITS.task({"cards": [  # noqa: E731
                {"id": k, "deck": d, **{f.lower(): _text(fl.get(f, "")) for f in AUDITED if fl.get(f)}} for k, d, fl in b]})
            for batch, res in ai.run_many(batches, make, workers=workers, label="audit batches", progress_every=10):
                if isinstance(res, Exception):
                    counts["error"] += len(batch)
                    continue
                by_id = {c["id"]: c for c in res.get("cards", [])}
                with ai.lock:
                    for key, deck, fields in batch:
                        v = by_id.get(key)
                        if not v:
                            counts["error"] += 1
                            continue
                        counts[v["verdict"]] += 1
                        conn.execute(
                            "INSERT OR REPLACE INTO audits (natural_key, direction, card_hash, verdict, severity, categories, issues, suggestion, model) VALUES (?, 'note', ?, ?, ?, ?, ?, ?, ?)",
                            (key, note_hash(fields), v["verdict"], v["severity"], ", ".join(v.get("categories") or []),
                             v.get("issues") or "", v.get("suggestion") or "", model),
                        )
                        if not (only_problems and v["verdict"] == "pass"):
                            w.writerow({"deck": deck, "key": key, "italian": _text(fields["Italian"]), "english": _text(fields["English"]),
                                        "verdict": v["verdict"], "severity": v["severity"], "categories": ", ".join(v.get("categories") or []),
                                        "issues": v.get("issues"), "suggestion": v.get("suggestion")})
                    conn.commit()
                    fh.flush()
    print(f"\n  pass={counts['pass']} warn={counts['warn']} fail={counts['fail']} error={counts['error']} · report: {out}")
    return 0
