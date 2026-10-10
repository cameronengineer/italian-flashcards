"""Resumable, non-destructive inventory of every media file, including unused originals."""

import json
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from . import assets, paths
from .util import md5_hex, sha256_file


def references(conn):
    refs = defaultdict(set)
    for row in conn.execute("SELECT key,fields_json FROM v4_notes"):
        for img, snd in re.findall(
            r'<img src="([^"]+)"|\[sound:([^\]]+)\]', row["fields_json"].replace('\\"', '"')
        ):
            refs[img or snd].add(row["key"])
    for table, field, identity in [("lexemes", "image_key", "id"), ("cards", "image_text", "natural_key")]:
        for row in conn.execute(f"SELECT {identity},{field} FROM {table} WHERE {field} IS NOT NULL"):
            for suffix in (".png", ".jpg"):
                name = md5_hex(row[1].strip()) + suffix
                if any((d / name).is_file() for d in (paths.IMAGE_DIR, paths.IMAGE_DIR_COMPRESSED)):
                    refs[name].add(f"{table}:{row[0]}")
    return refs


def scan(conn, *, out=None, limit=None, rehash=False):
    """Checksums are reused only for unchanged size+mtime; --rehash verifies bytes again.

    Each file commits independently. Interruptions leave a resumable inventory.
    No media file is created, rewritten or removed.
    """
    out = Path(out or paths.PROJECT_ROOT / "audit_reports" / "media_inventory.sqlite").resolve()
    root = paths.MEDIA_DIR.resolve()
    if out.is_relative_to(root):
        raise ValueError("Inventory must be stored outside media")
    out.parent.mkdir(parents=True, exist_ok=True)
    refs = references(conn)
    provenance = {r["filename"]: dict(r) for r in conn.execute("SELECT * FROM asset_manifest")}
    stamp = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(out)) as inventory:
        inventory.row_factory = sqlite3.Row
        inventory.execute(
            "CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY,bytes INTEGER,mtime_ns INTEGER,sha256 TEXT,status TEXT,error TEXT,expected_checksum TEXT,provenance TEXT,refs TEXT,checked_at TEXT)"
        )
        known = {r["path"]: dict(r) for r in inventory.execute("SELECT * FROM files")}
        current = set()
        checked = reused = 0
        complete = True
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            relative = str(path.relative_to(root))
            current.add(relative)
            if path.is_symlink():
                # Do not follow references outside the inventory root.
                status, error, digest, size, modified = "symlink", "Not followed", None, 0, 0
            else:
                st = path.stat()
                size, modified = st.st_size, st.st_mtime_ns
                previous = known.get(relative)
                metadata_file = path.name.startswith(".") or ".tmp" in path.name
                if previous and metadata_file:
                    inventory.execute("UPDATE files SET status='metadata',error='' WHERE path=?", (relative,))
                if (
                    previous
                    and previous["bytes"] == size
                    and previous["mtime_ns"] == modified
                    and previous["sha256"]
                    and not rehash
                ):
                    reused += 1
                    continue
                if limit is not None and checked >= limit:
                    complete = False
                    continue
                try:
                    digest = sha256_file(path)
                    after = path.stat()
                    if (after.st_size, after.st_mtime_ns) != (size, modified):
                        raise OSError("File changed during verification; retry")
                    status, error = ("ok", "") if size else ("invalid", "Empty file")
                    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"} and not assets.valid_image(
                        path
                    ):
                        status, error = "invalid", "Image decode/validation failed"
                    if metadata_file:
                        status, error = "metadata", ""
                    expected = provenance.get(path.name, {}).get("checksum")
                    expected = expected or (previous["sha256"] if previous else None)
                    if expected and digest != expected:
                        status, error = "changed", "Checksum differs from recorded generation"
                except OSError as exc:
                    status, error, digest = "unreadable", str(exc), None
            source = provenance.get(path.name, {})
            inventory.execute(
                "INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    relative,
                    size,
                    modified,
                    digest,
                    status,
                    error,
                    source.get("checksum"),
                    source.get("metadata") or "unknown (preserved existing asset)",
                    json.dumps(sorted(refs.get(path.name, ()))),
                    stamp,
                ),
            )
            inventory.commit()
            checked += 1
        # Refresh references even when content checksums were reusable.
        for relative in current & known.keys():
            inventory.execute(
                "UPDATE files SET refs=? WHERE path=?",
                (json.dumps(sorted(refs.get(Path(relative).name, ()))), relative),
            )
        if complete:
            for missing in known.keys() - current:
                inventory.execute(
                    "UPDATE files SET status='missing',error='File missing from current scan' WHERE path=?",
                    (missing,),
                )
        inventory.commit()
        statuses = dict(Counter(r[0] for r in inventory.execute("SELECT status FROM files")))
        found_names = {Path(p).name for p in current}
        return {
            "inventory": str(out),
            "complete": complete,
            "checked": checked,
            "reused": reused,
            "files": len(current),
            "status": statuses,
            "missing_references": sorted(refs.keys() - found_names),
            "unreferenced_preserved": sum(Path(p).name not in refs for p in current),
            "validation": "All file bytes hashed; images decoded; audio checksums verified, not full audio decoding",
        }


def run(*, out=None, limit=None, rehash=False):
    with closing(sqlite3.connect(paths.DB_PATH.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        result = scan(conn, out=out, limit=limit, rehash=rehash)
    print(json.dumps(result, indent=2))
    return int(any(result["status"].get(s) for s in ("missing", "changed", "invalid", "unreadable")))
