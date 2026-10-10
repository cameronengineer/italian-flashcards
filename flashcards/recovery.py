"""Versioned, verified recovery bundles. SQLite and media are authoritative.

Recovery creates a NEW workspace. It never replaces an existing database or
deletes any media. Use --include-media for a standalone, portable bundle.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from importlib.metadata import distributions
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from . import paths
from .util import sha256_file
from .runtime import atomic_json, atomic_text


def create(destination: Path, *, include_media: bool = False, anki_collection: Path | None = None) -> Path:
    destination = destination.resolve()
    if any(
        destination.is_relative_to(p.resolve())
        for p in (
            paths.INPUTS_DIR,
            paths.LEXICON_DIR,
            paths.MEDIA_DIR,
            paths.PROJECT_ROOT / "flashcards",
            paths.PROJECT_ROOT / "scripts",
        )
    ):
        raise ValueError(
            "Recovery destination must be outside inputs, lexicon, media, code and scripts to avoid copying itself"
        )
    if destination.exists():
        raise ValueError(f"Recovery destination already exists: {destination}")
    dictionary = paths.PROJECT_ROOT / "data" / "kaikki.sqlite"
    sources = [paths.DB_PATH, dictionary, paths.INPUTS_DIR, paths.LEXICON_DIR]
    if include_media:
        sources.append(paths.MEDIA_DIR)
    if anki_collection:
        if not anki_collection.is_file():
            raise ValueError("Anki recovery requires an existing collection.anki2 SQLite file")
        sources.append(anki_collection)
        if include_media:
            sources.append(anki_collection.parent / "collection.media")
    size = sum(
        p.stat().st_size
        for source in sources
        for p in ([source] if source.is_file() else source.rglob("*"))
        if p.is_file() and not p.is_symlink()
    )
    parent = destination.parent
    while not parent.exists():
        parent = parent.parent
    if shutil.disk_usage(parent).free < size * 1.1 + 16 * 1024 * 1024:
        raise ValueError("Insufficient free space for a verified recovery bundle")
    destination.mkdir(parents=True)
    with closing(sqlite3.connect(f"file:{paths.DB_PATH}?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(destination / "database.sqlite")) as target:
            source.backup(target)
            if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Database failed integrity validation")
            schema = target.execute("PRAGMA user_version").fetchone()[0]
    files = {"database.sqlite": sha256_file(destination / "database.sqlite")}
    for name in (
        "settings.toml",
        "lists.toml",
        "plan.toml",
        "pyproject.toml",
        "run.sh",
        "README.md",
        "review.csv",
        "cue_review.csv",
    ):
        src = paths.PROJECT_ROOT / name
        if src.exists():
            shutil.copy2(src, destination / name)
            files[name] = sha256_file(destination / name)
    # Includes private inputs: bundles are deliberately private, never git exports.
    for directory in (
        paths.INPUTS_DIR,
        paths.LEXICON_DIR,
        paths.PROJECT_ROOT / "flashcards",
        paths.PROJECT_ROOT / "scripts",
    ):
        if directory.exists():
            for src in sorted(directory.rglob("*")):
                if (
                    not src.is_file()
                    or src.is_symlink()
                    or "__pycache__" in src.parts
                    or src.suffix == ".pyc"
                ):
                    continue
                relative = str(src.relative_to(paths.PROJECT_ROOT))
                dest = destination / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                files[relative] = sha256_file(dest)

    def snapshot(source, relative, *, anki=False):
        dest = destination / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as live:
            with closing(sqlite3.connect(dest)) as target:
                if anki:
                    target.create_collation(
                        "unicase", lambda a, b: (a.casefold() > b.casefold()) - (a.casefold() < b.casefold())
                    )
                live.backup(target)
                if anki and not {"cards", "revlog"} <= {
                    r[0] for r in target.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }:
                    raise ValueError("Anki collection must contain cards and review history tables")
                if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError(f"Invalid recovery dependency: {relative}")
        files[relative] = sha256_file(dest)

    if dictionary.exists():
        snapshot(dictionary.resolve(), "data/kaikki.sqlite")
    if anki_collection:
        snapshot(anki_collection.resolve(), "anki/collection.anki2", anki=True)
        if include_media:
            for src in sorted((anki_collection.parent / "collection.media").glob("*")):
                if src.is_file() and not src.is_symlink():
                    relative = "anki/collection.media/" + src.name
                    dest = destination / relative
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)
                    files[relative] = sha256_file(dest)
    runtime = {
        "python": sys.version,
        "packages": sorted(
            {f"{d.metadata['Name']}=={d.version}" for d in distributions() if d.metadata["Name"]}
        ),
    }
    atomic_text(
        destination / "requirements-recovery.txt",
        "\n".join(p for p in runtime["packages"] if not p.lower().startswith("flashcards==")) + "\n",
    )
    files["requirements-recovery.txt"] = sha256_file(destination / "requirements-recovery.txt")
    atomic_json(destination / "runtime.json", runtime)
    files["runtime.json"] = sha256_file(destination / "runtime.json")
    assets = {}
    for src in sorted(paths.MEDIA_DIR.rglob("*")):
        if not src.is_file() or src.is_symlink() or src.name.startswith(".") or ".tmp" in src.name:
            continue
        relative = str(src.relative_to(paths.PROJECT_ROOT))
        assets[relative] = {"sha256": sha256_file(src), "bytes": src.stat().st_size}
        if include_media:
            dest = destination / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
    atomic_json(
        destination / "manifest.json",
        {
            "format": 1,
            "recovery_level": "study-system"
            if anki_collection and include_media and dictionary.exists()
            else "local-pipeline"
            if dictionary.exists() and include_media
            else "partial",
            "dictionary_included": dictionary.exists(),
            "anki_history_included": bool(anki_collection),
            "anki_media_included": bool(anki_collection and include_media),
            "runtime": "Installed versions recorded in runtime.json; Python/tool executables and login credentials are external dependencies",
            "complete": False,
            "schema": schema,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "media_included": include_media,
            "media_root": str(paths.PROJECT_ROOT),
            "files": files,
            "assets": assets,
        },
    )
    manifest = verify(destination, _allow_incomplete=True)
    manifest["complete"] = True
    atomic_json(destination / "manifest.json", manifest)
    return destination


def _safe(root: Path, relative: str) -> Path:
    p = (root / relative).resolve()
    if not p.is_relative_to(root.resolve()):
        raise ValueError(f"Unsafe recovery path: {relative}")
    return p


def verify(bundle: Path, *, media_root: Path | None = None, _allow_incomplete=False) -> dict:
    manifest = json.loads((bundle / "manifest.json").read_text())
    if manifest.get("complete") is False and not _allow_incomplete:
        raise ValueError("Recovery bundle was not completely verified")
    if manifest.get("format") != 1:
        raise ValueError("Unsupported recovery format")
    source = bundle if manifest["media_included"] else media_root or Path(manifest["media_root"])
    for rel, digest in manifest["files"].items():
        p = _safe(bundle, rel)
        if not p.is_file() or sha256_file(p) != digest:
            raise ValueError(f"Missing or changed recovery file: {rel}")
    for rel, spec in manifest["assets"].items():
        p = _safe(source, rel)
        if not p.is_file() or p.stat().st_size != spec["bytes"] or sha256_file(p) != spec["sha256"]:
            raise ValueError(f"Missing or changed media: {rel}")
    with closing(sqlite3.connect(f"file:{bundle / 'database.sqlite'}?mode=ro", uri=True)) as conn:
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok" or list(
            conn.execute("PRAGMA foreign_key_check")
        ):
            raise ValueError("Recovery database integrity check failed")
    return manifest


def restore(bundle: Path, destination: Path, *, media_root: Path | None = None) -> Path:
    if destination.exists():
        raise ValueError("Restore requires a new workspace; existing files are never overwritten")
    manifest = verify(bundle, media_root=media_root)
    destination.mkdir(parents=True)
    source = bundle if manifest["media_included"] else media_root or Path(manifest["media_root"])
    for rel in manifest["files"]:
        dest = _safe(destination, rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_safe(bundle, rel), dest)
    for rel in manifest["assets"]:
        dest = _safe(destination, rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_safe(source, rel), dest)
    for rel, digest in manifest["files"].items():
        if sha256_file(_safe(destination, rel)) != digest:
            raise ValueError(f"Restored file verification failed: {rel}")
    for rel, spec in manifest["assets"].items():
        if sha256_file(_safe(destination, rel)) != spec["sha256"]:
            raise ValueError(f"Restored media verification failed: {rel}")
    atomic_json(
        destination / "restore_verified.json",
        {
            "bundle": str(bundle.resolve()),
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "schema": manifest["schema"],
        },
    )
    return destination
