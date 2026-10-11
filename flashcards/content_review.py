"""Audit selection, ambiguity detection and grammar-hold reporting (no human review step)."""

import json
import re
from collections import defaultdict

from . import quality


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
    """Retain previously saved human corrections without a manual review gate."""
    import html

    row = conn.execute("SELECT * FROM cue_overrides WHERE key=?", (key,)).fetchone()
    if row and row["content_hash"] == quality.note_hash(fields):
        if row["hint"]:
            fields["Hint"] = html.escape(row["hint"])
        if row["alternatives"]:
            fields["Note"] = " · ".join(
                filter(None, [fields.get("Note"), "Also accepted: " + html.escape(row["alternatives"])])
            )


def grammar_holds(conn):
    from .semantics import note_hold, form_suffix

    keys = {
        r[0]
        for r in conn.execute(
            "SELECT key FROM identity UNION SELECT key FROM adoptions UNION SELECT key FROM v4_notes"
        )
    }
    # An AI decision to ignore a form is a finished outcome even if that form
    # was never published. Make its reason inspectable alongside curated holds.
    for row in conn.execute(
        "SELECT x.lexeme_id,x.tense,x.person,s.idx FROM form_exclusions x JOIN senses s "
        "ON s.lexeme_id=x.lexeme_id AND s.source_id=x.source_id WHERE s.active=1 "
        "AND s.idx=(SELECT min(s2.idx) FROM senses s2 WHERE s2.lexeme_id=s.lexeme_id AND s2.active=1)"
    ):
        keys.add(f"form:{row['lexeme_id']}:{row['tense']}:{row['person']}{form_suffix(row)}")
    return [
        {"key": key, "action": "ignore", "reason": reason}
        for key in sorted(keys)
        if (reason := note_hold(conn, key))
    ]
