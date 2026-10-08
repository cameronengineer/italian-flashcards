"""CLI entry point: ``flashcards <command>`` (or ``python -m flashcards``).

Pipeline
  discover    Validate sources.json and list the sources (no writes).
  build       Ingest sources, retire removed rows, add fun facts, write cards.
  audio       ElevenLabs audio for cards that don't have it yet.
  images      AI illustrations for cards that don't have one yet.
  compress    Compress media for packaging (PNG→JPEG, MP3→48 kbps mono).
  export      Write one .apkg per deck into decks/.
  sync        Import into Anki, clean up orphans, reorder new cards.
  run         All of the above, in order (backs up the DB first).

Study tools
  practice    Interactive translation practice from your learnt words.
  learnt      Export every word you've graduated to review.
  leech       Suspend + tag cards you keep failing.

Quality
  audit       AI review of the generated cards (verdicts saved to the DB).
  review      Low-confidence entries, failed audits and doubtful facts.

Defaults for workers, limits and models live in settings.toml.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .settings import settings


def cmd_discover(_args) -> int:
    from .sources import load, summarise, validate

    sources, parse_errors = load()
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
    from .commands import build

    result = build.run(workers=args.workers, select=args.source, skip_ai=args.skip_ai,
                       refresh=args.refresh)
    return 1 if result["failed_sources"] else 0


def cmd_audio(args) -> int:
    from .commands import media

    if args.per_deck:
        media.generate_audio_per_deck(args.per_deck, workers=args.workers)
    else:
        media.generate_audio(workers=args.workers, limit=args.limit, decks=args.deck or None)
    return 0


def cmd_images(args) -> int:
    from .commands import media

    media.generate_images(workers=args.workers, limit=args.limit)
    return 0


def cmd_compress(args) -> int:
    from .commands import media

    media.compress(workers=args.workers)
    return 0


def cmd_export(_args) -> int:
    from .commands import export

    export.run()
    return 0


def cmd_sync(args) -> int:
    from .commands import sync

    result = sync.run(
        dry_run=args.dry_run,
        allow_orphan_delete=args.allow_orphan_delete,
        delete_reviewed_orphans=args.delete_reviewed_orphans,
    )
    return 1 if result["import_failed"] else 0


def cmd_run(args) -> int:
    from . import backup
    from .commands import build, export, media, sync

    print(f"Workers — build: {args.build_workers}, audio: {args.audio_workers}, "
          f"images: {args.image_workers}, compress: {args.compress_workers}")
    backup.snapshot("before run")
    result = build.run(workers=args.build_workers, refresh=args.refresh)
    failed = result.get("failed_sources") or []
    media.generate_audio(workers=args.audio_workers, limit=args.audio_limit,
                         decks=args.audio_deck or None)
    media.generate_images(workers=args.image_workers, limit=args.image_limit)
    media.compress(workers=args.compress_workers)
    export.run()
    if args.no_sync:
        print("\nSync skipped (--no-sync).")
        return 0
    if failed:
        print(f"\nSync skipped because the build had failures: {failed}. "
              "Fix and re-run, or run sync manually.")
        return 1
    try:
        sync.run(allow_orphan_delete=args.allow_orphan_delete,
                 delete_reviewed_orphans=args.delete_reviewed_orphans)
    except RuntimeError as exc:
        print(f"\nSync skipped (Anki not running?): {exc}")
    return 0


def cmd_practice(args) -> int:
    from .commands import practice

    if args.list_styles:
        return practice.list_styles()
    return practice.run(words=args.words, sentences=args.sentences, deck=args.deck,
                        seed=args.seed, length=args.length, styles=args.style,
                        no_subjunctive=args.no_subjunctive)


def cmd_learnt(args) -> int:
    from .commands import learnt

    return learnt.run(deck=args.deck, output=args.output)


def cmd_leech(args) -> int:
    from .commands import leech

    return leech.run(deck=args.deck, threshold=args.threshold,
                     update_config=args.update_config, dry_run=args.dry_run)


def cmd_audit(args) -> int:
    from .commands import audit

    return audit.run(decks=args.deck, limit=args.limit, workers=args.workers,
                     only_problems=args.only_problems, out=args.out)


def cmd_review(args) -> int:
    from .commands import review

    return review.run(limit=args.limit)


def _orphan_flags(sp) -> None:
    sp.add_argument("--allow-orphan-delete", action="store_true",
                    help="Allow deleting more orphans than the [sync] limits in settings.toml.")
    sp.add_argument("--delete-reviewed-orphans", action="store_true",
                    help="Also delete orphaned notes that have review history (kept by default).")


def build_parser() -> argparse.ArgumentParser:
    run_cfg = settings.run
    p = argparse.ArgumentParser(prog="flashcards", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")

    sp = sub.add_parser("discover", help="Validate sources.json and list sources.")
    sp.set_defaults(func=cmd_discover)

    sp = sub.add_parser("build", help="Ingest sources and write cards.")
    sp.add_argument("--workers", type=int, default=run_cfg.build_workers)
    sp.add_argument("--source", action="append", help="Only this source id (repeatable).")
    sp.add_argument("--skip-ai", action="store_true",
                    help="No AI calls: skip ingest + facts, only rebuild cards from the DB.")
    sp.add_argument("--refresh", action="append", metavar="SOURCE",
                    help="Re-ask the AI for every row of this source (repeatable).")
    sp.set_defaults(func=cmd_build)

    sp = sub.add_parser("audio", help="Generate ElevenLabs audio.")
    sp.add_argument("--workers", type=int, default=run_cfg.audio_workers)
    sp.add_argument("--limit", type=int, default=None)
    sp.add_argument("--deck", action="append", metavar="DECK", help="Only this deck (repeatable).")
    sp.add_argument("--per-deck", type=int, default=None, metavar="N",
                    help="Generate up to N new files for every deck.")
    sp.set_defaults(func=cmd_audio)

    sp = sub.add_parser("images", help="Generate AI images.")
    sp.add_argument("--workers", type=int, default=run_cfg.image_workers)
    sp.add_argument("--limit", type=int, default=None)
    sp.set_defaults(func=cmd_images)

    sp = sub.add_parser("compress", help="Compress media.")
    sp.add_argument("--workers", type=int, default=run_cfg.compress_workers)
    sp.set_defaults(func=cmd_compress)

    sp = sub.add_parser("export", help="Write .apkg files.")
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("sync", help="Push to Anki via AnkiConnect.")
    sp.add_argument("--dry-run", action="store_true", help="Preview without changing Anki.")
    _orphan_flags(sp)
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("run", help="Full pipeline (defaults from settings.toml [run]).")
    sp.add_argument("--build-workers", type=int, default=run_cfg.build_workers)
    sp.add_argument("--audio-workers", type=int, default=run_cfg.audio_workers)
    sp.add_argument("--image-workers", type=int, default=run_cfg.image_workers)
    sp.add_argument("--compress-workers", type=int, default=run_cfg.compress_workers)
    sp.add_argument("--audio-limit", type=int, default=run_cfg.audio_limit)
    sp.add_argument("--image-limit", type=int, default=run_cfg.image_limit)
    sp.add_argument("--audio-deck", action="append", metavar="DECK",
                    help="Only generate audio for this deck (repeatable).")
    sp.add_argument("--refresh", action="append", metavar="SOURCE",
                    help="Re-ask the AI for every row of this source (repeatable).")
    sp.add_argument("--no-sync", action="store_true", help="Skip the AnkiConnect sync.")
    _orphan_flags(sp)
    sp.set_defaults(func=cmd_run)

    from .commands.practice import LENGTH_GUIDANCE, STYLE_GUIDANCE

    sp = sub.add_parser("practice", help="Interactive translation practice.")
    sp.add_argument("--words", "--count", type=int, default=None,
                    help=f"Word-bank size per sentence (default {settings.practice.words}).")
    sp.add_argument("--sentences", type=int, default=None,
                    help=f"Number of sentences (default {settings.practice.sentences}).")
    sp.add_argument("--deck", help="Only use words from this deck.")
    sp.add_argument("--seed", type=int, default=None, help="Random seed for word selection.")
    sp.add_argument("--length", choices=sorted(LENGTH_GUIDANCE), default=None)
    sp.add_argument("--style", action="append", choices=sorted(STYLE_GUIDANCE), metavar="STYLE",
                    default=[], help="Grammar constraint (repeatable); see --list-styles.")
    sub_group = sp.add_mutually_exclusive_group()
    sub_group.add_argument("--no-subjunctive", dest="no_subjunctive", action="store_true", default=None)
    sub_group.add_argument("--subjunctive", dest="no_subjunctive", action="store_false")
    sp.add_argument("--list-styles", action="store_true")
    sp.set_defaults(func=cmd_practice)

    sp = sub.add_parser("learnt", help="Export learnt words.")
    sp.add_argument("--deck")
    sp.add_argument("--output", type=Path, default=None)
    sp.set_defaults(func=cmd_learnt)

    sp = sub.add_parser("leech", help="Suspend + tag leech cards.")
    sp.add_argument("--deck")
    sp.add_argument("--threshold", type=int, default=None,
                    help=f"Consecutive failures (default {settings.leech.threshold}).")
    sp.add_argument("--update-config", action="store_true",
                    help="Also set each deck's leech threshold in Anki.")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_leech)

    sp = sub.add_parser("audit", help="AI review of generated cards.")
    sp.add_argument("--deck", action="append", metavar="DECK")
    sp.add_argument("--limit", type=int, default=None)
    sp.add_argument("--workers", type=int, default=None)
    sp.add_argument("--only-problems", action="store_true")
    sp.add_argument("--out", type=Path, default=None)
    sp.set_defaults(func=cmd_audit)

    sp = sub.add_parser("review", help="Items that need a human look.")
    sp.add_argument("--limit", type=int, default=20, help="Rows to print (CSV has all).")
    sp.set_defaults(func=cmd_review)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
