"""``review`` command — everything that needs a human look, in one list.

* roots Claude disputed while verifying the dictionary data (``needs_review``)
  — these are held back from Anki until approved;
* notes whose latest audit is warn / fail and still matches the note;
* fun facts generated but hidden for low confidence;
* AI jobs that failed three times.

``flashcards review approve <lemma|--all>`` releases held roots. To fix
content instead, edit ``lexicon/lexemes.jsonl`` and mark the field
``"provenance": {"field": "human"}`` — human edits always win.
"""

from __future__ import annotations

import csv
import io
import json
from contextlib import closing
from datetime import datetime

from ..db import connect
from ..paths import PROJECT_ROOT
from ..runtime import atomic_text
from .. import report
from ..settings import settings
from ..util import print_banner, table, md5_hex
from ..quality import note_hash
from .audit import REPORTS_DIR


def run(*, limit: int = 20) -> int:
    print_banner("review — disputed roots, failed audits, hidden facts, failed jobs")
    items: list[dict] = []
    with closing(connect()) as conn:
        for r in conn.execute(
            "SELECT lemma, pos, issues FROM lexemes WHERE status = 'needs_review' ORDER BY lemma"
        ):
            items.append(
                {
                    "kind": "disputed",
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


def content_hash(conn, row) -> str:
    """The content the user actually reviewed, including meanings and source evidence."""
    fields = (
        "lemma",
        "pos",
        "display",
        "gender",
        "plural",
        "english_plural",
        "forms_json",
        "issues",
        "source_hash",
    )
    data = {k: row[k] for k in fields}
    data["senses"] = [
        dict(s)
        for s in conn.execute(
            "SELECT idx,prompt,hint,register,note,also,source_id,context,features FROM senses "
            "WHERE lexeme_id=? AND active=1 ORDER BY idx",
            (row["id"],),
        )
    ]
    return md5_hex(json.dumps(data, sort_keys=True, ensure_ascii=False))


def _approve(conn, rows, reason: str) -> None:
    """Record approval of the checked revision; approval does not correct its grammar."""
    for row in rows:
        conn.execute(
            "INSERT OR REPLACE INTO review_decisions(subject,content_hash,decision,reason) VALUES(?,?,'approve',?)",
            (row["id"], content_hash(conn, row), reason),
        )
        conn.execute("UPDATE lexemes SET status='ready',issues=NULL WHERE id=?", (row["id"],))
    if rows:
        conn.execute("DELETE FROM metadata WHERE key='notes_generation'")


REVIEW_FILE = PROJECT_ROOT / "review.csv"
_YES = {"y", "yes", "x", "1", "true", "ok", "✓", "approve", "approved"}
_COLUMNS = ["approve", "word", "part_of_speech", "why_held", "id", "content_hash", "review_status"]


def _review_rows():
    if not REVIEW_FILE.exists():
        return []
    with REVIEW_FILE.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def apply_review_file(conn) -> int:
    """Only approve the exact exported revision. Legacy/unversioned marks require review."""
    approved, stale = [], 0
    for mark in _review_rows():
        if (mark.get("approve") or "").strip().lower() not in _YES or not mark.get("id"):
            continue
        row = conn.execute(
            "SELECT * FROM lexemes WHERE id=? AND status='needs_review'", (mark["id"],)
        ).fetchone()
        if row is None:
            continue
        if mark.get("content_hash") != content_hash(conn, row):
            stale += 1
            continue
        approved.append(row)
    _approve(conn, approved, "Approved the exported revision in review.csv")
    conn.commit()
    if stale:
        report.line(
            f"review.csv: {stale} stale/unversioned approval(s) held; review the refreshed rows again"
        )
    return len(approved)


def write_review_file(conn) -> int:
    """Refresh held content without losing unprocessed marks or silently reusing stale ones."""
    previous = {r.get("id"): r for r in _review_rows() if r.get("id")}
    rows = conn.execute(
        "SELECT * FROM lexemes WHERE status='needs_review' "
        "AND id IN (SELECT lexeme_id FROM list_items) ORDER BY lemma"
    ).fetchall()
    output = []
    for row in rows:
        old = previous.pop(row["id"], {})
        digest = content_hash(conn, row)
        mark = old.get("approve", "")
        same = old.get("content_hash") == digest
        status = old.get("review_status", "") if same else ""
        if mark and not same:
            status = f"Previous mark {mark!r} not applied: content changed or version missing; review again"
        output.append(
            dict(
                zip(
                    _COLUMNS,
                    [
                        mark if same else "",
                        row["lemma"],
                        row["pos"],
                        "; ".join(json.loads(row["issues"] or "[]")),
                        row["id"],
                        digest,
                        status,
                    ],
                    strict=True,
                )
            )
        )
    # Preserve marked rows outside the current list, unknown IDs and non-approval
    # notes. Remove only revisions whose approval was durably recorded.
    for old in previous.values():
        if not old.get("approve"):
            continue
        applied = conn.execute(
            "SELECT 1 FROM review_decisions WHERE subject=? AND content_hash=? AND decision='approve'",
            (old["id"], old.get("content_hash", "")),
        ).fetchone()
        if not applied:
            old["review_status"] = (
                "Mark retained: this word is not currently awaiting review in an active list"
            )
            output.append({k: old.get(k, "") for k in _COLUMNS})
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_COLUMNS)
    writer.writeheader()
    writer.writerows(output)
    atomic_text(REVIEW_FILE, buf.getvalue())
    return len(rows)


def approve(*, lemmas: list[str], all_: bool = False, reason: str = "Reviewed by user") -> int:
    from .. import notes, lexicon

    if not reason.strip():
        raise ValueError("An approval reason is required")
    with closing(connect()) as conn:
        rows = conn.execute("SELECT * FROM lexemes WHERE status='needs_review'").fetchall()
        selected = [r for r in rows if all_ or r["lemma"] in lemmas or r["id"] in lemmas]
        _approve(conn, selected, reason)
        # Explicit note keys can approve a current failed audit without erasing the verdict.
        for key in lemmas:
            r = conn.execute("SELECT fields_json FROM v4_notes WHERE key=?", (key,)).fetchone()
            if r:
                conn.execute(
                    "INSERT OR REPLACE INTO review_decisions(subject,content_hash,decision,reason) VALUES(?,?,'approve',?)",
                    (key, note_hash(json.loads(r[0])), reason),
                )
        conn.commit()
        notes.build(conn)
        lexicon.export_jsonl(conn)
    print(f"  approved {len(selected)} roots; notes rebuilt")
    return 0
