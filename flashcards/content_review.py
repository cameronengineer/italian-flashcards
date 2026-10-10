"""Targeted audit selection and versioned, human-reviewed production-cue corrections."""

import csv
import io
import json
import re
from collections import defaultdict

from . import paths, quality
from .runtime import atomic_text

COLUMNS = [
    "key",
    "italian",
    "english",
    "hint",
    "labels",
    "image",
    "content_hash",
    "new_hint",
    "accepted_alternatives",
    "reason",
    "apply",
    "review_status",
]


def collisions(rows):
    groups = defaultdict(list)
    for row in rows:
        fields = row.get("fields") or json.loads(row["fields_json"])
        if not fields.get("Production") or not fields.get("English"):
            continue
        cue = tuple(
            " ".join(quality.text(fields.get(k, "")).casefold().split())
            for k in ("English", "Hint", "Labels")
        )
        groups[cue].append((row["key"], fields))
    return [g for g in groups.values() if len({quality.text(f["Italian"]) for _k, f in g}) > 1]


def language_direction_warning(italian, english):
    """A review hint only: mixed language and loanwords are allowed."""
    en = {
        "the",
        "this",
        "that",
        "would",
        "could",
        "have",
        "please",
        "where",
        "what",
        "with",
        "some",
        "how",
        "your",
        "you",
    }
    it = {
        "il",
        "lo",
        "gli",
        "una",
        "della",
        "degli",
        "delle",
        "sono",
        "posso",
        "vorrei",
        "avere",
        "per",
        "con",
        "cosa",
        "dove",
        "questo",
    }
    left, right = (set(re.findall(r"[a-zàèéìòù]+", s.casefold())) for s in (italian, english))
    return len(left & en) >= 2 and len(right & it) >= 2 and len(left & it) < len(right & it)


def audit_priority(conn, rows):
    ambiguous = {key for g in collisions(rows) for key, _fields in g}
    unusual = {
        r[0]
        for r in conn.execute(
            "SELECT lexeme_id FROM senses WHERE active=1 AND (json_extract(features,'$.plural_gender') != json_extract(features,'$.singular_gender'))"
        )
    }

    for row in conn.execute("SELECT id,gender,forms_json FROM lexemes WHERE pos='noun'"):
        if any(
            f.get("number") == "plural" and f.get("gender") and f["gender"] != row["gender"]
            for f in json.loads(row["forms_json"] or "{}").get("noun_forms", [])
        ):
            unusual.add(row["id"])

    def rank(row):
        high_risk = (
            row["key"] in ambiguous
            or row.get("card_type") == "form"
            or row.get("lexeme_id") in unusual
            or "cafe" in row.get("tags", "")
        )
        return (not high_risk, row.get("sort_order", 0), row["key"])

    return sorted(rows, key=rank)


def audit_coverage(conn):
    audits = {r["natural_key"]: r for r in conn.execute("SELECT * FROM audits WHERE direction='note'")}
    result = {"publishable": 0, "audited": 0, "unaudited": 0, "pass": 0, "warn": 0, "fail": 0}
    for row in conn.execute("SELECT key,fields_json FROM v4_notes WHERE ready=1"):
        result["publishable"] += 1
        audit = audits.get(row["key"])
        if audit and audit["card_hash"] == quality.note_hash(json.loads(row["fields_json"])):
            result["audited"] += 1
            result[audit["verdict"]] += 1
        else:
            result["unaudited"] += 1
    return result


def apply_overrides(conn, key, fields):
    row = conn.execute("SELECT * FROM cue_overrides WHERE key=?", (key,)).fetchone()
    if row and row["content_hash"] == quality.note_hash(fields):
        if row["hint"]:
            import html

            fields["Hint"] = html.escape(row["hint"])
        if row["alternatives"]:
            import html

            fields["Note"] = " · ".join(
                filter(None, [fields.get("Note"), "Also accepted: " + html.escape(row["alternatives"])])
            )


