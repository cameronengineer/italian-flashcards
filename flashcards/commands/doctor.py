"""Read-only workspace health, study budget, and tool-version report."""

import importlib.metadata
import json
import shutil
import sqlite3
import subprocess
from contextlib import closing

from .. import paths
from ..db import SCHEMA_VERSION
from ..lists import load, validate, load_plan
from ..runtime import atomic_json
from ..settings import settings


def report():
    config = load_plan()
    result = {
        "workspace": str(paths.PROJECT_ROOT),
        "database": str(paths.DB_PATH),
        "configuration_errors": validate(load()),
        "tools": {},
        "dependencies": {},
        "daily_card_budget": config.get("new_cards_per_day", 25),
        "horizon_days": config.get("horizon_days"),
        "assets_per_run": {"images": settings.run.image_limit, "audio": settings.run.audio_limit},
    }
    for name in ("claude", "codex", "ffmpeg"):
        path = shutil.which(name)
        if not path:
            result["tools"][name] = "missing"
            continue
        try:
            proc = subprocess.run(
                [path, "-version" if name == "ffmpeg" else "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            result["tools"][name] = (proc.stdout or proc.stderr).splitlines()[0][:150]
        except (OSError, subprocess.TimeoutExpired, IndexError):
            result["tools"][name] = "version unavailable"
    for name in ("genanki", "Pillow", "elevenlabs"):
        try:
            result["dependencies"][name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result["dependencies"][name] = "missing"
    if not paths.DB_PATH.exists():
        result["database_status"] = "missing; initialize with lexicon"
        return result
    with closing(sqlite3.connect(f"file:{paths.DB_PATH}?mode=ro", uri=True)) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        result["schema"] = version
        result["supported_schema"] = SCHEMA_VERSION
        result["database_status"] = conn.execute("PRAGMA quick_check").fetchone()[0]
        result["foreign_key_violations"] = len(list(conn.execute("PRAGMA foreign_key_check")))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "ai_jobs" in tables:
            result["jobs"] = [
                list(r) for r in conn.execute("SELECT kind,status,count(*) FROM ai_jobs GROUP BY kind,status")
            ]
        if "v4_notes" in tables:
            result["notes"] = [
                list(r)
                for r in conn.execute(
                    "SELECT card_type,ready,count(*) FROM v4_notes GROUP BY card_type,ready"
                )
            ]
            result["blocked"] = [
                list(r)
                for r in conn.execute(
                    "SELECT blocked_by,count(*) FROM v4_notes WHERE ready=0 GROUP BY blocked_by"
                )
            ]
        if "metadata" in tables:
            result["checkpoints"] = dict(conn.execute("SELECT key,value FROM metadata"))
        if "lexemes" in tables:
            result["needs_review"] = conn.execute(
                "SELECT count(*) FROM lexemes WHERE status='needs_review'"
            ).fetchone()[0]
    return result


def run(out=None):
    result = report()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if out:
        atomic_json(out, result)
    return int(
        bool(
            result["configuration_errors"]
            or result.get("foreign_key_violations")
            or result.get("schema", 0) > SCHEMA_VERSION
            or result.get("database_status") != "ok"
        )
    )
