"""Database snapshots.

``database.sqlite`` holds the identities behind every Anki GUID, so it is
the one file that can't be regenerated. ``snapshot()`` runs before every
``flashcards run`` and before any schema migration:

* ``VACUUM INTO`` makes a consistent, compact copy even while the DB is in
  WAL mode (a plain file copy can miss pages still in ``-wal``);
* the copy is dropped if it is identical to the newest backup;
* only the newest ``backups.keep_last`` snapshots are kept (files named
  ``database.backup.<YYYYMMDD_HHMMSS>.sqlite``; nothing else in
  ``backups/`` is touched);
* if ``backups.mirror_dir`` is set, each new snapshot is also copied there
  (e.g. a synced folder outside the repo).
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from . import paths
from .settings import settings

PATTERN = "database.backup.[0-9]*.sqlite"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot(reason: str = "") -> Path | None:
    """Back up the DB; returns the new file, or None if nothing changed."""
    if not paths.DB_PATH.exists():
        return None
    paths.BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = paths.BACKUPS_DIR / f"database.backup.{ts}.sqlite"
    tmp = paths.BACKUPS_DIR / f".tmp.{ts}.sqlite"
    tmp.unlink(missing_ok=True)
    with closing(sqlite3.connect(paths.DB_PATH)) as conn:
        conn.execute("VACUUM INTO ?", (str(tmp),))

    existing = sorted(paths.BACKUPS_DIR.glob(PATTERN))
    if existing and _sha256(existing[-1]) == _sha256(tmp):
        tmp.unlink()
        print(f"Backup unchanged; keeping {existing[-1].name}")
        return None
    tmp.replace(dest)
    print(f"Backup created: {dest.name}" + (f" ({reason})" if reason else ""))

    if settings.backups.mirror_dir:
        mirror = Path(settings.backups.mirror_dir).expanduser()
        try:
            mirror.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, mirror / dest.name)
            for old in sorted(mirror.glob(PATTERN))[:-settings.backups.keep_last]:
                old.unlink()
        except OSError as exc:
            print(f"  (mirror copy to {mirror} failed: {exc})")

    keep = max(1, settings.backups.keep_last)
    for old in sorted(paths.BACKUPS_DIR.glob(PATTERN))[:-keep]:
        old.unlink()
        print(f"  removed old backup {old.name} (keeping last {keep})")
    return dest