def _rows(path):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def apply_file(conn):
    path = paths.PROJECT_ROOT / "cue_review.csv"
    done = 0
    for row in _rows(path):
        if row.get("apply", "").strip().casefold() not in {"yes", "y", "true", "1"}:
            continue
        current = conn.execute("SELECT fields_json FROM v4_notes WHERE key=?", (row.get("key"),)).fetchone()
        if not current or row.get("content_hash") != quality.note_hash(json.loads(current[0])):
            continue
        if not row.get("reason", "").strip() or not (
            row.get("new_hint", "").strip() or row.get("accepted_alternatives", "").strip()
        ):
            continue
        conn.execute(
            "INSERT OR REPLACE INTO cue_overrides(key,content_hash,hint,alternatives,reason) VALUES(?,?,?,?,?)",
            (
                row["key"],
                row["content_hash"],
                row.get("new_hint", "").strip(),
                row.get("accepted_alternatives", "").strip(),
                row["reason"].strip(),
            ),
        )
        done += 1
    if done:
        conn.execute("DELETE FROM metadata WHERE key='notes_generation'")
    conn.commit()
    return done


def write_file(conn):
    path = paths.PROJECT_ROOT / "cue_review.csv"
    old = {r["key"]: r for r in _rows(path) if r.get("key")}
    output = []
    rows = [dict(r) for r in conn.execute("SELECT * FROM v4_notes WHERE ready=1")]
    for group in collisions(rows):
        for key, fields in group:
            previous = old.pop(key, {})
            digest = quality.note_hash(fields)
            saved = conn.execute("SELECT * FROM cue_overrides WHERE key=?", (key,)).fetchone()
            # An explicitly accepted alternative resolves the ambiguity for this
            # revision; when the base changes the override ceases to apply.
            if (
                saved
                and saved["alternatives"]
                and ("Also accepted: " + saved["alternatives"]) in quality.text(fields.get("Note"))
            ):
                continue
            row = dict(
                zip(
                    COLUMNS[:7],
                    [
                        key,
                        quality.text(fields.get("Italian")),
                        quality.text(fields.get("English")),
                        quality.text(fields.get("Hint")),
                        quality.text(fields.get("Labels")),
                        fields.get("Image", ""),
                        digest,
                    ],
                    strict=True,
                )
            )
            for col in COLUMNS[7:]:
                row[col] = previous.get(col, "")
            if previous and previous.get("content_hash") != digest:
                row["apply"] = ""
                row["review_status"] = (
                    "Content changed; proposed correction retained, review and mark yes again"
                )
            elif previous.get("apply"):
                row["review_status"] = "Not applied: check content version, correction and reason"
            output.append(row)
    for key, row in old.items():
        if any(row.get(col) for col in ("new_hint", "accepted_alternatives", "reason", "apply")):
            saved = conn.execute("SELECT content_hash FROM cue_overrides WHERE key=?", (key,)).fetchone()
            if not saved or saved[0] != row.get("content_hash"):
                row["review_status"] = (
                    "Unprocessed correction retained; note is no longer in the current collision list"
                )
                output.append({k: row.get(k, "") for k in COLUMNS})
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(output)
    atomic_text(path, buf.getvalue())
    return len(output)


def grammar_holds(conn):
    from .semantics import note_hold

    keys = {
        r[0]
        for r in conn.execute(
            "SELECT key FROM identity UNION SELECT key FROM adoptions UNION SELECT key FROM v4_notes"
        )
    }
    return [{"key": key, "reason": reason} for key in sorted(keys) if (reason := note_hold(conn, key))]


def write_grammar_file(conn):
    rows = grammar_holds(conn)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["key", "reason"])
    writer.writeheader()
    writer.writerows(rows)
    atomic_text(paths.PROJECT_ROOT / "grammar_review.csv", buf.getvalue())
    return len(rows)
