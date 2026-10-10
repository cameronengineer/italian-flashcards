"""Database snapshots.

``database.sqlite`` holds the identities behind every Anki GUID, so it is
the one file that can't be regenerated. ``snapshot()`` runs before every
``flashcards run`` and before any schema migration:

* SQLite's online backup API makes a consistent copy even while the DB is
  in WAL mode (a plain file copy can miss pages still in ``-wal``); a
  migration passes its own connection so the copy sees exactly what it
  is about to change;
* the copy is dropped if it is identical to the newest backup;
* only the newest ``backups.keep_last`` snapshots are kept (files named
  ``database.backup.<YYYYMMDD_HHMMSS>_<id>.sqlite``; nothing else in
  ``backups/`` is touched);
* if ``backups.mirror_dir`` is set, each new snapshot is also copied there
  (e.g. a synced folder outside the repo).
"""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from . import paths, report
from .settings import settings
from .util import sha256_file

PATTERN = "database.backup.[0-9]*.sqlite"


def snapshot(reason: str = "", *, connection: sqlite3.Connection | None = None) -> Path | None:
    """Back up the DB; returns the new file, or None if nothing changed."""
    if connection is None and not paths.DB_PATH.exists():
        return None
    paths.BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
    dest = paths.BACKUPS_DIR / f"database.backup.{ts}.sqlite"
    tmp = paths.BACKUPS_DIR / f".tmp.{ts}.sqlite"
    tmp.unlink(missing_ok=True)
    with closing(sqlite3.connect(tmp)) as target:
        if connection is not None:
            if connection.in_transaction:
                raise ValueError("Cannot snapshot an uncommitted transaction")
            connection.backup(target)
        else:
            with closing(sqlite3.connect(f"file:{paths.DB_PATH}?mode=ro", uri=True)) as source:
                source.backup(target)

    existing = sorted(paths.BACKUPS_DIR.glob(PATTERN))
    if existing and sha256_file(existing[-1]) == sha256_file(tmp):
        tmp.unlink()
        report.detail(f"Backup unchanged; keeping {existing[-1].name}")
        return None
    tmp.replace(dest)
    report.detail(f"Backup created: {dest.name}" + (f" ({reason})" if reason else ""))

    if settings.backups.mirror_dir:
        mirror = Path(settings.backups.mirror_dir).expanduser()
        try:
            mirror.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, mirror / dest.name)
            for old in sorted(mirror.glob(PATTERN))[: -settings.backups.keep_last]:
                old.unlink()
        except OSError as exc:
            print(f"  (mirror copy to {mirror} failed: {exc})")

    keep = max(1, settings.backups.keep_last)
    for old in sorted(paths.BACKUPS_DIR.glob(PATTERN))[:-keep]:
        old.unlink()
        report.detail(f"  removed old backup {old.name} (keeping last {keep})")
    return dest


# ── Anki collection ────────────────────────────────────────────────────────

#: Where Anki keeps its profiles (macOS); a profile's collection lives under it.
ANKI_DIR = Path.home() / "Library" / "Application Support" / "Anki2"
ANKI_KEEP = 5


def anki_snapshot(profile: str) -> Path | None:
    """Back up the whole Anki collection — every deck, note, card and review —
    to ``backups/anki/collection.<profile>.<time>.zip``.

    SQLite's online backup reads Anki's database consistently while Anki is
    open, without changing it. Media is not copied (it is never deleted, and
    the media folder is several GB). Restore: quit Anki and put the unzipped
    ``collection.anki2`` back in the profile folder. Keeps the newest five.
    """
    import zipfile

    source = ANKI_DIR / profile / "collection.anki2"
    if not profile or not source.exists():
        return None
    folder = paths.BACKUPS_DIR / "anki"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    copy = folder / f".collection.{stamp}.anki2"
    dest = folder / f"collection.{profile.replace(' ', '_')}.{stamp}.zip"
    try:
        with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)) as live:
            with closing(sqlite3.connect(copy)) as target:
                # Anki's indexes use its own case-insensitive collation.
                target.create_collation(
                    "unicase", lambda a, b: (a.casefold() > b.casefold()) - (a.casefold() < b.casefold())
                )
                live.backup(target)
                if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("Anki backup failed its integrity check")
                target.execute("PRAGMA journal_mode=DELETE")
        tmp = dest.with_suffix(".tmp")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(copy, "collection.anki2")
        tmp.replace(dest)
    finally:
        copy.unlink(missing_ok=True)
    for old in sorted(folder.glob("collection.*.zip"))[:-ANKI_KEEP]:
        old.unlink()
    return dest
