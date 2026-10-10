"""Italian flashcards — everything goes through ./run.sh.

  ./run.sh            Do everything: lists → cards → Anki → Claude → audio → Anki.
                      Safe to run any time; it only does what's left.
                      Options: --images N  --audio N  --batches N  --once
                               --no-ai  --no-sync  --keep-old

Extras (./run.sh <name> …):
  movie       Coverage report + word list for a film (datasets/<film>.csv).
  practice    Translation practice from words you've learnt; mistakes become cards.
  learnt      Export every word you've graduated.
  leech       Suspend + tag cards you keep failing (--doctor: memory aids).
  audit       Have Claude double-check finished cards.
  review      List everything that needs a look (approve words in review.csv).
  share       Export cards as an .apkg for someone else.
  doctor      Health report.  data-review: data-quality census.
  recovery    Create / verify / restore a recovery bundle.
  kaikki      Re-download the Wiktionary dictionary.

The single steps run.sh performs are also available (check, lexicon, jobs,
work, notes, plan, apply, audio, retire); you shouldn't need them.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from .settings import settings


def _conn():
    from .db import connect, init_schema

    conn = connect()
    init_schema(conn)
    return conn


def cmd_check(_args) -> int:
    from . import lists

    defs = lists.load()
    errors = lists.validate(defs)
    for l in defs:
        print(f"  [{l.kind:<7}] {l.id:<18} → {l.deck}")
    if errors:
        print("\nErrors:\n  - " + "\n  - ".join(errors))
        return 1
    print("\nlists.toml + plan.toml: OK")
    return 0


def cmd_kaikki(args) -> int:
    from . import kaikki

    if args.action == "import":
        print(kaikki.import_stream())
    else:
        print("kaikki index:", "present" if kaikki.available() else "missing", f"({kaikki.KAIKKI_DB})")
    return 0


def cmd_lexicon(_args) -> int:
    from . import lexicon

    with closing(_conn()) as conn:
        edits = lexicon.import_human_edits(conn)
        if edits:
            print(f"  applied {edits} human edit(s) from lexicon/lexemes.jsonl")
        lexicon.sync(conn)
        print(f"  exported {lexicon.export_jsonl(conn)}")
    return 0


def cmd_jobs(args) -> int:
    from . import queue
    from .util import table

    if args.action == "status":
        from .paths import DB_PATH

        # Monitoring must work while the worker holds its single-writer lock,
        # and must never migrate, re-plan, or reopen completed jobs.
        with closing(sqlite3.connect(DB_PATH.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            rows = queue.status(conn)
            print(table(["Kind", "Status", "Jobs"], [list(r) for r in rows]) if rows else "  queue empty")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version < 5:
                return 0
            for job in conn.execute(
                "SELECT kind,count(*) n,min(started_at) started,owner FROM ai_jobs "
                "WHERE status='running' GROUP BY kind,owner"
            ):
                print(
                    f"  In flight: {job['n']} {job['kind']} · started {job['started']} UTC · owner {job['owner']}"
                )
            last = conn.execute("SELECT max(updated_at) FROM ai_jobs WHERE status='done'").fetchone()[0]
            print(f"  Last completed job: {last or 'none'} UTC")
            for row in conn.execute(
                "SELECT kind,count(*) n,min(next_retry_at) retry FROM ai_jobs "
                "WHERE status='pending' AND next_retry_at>datetime('now') GROUP BY kind"
            ):
                print(f"  Retry cooling down: {row['n']} {row['kind']} · earliest {row['retry']} UTC")
            for row in conn.execute(
                "SELECT kind,error,count(*) n FROM ai_jobs WHERE status IN ('pending','failed') "
                "AND error IS NOT NULL GROUP BY kind,error ORDER BY n DESC LIMIT 5"
            ):
                print(f"  {row['n']} {row['kind']}: {row['error']}")
            if settings.run.image_limit == 0:
                print("  Default run skips images (image_limit=0); image jobs remain pending.")
            if version < 6:
                print("  Next worker restart will repair known queue failures and add the planning index.")
        return 0

    with closing(_conn()) as conn:
        if args.action == "retry":
            print(f"  {queue.retry_failed(conn)} failed job(s) back in the queue")
        elif args.action == "refresh":
            if not args.list:
                print("  usage: flashcards jobs refresh LIST")
                return 1
            print(f"  {queue.refresh(conn, args.list)} root(s) of {args.list} will be re-asked")
            print(f"  queued: {queue.plan(conn)}")
    return 0


def cmd_work(args) -> int:
    from . import queue

    with closing(_conn()) as conn:
        queue.plan(conn)
        kinds = args.kind or queue.ORDER
        result = queue.drain(conn, kinds=kinds, max_batches=args.batches, image_limit=args.images)
        print(result)
        from . import notes, lexicon

        notes.build(conn)
        lexicon.export_jsonl(conn)
        return 1 if any(r["failed"] for r in result.values()) else 0


def cmd_notes(_args) -> int:
    from . import notes

    with closing(_conn()) as conn:
        notes.build(conn)
    return 0


def cmd_plan(args) -> int:
    from . import reconcile

    with closing(_conn()) as conn:
        from .notes import build

        build(conn)
        plan = reconcile.run(conn, dry_run=True)
        if plan.conflicts:
            return 2
    return 0


def cmd_apply(args) -> int:
    from . import reconcile

    with closing(_conn()) as conn:
        from . import notes, lexicon

        notes.build(conn)
        plan = reconcile.run(conn, dry_run=False, allow_retire=not getattr(args, "keep_old", False))
        lexicon.export_jsonl(conn)
    return 2 if plan.rejected else 0


def cmd_audio(args) -> int:
    from .commands import media

    if args.per_deck:
        result = media.generate_audio_per_deck(
            args.per_deck, workers=args.workers, decks=args.deck, limit=args.limit
        )
    else:
        result = media.generate_audio(workers=args.workers, limit=args.limit, decks=args.deck or None)
    compression = media.compress(workers=settings.run.compress_workers)
    return 1 if result["failed"] or any(v["failed"] for v in compression.values()) else 0


def cmd_run(args) -> int:
    from . import pipeline

    return pipeline.run(args)


def cmd_movie(args) -> int:
    from .commands import movie

    return movie.run(args.list)


def cmd_practice(args) -> int:
    from .commands import practice

    if args.list_styles:
        return practice.list_styles()
    return practice.run(
        words=args.words,
        sentences=args.sentences,
        deck=args.deck,
        seed=args.seed,
        length=args.length,
        styles=args.style,
        no_subjunctive=args.no_subjunctive,
    )


def cmd_learnt(args) -> int:
    from .commands import learnt

    return learnt.run(deck=args.deck, output=args.output)


def cmd_leech(args) -> int:
    from .commands import leech

    if args.doctor:
        return leech.doctor(unsuspend=args.unsuspend, deck=args.deck, dry_run=args.dry_run)
    return leech.run(
        deck=args.deck, threshold=args.threshold, update_config=args.update_config, dry_run=args.dry_run
    )


def cmd_audit(args) -> int:
    from .commands import audit

    return audit.run(
        decks=args.deck,
        limit=args.limit,
        workers=args.workers,
        only_problems=args.only_problems,
        out=args.out,
    )


def cmd_review(args) -> int:
    from .commands import review

    if args.action == "approve":
        return review.approve(lemmas=args.lemmas, all_=args.all, reason=args.reason)
    return review.run(limit=args.limit)


def cmd_share(args) -> int:
    from .commands import share

    return share.run(decks=args.deck, out=args.out, include_private=args.include_private)


def cmd_doctor(args):
    from .commands import doctor

    return doctor.run(args.out)


def cmd_data_review(args):
    from .commands import data_review

    return data_review.run(database=args.database, out=args.out, check_media=not args.skip_media)


def cmd_recovery(args):
    from . import recovery

    if args.action == "create":
        print(
            recovery.create(args.path, include_media=args.include_media, anki_collection=args.anki_collection)
        )
    elif args.action == "verify":
        result = recovery.verify(args.path, media_root=args.media_root)
        print(f"Verified {len(result['assets'])} media assets and {len(result['files'])} files")
    else:
        if not args.destination:
            raise ValueError("restore needs --destination pointing to a NEW workspace")
        print(recovery.restore(args.path, args.destination, media_root=args.media_root))
    return 0


def cmd_retire(args):
    with closing(_conn()) as conn:
        if not args.reason.strip():
            raise ValueError("A retirement reason is required")
        conn.execute("INSERT OR REPLACE INTO retirements(key,reason) VALUES(?,?)", (args.key, args.reason))
        conn.commit()
    print("Retirement reason recorded. Inspect plan; studied notes remain protected.")
    return 0


def _count(text: str) -> int | None:
    """An asset count: 0 = none, n = at most n, negative = no limit (None)."""
    from .settings import asset_limit

    return asset_limit(int(text))


def _image_flags(sp) -> None:
    sp.add_argument(
        "--images",
        type=_count,
        default=settings.run.image_limit,
        metavar="N",
        help="New Codex images (default: [run] image_limit; 0 = skip Codex, -1 = no limit).",
    )
    sp.add_argument("--no-images", dest="images", action="store_const", const=0, help="Same as --images 0.")


def build_parser() -> argparse.ArgumentParser:
    run_cfg = settings.run
    p = argparse.ArgumentParser(
        prog="flashcards", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")

    sp = sub.add_parser("doctor", help="Read-only workspace health and tool versions.")
    sp.add_argument("--out", type=Path)
    sp.set_defaults(func=cmd_doctor)
    sp = sub.add_parser("data-review", help="Offline data-quality census; no AI, Anki writes, or migrations.")
    sp.add_argument("--database", type=Path, help="Review a SQLite snapshot instead of the live database.")
    sp.add_argument("--out", type=Path)
    sp.add_argument(
        "--skip-media", action="store_true", help="Skip read-only checks of referenced local media."
    )
    sp.set_defaults(func=cmd_data_review)
    sp = sub.add_parser("media-inventory", help="Resumable checksum inventory; never changes media.")
    sp.add_argument("--out", type=Path)
    sp.add_argument("--limit", type=int)
    sp.add_argument("--rehash", action="store_true")
    sp.set_defaults(
        func=lambda args: __import__("flashcards.media_inventory", fromlist=["run"]).run(
            out=args.out, limit=args.limit, rehash=args.rehash
        )
    )
    sp = sub.add_parser("recovery", help="Create, verify, or restore an authoritative recovery bundle.")
    sp.add_argument("action", choices=["create", "verify", "restore"])
    sp.add_argument("path", type=Path)
    sp.add_argument("--include-media", action="store_true")
    sp.add_argument(
        "--anki-collection",
        type=Path,
        help="Include this collection.anki2 and its study history; --include-media also copies collection.media.",
    )
    sp.add_argument("--media-root", type=Path)
    sp.add_argument("--destination", type=Path)
    sp.set_defaults(func=cmd_recovery)
    sp = sub.add_parser("retire", help="Record an explicit reason for retiring an unstudied note.")
    sp.add_argument("key")
    sp.add_argument("--reason", required=True)
    sp.set_defaults(func=cmd_retire)

    sub.add_parser("check", help="Validate lists.toml + plan.toml.").set_defaults(func=cmd_check)

    sp = sub.add_parser("kaikki", help="Import / inspect the Kaikki dictionary.")
    sp.add_argument("action", nargs="?", choices=["import", "status"], default="status")
    sp.set_defaults(func=cmd_kaikki)

    sub.add_parser("lexicon", help="Resolve lists to root words; export JSONL.").set_defaults(
        func=cmd_lexicon
    )

    sp = sub.add_parser("jobs", help="Show / retry / refresh the AI queue.")
    sp.add_argument("action", nargs="?", choices=["status", "retry", "refresh"], default="status")
    sp.add_argument("list", nargs="?", help="list id for `jobs refresh`")
    sp.set_defaults(func=cmd_jobs)

    sp = sub.add_parser("work", help="Work through the AI / image queue.")
    sp.add_argument(
        "--kind",
        action="append",
        choices=["lexeme_enrich", "phrase_enrich", "verb_prompts", "disambiguate", "image"],
    )
    sp.add_argument(
        "--batches", type=int, default=None, help="Stop after N batches (default: until the queue is done)."
    )
    _image_flags(sp)
    sp.set_defaults(func=cmd_work)

    sub.add_parser("notes", help="Compute the desired Anki notes.").set_defaults(func=cmd_notes)
    sub.add_parser("plan", help="Dry run of `apply`.").set_defaults(func=cmd_plan)
    sp = sub.add_parser("apply", help="Sync Anki.")
    sp.add_argument(
        "--keep-old", action="store_true", help="Keep unstudied old cards that have a replacement."
    )
    sp.add_argument("--allow-retire", action="store_true", help=argparse.SUPPRESS)  # now the default
    sp.set_defaults(func=cmd_apply)

    sp = sub.add_parser("audio", help="ElevenLabs audio in study order.")
    sp.add_argument("--workers", type=int, default=run_cfg.audio_workers)
    sp.add_argument(
        "--limit",
        type=_count,
        default=run_cfg.audio_limit,
        metavar="N",
        help="New audio files (default: [run] audio_limit; -1 = no limit).",
    )
    sp.add_argument("--deck", action="append", metavar="DECK")
    sp.add_argument("--per-deck", type=int, default=None, metavar="N")
    sp.set_defaults(func=cmd_audio)

    sp = sub.add_parser("run", help="Full pipeline.")
    sp.add_argument("--audio-workers", type=int, default=run_cfg.audio_workers)
    sp.add_argument(
        "--audio",
        "--audio-limit",
        dest="audio",
        type=_count,
        default=run_cfg.audio_limit,
        metavar="N",
        help="New ElevenLabs audio files this run (default: [run] audio_limit; -1 = no limit).",
    )
    sp.add_argument("--no-audio", dest="audio", action="store_const", const=0, help="Same as --audio 0.")
    sp.add_argument("--no-ai", action="store_true", help="Skip the AI / image queue this run.")
    _image_flags(sp)
    sp.add_argument(
        "--batches",
        type=int,
        default=run_cfg.max_batches,
        help="AI batches between Anki syncs (default: settings.toml [run] max_batches).",
    )
    sp.add_argument("--once", action="store_true", help="Stop after one round of AI batches.")
    sp.add_argument("--no-sync", action="store_true", help="Don't touch Anki.")
    sp.add_argument(
        "--keep-old",
        action="store_true",
        help="Keep unstudied old cards even when their replacement is in Anki.",
    )
    sp.add_argument("--allow-retire", action="store_true", help=argparse.SUPPRESS)  # now the default
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("movie", help="Coverage report + dataset for a film list.")
    sp.add_argument("list", nargs="?")
    sp.set_defaults(func=cmd_movie)

    from .commands.practice import LENGTH_GUIDANCE, STYLE_GUIDANCE

    sp = sub.add_parser("practice", help="Interactive translation practice.")
    sp.add_argument("--words", "--count", type=int, default=None)
    sp.add_argument("--sentences", type=int, default=None)
    sp.add_argument("--deck")
    sp.add_argument("--seed", type=int, default=None)
    sp.add_argument("--length", choices=sorted(LENGTH_GUIDANCE), default=None)
    sp.add_argument("--style", action="append", choices=sorted(STYLE_GUIDANCE), metavar="STYLE", default=[])
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--no-subjunctive", dest="no_subjunctive", action="store_true", default=None)
    g.add_argument("--subjunctive", dest="no_subjunctive", action="store_false")
    sp.add_argument("--list-styles", action="store_true")
    sp.set_defaults(func=cmd_practice)

    sp = sub.add_parser("learnt", help="Export learnt words.")
    sp.add_argument("--deck")
    sp.add_argument("--output", type=Path, default=None)
    sp.set_defaults(func=cmd_learnt)

    sp = sub.add_parser("leech", help="Suspend + tag leeches; --doctor writes memory aids.")
    sp.add_argument("--deck")
    sp.add_argument("--threshold", type=int, default=None)
    sp.add_argument("--update-config", action="store_true")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--doctor", action="store_true")
    sp.add_argument("--unsuspend", action="store_true", help="With --doctor: unsuspend helped cards.")
    sp.set_defaults(func=cmd_leech)

    sp = sub.add_parser("audit", help="AI review of notes.")
    sp.add_argument("--deck", action="append", metavar="DECK")
    sp.add_argument("--limit", type=int, default=None)
    sp.add_argument("--workers", type=int, default=None)
    sp.add_argument("--only-problems", action="store_true")
    sp.add_argument("--out", type=Path, default=None)
    sp.set_defaults(func=cmd_audit)

    sp = sub.add_parser("review", help="Items needing a human look (review approve …).")
    sp.add_argument("action", nargs="?", choices=["list", "approve"], default="list")
    sp.add_argument("lemmas", nargs="*")
    sp.add_argument("--all", action="store_true")
    sp.add_argument("--reason", default="Reviewed by user")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_review)

    sp = sub.add_parser("share", help="Export ready notes as .apkg.")
    sp.add_argument("--deck", action="append", metavar="DECK")
    sp.add_argument("--out", type=Path, default=None)
    sp.add_argument(
        "--include-private", action="store_true", help="Explicitly include private sources and practice data."
    )
    sp.set_defaults(func=cmd_share)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from .runtime import writer_lock
    from .pool import STOP

    STOP.clear()
    try:
        for name in ("workers", "audio_workers", "per_deck", "words", "sentences", "threshold"):
            value = getattr(args, name, None)
            if value is not None and value <= 0:
                raise ValueError(f"--{name.replace('_', '-')} must be positive")
        if getattr(args, "batches", None) is not None and args.batches < 0:
            raise ValueError("--batches must be nonnegative")
        if getattr(args, "limit", None) is not None and args.limit < 0:
            raise ValueError("--limit must be nonnegative")
        read_only = (
            args.cmd in {"check", "doctor", "data-review", "learnt", "media-inventory"}
            or (args.cmd == "jobs" and args.action == "status")
            or (args.cmd == "kaikki" and args.action == "status")
            or (args.cmd == "recovery" and args.action in {"verify", "restore"})
        )
        if read_only:
            return args.func(args)
        with writer_lock():
            if args.cmd not in {"kaikki", "recovery", "run"}:  # run prepares its own database
                with closing(_conn()):
                    pass
            return args.func(args)
    except KeyboardInterrupt:
        print("Interrupted; completed checkpoints retained.", file=sys.stderr)
        return 130
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
