"""Offline data-quality census of a consistent SQLite snapshot.

No migrations, provider calls, Anki calls or media writes. Examples are stable
record keys, never subtitle lines or private learner text. A zero exit status
means structural checks passed, not that translations have been certified.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .. import paths, quality, content_review
from ..assets import locate
from ..db import SCHEMA_VERSION
from ..runtime import atomic_json


def report(conn, *, check_media=True):
    """Analyze one fixed connection; callers supply a snapshot/read transaction."""
    result = {"format": 1, "captured_at": datetime.now(timezone.utc).isoformat(), "findings": []}
    findings = result["findings"]

    def finding(code, severity, count, detail, examples=()):
        if count:
            findings.append(
                {
                    "code": code,
                    "severity": severity,
                    "count": count,
                    "detail": detail,
                    "examples": list(examples)[:10],
                }
            )

    result["schema"] = conn.execute("PRAGMA user_version").fetchone()[0]
    if result["schema"] != SCHEMA_VERSION:
        raise ValueError(f"data-review requires schema {SCHEMA_VERSION}; it never migrates the database")
    result["integrity"] = [r[0] for r in conn.execute("PRAGMA integrity_check")]
    result["foreign_key_violations"] = [list(r) for r in conn.execute("PRAGMA foreign_key_check")]
    finding("database_integrity", "error", int(result["integrity"] != ["ok"]), "SQLite integrity failed")
    finding("foreign_keys", "error", len(result["foreign_key_violations"]), "Broken relational references")
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    result["table_counts"] = {t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}
    checkpoints = dict(conn.execute("SELECT key,value FROM metadata"))
    result["checkpoints"] = {k: v for k, v in checkpoints.items() if k != "anki_profile"}
    stale = bool(
        checkpoints.get("source_generation")
        and checkpoints.get("source_generation") != checkpoints.get("notes_generation")
    )
    finding(
        "stale_notes",
        "warning",
        int(stale),
        "Upstream data changed since the last note build; this can be normal between worker batches",
    )
    finding(
        "unversioned_dictionary",
        "warning",
        int(not checkpoints.get("dictionary_version")),
        "The database checkpoint does not identify its dictionary version",
    )
    result["lexemes"] = [
        dict(r)
        for r in conn.execute(
            "SELECT status,count(*) AS count FROM lexemes WHERE id IN (SELECT lexeme_id FROM list_items) GROUP BY status"
        )
    ]
    orphaned = conn.execute(
        "SELECT count(*) FROM lexemes WHERE id NOT IN (SELECT lexeme_id FROM list_items)"
    ).fetchone()[0]
    finding(
        "retained_inactive_roots",
        "info",
        orphaned,
        "Roots without current membership are retained; do not delete their media or study history",
    )
    result["lists"] = [
        dict(r)
        for r in conn.execute(
            "SELECT l.id,l.kind,(SELECT count(*) FROM source_observations o WHERE o.list_id=l.id) AS observations,"
            "count(li.lexeme_id) AS memberships FROM lists l LEFT JOIN list_items li ON li.list_id=l.id GROUP BY l.id"
        )
    ]
    result["jobs"] = [
        dict(r)
        for r in conn.execute("SELECT kind,status,count(*) AS count FROM ai_jobs GROUP BY kind,status")
    ]
    failed = [r[0] for r in conn.execute("SELECT id FROM ai_jobs WHERE status='failed' ORDER BY id")]
    finding("failed_jobs", "warning", len(failed), "Jobs require diagnosis and an explicit retry", failed)
    active_senses = conn.execute("SELECT count(*) FROM senses WHERE active=1").fetchone()[0]
    missing_lineage = conn.execute(
        "SELECT count(*) FROM senses WHERE active=1 AND (source_id IS NULL OR source_id='')"
    ).fetchone()[0]
    unverified = conn.execute(
        "SELECT count(*) FROM senses WHERE active=1 AND (source_id LIKE 'legacy:%' OR source_id LIKE 'unverified:%')"
    ).fetchone()[0]
    result["senses"] = {
        "active": active_senses,
        "without_source_id": missing_lineage,
        "legacy_or_unverified_source": unverified,
    }
    finding(
        "unverified_meaning_sources",
        "warning",
        unverified,
        "Stable IDs preserve history but still need a verified dictionary/source match",
    )
    swapped = []
    for row in conn.execute("SELECT list_id,row_key,payload FROM source_observations"):
        item = json.loads(row["payload"])
        if content_review.language_direction_warning(item.get("raw", ""), item.get("english") or ""):
            swapped.append(f"{row['list_id']}:{row['row_key']}")
    finding(
        "possible_swapped_languages",
        "warning",
        len(swapped),
        "Review source language direction; loanwords/mixed text are allowed",
        swapped,
    )
    result["audit_coverage"] = content_review.audit_coverage(conn)
    result["grammar_holds"] = content_review.grammar_holds(conn)
    finding(
        "grammar_holds",
        "warning",
        len(result["grammar_holds"]),
        "Meaning-specific constraints hold these published/planned drills; study history is preserved",
        [r["key"] for r in result["grammar_holds"]],
    )
    finding(
        "untraced_senses",
        "warning",
        missing_lineage,
        "Active meanings do not identify the dictionary sense/source observation supporting them",
    )

    audits = {r["natural_key"]: r for r in conn.execute("SELECT * FROM audits WHERE direction='note'")}
    counts = Counter()
    blocks = Counter()
    media = set()
    missing = []
    readiness_conflicts = []
    production = defaultdict(list)
    directions = Counter()
    unaudited = []
    no_audio = []
    ready_count = 0
    for row in conn.execute("SELECT * FROM v4_notes ORDER BY key"):
        counts[(row["card_type"], bool(row["ready"]))] += 1
        blocks.update(x for x in (row["blocked_by"] or "").split(",") if x)
        try:
            fields = json.loads(row["fields_json"])
            if not isinstance(fields, dict) or any(not isinstance(v, str) for v in fields.values()):
                raise ValueError("note fields must be strings")
        except (ValueError, TypeError):
            finding("invalid_fields", "error", 1, "Invalid note field JSON", [row["key"]])
            continue
        if not row["ready"]:
            continue
        ready_count += 1
        if quality.reasons(conn, row["key"], row["lexeme_id"], fields):
            readiness_conflicts.append(row["key"])
        for direction in ("Recognition", "Production"):
            directions[direction] += bool(fields.get(direction))
        if not fields.get("Audio"):
            no_audio.append(row["key"])
        audited = audits.get(row["key"])
        if not audited or audited["card_hash"] != quality.note_hash(fields):
            unaudited.append(row["key"])
        if fields.get("Production"):
            # Compare visible text cues. Images may distinguish the cards, so
            # these are candidates for review, not automatic translation errors.
            cue = tuple(
                " ".join(quality.text(fields.get(f, "")).casefold().split())
                for f in ("English", "Hint", "Labels")
            )
            production[cue].append((row["key"], quality.text(fields.get("Italian"))))
        for value in fields.values():
            media.update(a or b for a, b in re.findall(r'<img src="([^"]+)"|\[sound:([^\]]+)\]', value))
    result["notes"] = [{"type": t, "ready": ready, "count": n} for (t, ready), n in sorted(counts.items())]
    result["ready_directions"] = dict(directions)
    result["block_reasons"] = dict(blocks)
    result["ready_count"] = ready_count
    finding(
        "readiness_conflicts",
        "error",
        len(readiness_conflicts),
        "Stored ready flags disagree with the current publication gate; rebuild notes",
        readiness_conflicts,
    )
    finding(
        "unaudited_ready_notes",
        "warning",
        len(unaudited),
        "Ready means publishable; these notes have no matching independent note audit",
        unaudited,
    )
    finding(
        "ready_without_audio", "info", len(no_audio), "Audio is optional and separately budgeted", no_audio
    )
    collisions = [items for items in production.values() if len({answer for _key, answer in items}) > 1]
    result["production_collision_groups"] = [[key for key, _answer in items] for items in collisions]
    finding(
        "ambiguous_production_cues",
        "warning",
        len(collisions),
        "Identical English/hint/labels lead to different answers; images may differ",
        [items[0][0] for items in collisions],
    )
    if check_media:
        for name in sorted(media):
            try:
                valid = locate(name)
            except ValueError:
                valid = None
            if not valid:
                missing.append(name)
    result["media"] = {"referenced_files": len(media), "checked": check_media, "missing_or_invalid": missing}
    finding(
        "missing_media", "error", len(missing), "Ready notes reference unavailable/invalid media", missing
    )
    result["knowledge"] = [
        dict(r)
        for r in conn.execute(
            "SELECT direction,state,count(*) AS count FROM card_knowledge GROUP BY direction,state"
        )
    ]
    result["status"] = (
        "error" if any(f["severity"] == "error" for f in findings) else "review" if findings else "ok"
    )
    return result


def collect(database: Path | None = None, *, check_media=True):
    database = (database or paths.DB_PATH).resolve()
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(":memory:")) as snapshot:
            source.backup(snapshot)
            snapshot.row_factory = sqlite3.Row
            return report(snapshot, check_media=check_media)


def run(*, database=None, out=None, check_media=True):
    result = collect(database, check_media=check_media)
    if out:
        atomic_json(out, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(result["status"] == "error")
