"""CLI entry point: ``python -m flashcards <command>``.

Commands
--------
  discover    Print the result of source discovery (no DB writes).
  build       Discover → ingest → materialise → re-sort cards in DB.
  audio       Generate ElevenLabs audio for new audio_text rows.
  images      Generate AI images for new image_text rows.
  compress    Compress media (PNG→JPEG, MP3→48kbps mono).
  export      Write .apkg files into decks/.
  sync        Import decks via AnkiConnect, delete orphans, reorder new cards.
  run         The full pipeline: build → audio → images → compress → export → sync.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from datetime import datetime

from . import paths
from .commands import build, export, media, sync


def cmd_discover(_args) -> int:
    from .sources import discover, summarise, validate

    sources, parse_errors = discover()
    print(summarise(sources))
    errors = validate(sources, parse_errors)
    if errors:
        print("\nValidation errors:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("\nValidation: OK.")
    return 0


def cmd_build(args) -> int:
    build.run(workers=args.workers, select=args.source, skip_ai=args.skip_ai)
    return 0


def cmd_audio(args) -> int:
    media.generate_audio(workers=args.workers, limit=args.limit, decks=args.deck or None)
    return 0


def cmd_images(args) -> int:
    media.generate_images(workers=args.workers, limit=args.limit)
    return 0


def cmd_compress(args) -> int:
    media.compress(workers=args.workers)
    return 0


def cmd_export(_args) -> int:
    export.run()
    return 0


def cmd_sync(args) -> int:
    sync.run(dry_run=args.dry_run, allow_orphan_delete=args.allow_orphan_delete)
    return 0


def _file_sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _backup_db() -> None:
    """Snapshot ``database.sqlite`` into ``backups/`` then dedup.

    Skips writing entirely when the current DB matches **any** existing
    backup with that content, and removes duplicate backups (keeping the
    oldest copy of each content hash). Without this, a daily-run cadence
    accumulates hundreds of MB of near-identical sqlite files.
    """
    paths.BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    if not paths.DB_PATH.exists():
        return
    current_hash = _file_sha256(paths.DB_PATH)

    # Filename is ``database.backup.YYYYMMDD_HHMMSS.sqlite`` (timestamp is
    # all-digits). Glob restricted to that shape so a manually-renamed file
    # like ``database.backup.manual.sqlite`` can't sort weirdly and silently
    # become the "kept" copy for its content hash.
    existing = sorted(paths.BACKUPS_DIR.glob("database.backup.[0-9]*.sqlite"))
    seen: dict[str, "object"] = {}
    for backup in existing:
        try:
            h = _file_sha256(backup)
        except OSError:
            continue
        if h in seen:
            try:
                backup.unlink()
                print(f"  Removed duplicate backup: {backup.name}")
            except OSError:
                pass
        else:
            seen[h] = backup

    if current_hash in seen:
        print(f"Backup unchanged; keeping {seen[current_hash].name}")
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = paths.BACKUPS_DIR / f"database.backup.{ts}.sqlite"
    shutil.copy2(paths.DB_PATH, dest)
    print(f"Backup created: {dest}")


def _resolve_workers(args) -> tuple[int, int, int, int]:
    """Return (build, audio, image, compress) worker counts.

    Phase-specific flags take precedence; ``--workers`` is the shared
    fallback; hard-coded phase defaults apply last.  Audio defaults to 5
    to stay within ElevenLabs' concurrent-request limit.
    """
    global_w = args.workers
    build_w    = args.build_workers    or global_w or 20
    audio_w    = args.audio_workers    or global_w or 5
    image_w    = args.image_workers    or global_w or 10
    compress_w = args.compress_workers or global_w or 8
    return build_w, audio_w, image_w, compress_w


def cmd_run(args) -> int:
    build_w, audio_w, image_w, compress_w = _resolve_workers(args)
    print(
        f"Workers — build: {build_w}, audio: {audio_w}, "
        f"images: {image_w}, compress: {compress_w}"
    )
    _backup_db()
    result = build.run(workers=build_w)
    failed = result.get("failed_sources") or []
    audio_limit = args.audio_limit if args.audio_limit is not None else args.limit
    image_limit = args.image_limit if args.image_limit is not None else args.limit
    audio_decks = args.audio_deck or None
    media.generate_audio(workers=audio_w, limit=audio_limit, decks=audio_decks)
    media.generate_images(workers=image_w, limit=image_limit)
    media.compress(workers=compress_w)
    export.run()
    if args.no_sync:
        print("\nSync skipped (--no-sync).")
        return 0
    if failed:
        print(
            "\nSync skipped because the build had failures: "
            f"{failed}. Fix and re-run, or run sync manually."
        )
        return 1
    try:
        sync.run(allow_orphan_delete=args.allow_orphan_delete)
    except RuntimeError as exc:
        print(f"\nSync skipped (Anki not running?): {exc}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="flashcards", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("discover", help="Print discovered sources.")
    sp.set_defaults(func=cmd_discover)

    sp = sub.add_parser("build", help="Ingest sources and materialise cards.")
    sp.add_argument("--workers", type=int, default=10)
    sp.add_argument("--source", action="append", help="Only this source id (repeatable).")
    sp.add_argument("--skip-ai", action="store_true",
                    help="Skip the ingest pass; only re-materialise existing entries.")
    sp.set_defaults(func=cmd_build)

    sp = sub.add_parser("audio", help="Generate ElevenLabs audio.")
    sp.add_argument("--workers", type=int, default=10)
    sp.add_argument("--limit", type=int, default=None)
    sp.add_argument("--deck", action="append", metavar="DECK",
                    help="Only generate audio for this deck (repeatable, e.g. --deck 'Italian - CILS A1').")
    sp.set_defaults(func=cmd_audio)

    sp = sub.add_parser("images", help="Generate AI images.")
    sp.add_argument("--workers", type=int, default=10)
    sp.add_argument("--limit", type=int, default=None)
    sp.set_defaults(func=cmd_images)

    sp = sub.add_parser("compress", help="Compress media.")
    sp.add_argument("--workers", type=int, default=8)
    sp.set_defaults(func=cmd_compress)

    sp = sub.add_parser("export", help="Write .apkg files.")
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("sync", help="Push to Anki via AnkiConnect.")
    sp.add_argument("--dry-run", action="store_true",
                    help="Preview every action without changing Anki.")
    sp.add_argument("--allow-orphan-delete", action="store_true",
                    help="Allow deletion of more than 200 notes (or >10%% of a deck). "
                         "Required when the local DB is dramatically smaller than Anki.")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("run", help="Full pipeline.")
    sp.add_argument("--workers", type=int, default=None,
                    help="Default concurrency for all phases (overridden by phase-specific flags).")
    sp.add_argument("--build-workers", type=int, default=None,
                    help="Concurrency for the build/AI-ingest phase (default: --workers or 20).")
    sp.add_argument("--audio-workers", type=int, default=None,
                    help="Concurrency for ElevenLabs audio generation (default: --workers or 5).")
    sp.add_argument("--image-workers", type=int, default=None,
                    help="Concurrency for AI image generation (default: --workers or 10).")
    sp.add_argument("--compress-workers", type=int, default=None,
                    help="Concurrency for media compression (default: --workers or 8).")
    sp.add_argument("--limit", type=int, default=None,
                    help="Shared fallback limit for audio + images "
                         "(overridden by --audio-limit / --image-limit).")
    sp.add_argument("--audio-limit", type=int, default=None,
                    help="Max audio files to generate this run (default: --limit or unlimited).")
    sp.add_argument("--audio-deck", action="append", metavar="DECK",
                    help="Only generate audio for this deck (repeatable). "
                         "e.g. --audio-deck 'Italian - CILS A1' --audio-deck 'Italian - CILS A2'")
    sp.add_argument("--image-limit", type=int, default=None,
                    help="Max images to generate this run (default: --limit or unlimited).")
    sp.add_argument("--no-sync", action="store_true",
                    help="Skip AnkiConnect sync (useful when Anki isn't running).")
    sp.add_argument("--allow-orphan-delete", action="store_true",
                    help="Pass through to `sync` step (see `sync --help`).")
    sp.set_defaults(func=cmd_run)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
