"""CLI entry point: ``flashcards <command>`` (or ``./fc.sh <command>``).

Pipeline (``run`` does all of it, in this order)
  check       Validate lists.toml and plan.toml.
  lexicon     Resolve every list to root words (Kaikki + rules); export JSONL.
  jobs        Show the AI / image queue (also: jobs retry, jobs refresh LIST).
  work        Work through the queue: Claude for text, Codex for images.
  notes       Compute the notes Anki should hold.
  plan        Dry run: what `apply` would change in Anki.
  apply       Sync Anki: add, update, adopt studied notes, retire, reorder.
  audio       ElevenLabs audio, in study order.
  run         Everything above (backs up the DB first).

Movies
  movie       Coverage report + word dataset for a film.

Study tools
  practice    Translation practice from your learnt words (mistakes → cards).
  learnt      Export every word you've graduated to review.
  leech       Suspend + tag cards you keep failing (--doctor: memory aids).

Quality
  audit       AI review of the notes (verdicts saved).
  review      Disputed roots, failed audits, hidden facts, failed jobs.

Other
  kaikki      Import the Wiktionary (Kaikki) Italian dictionary.
  share       Export ready notes as an .apkg for someone else.
"""

from __future__ import annotations

import argparse
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

    with closing(_conn()) as conn:
        if args.action == "retry":
            print(f"  {queue.retry_failed(conn)} failed job(s) back in the queue")
        elif args.action == "refresh":
            if not args.list:
                print("  usage: flashcards jobs refresh LIST")
                return 1
            print(f"  {queue.refresh(conn, args.list)} root(s) of {args.list} will be re-asked")
            print(f"  queued: {queue.plan(conn)}")
        else:
            print(f"  newly queued: {queue.plan(conn)}")
            rows = queue.status(conn)
            print(table(["Kind", "Status", "Jobs"], [list(r) for r in rows]) if rows else "  queue empty")
    return 0


def cmd_work(args) -> int:
    from . import queue

    with closing(_conn()) as conn:
        queue.plan(conn)
        kinds = args.kind or [k for k in queue.ORDER if not (args.no_images and k == "image")]
        print(queue.drain(conn, kinds=kinds, max_batches=args.batches))
    return 0


def cmd_notes(_args) -> int:
    from . import notes

    with closing(_conn()) as conn:
        notes.build(conn)
    return 0


def cmd_plan(args) -> int:
    from . import reconcile

    with closing(_conn()) as conn:
        reconcile.run(conn, dry_run=True)
    return 0


def cmd_apply(args) -> int:
    from . import reconcile

    with closing(_conn()) as conn:
        reconcile.run(conn, dry_run=False, allow_retire=args.allow_retire)
    return 0


def cmd_audio(args) -> int:
    from .commands import media

    if args.per_deck:
        media.generate_audio_per_deck(args.per_deck, workers=args.workers)
    else:
        media.generate_audio(workers=args.workers, limit=args.limit, decks=args.deck or None)
    media.compress(workers=settings.run.compress_workers)
    return 0


def cmd_run(args) -> int:
    from . import backup, lexicon, notes, queue, reconcile
    from .anki import invoke
    from .commands import media

    backup.snapshot("before run")
    with closing(_conn()) as conn:
        edits = lexicon.import_human_edits(conn)
        if edits:
            print(f"  applied {edits} human edit(s) from lexicon/lexemes.jsonl")
        lexicon.sync(conn)
        print(f"  queued: {queue.plan(conn)}")
        notes.build(conn)
        anki_up = True
        try:
            invoke("version")
        except RuntimeError:
            anki_up = False
            print("\n  Anki is not running — skipping sync this run (open Anki and run `./fc.sh apply`).")
        if anki_up and not args.no_sync:
            reconcile.run(conn, dry_run=False, allow_retire=args.allow_retire)  # sync what's ready now
        if not args.no_ai:
            kinds = [k for k in queue.ORDER if not (args.no_images and k == "image")]
            queue.drain(conn, kinds=kinds)
            queue.plan(conn)  # follow-up jobs (disambiguation, images for new roots)
            queue.drain(conn, kinds=kinds)
        notes.build(conn)
    media.generate_audio(workers=args.audio_workers, limit=args.audio_limit)
    media.compress(workers=settings.run.compress_workers)
    with closing(_conn()) as conn:
        notes.build(conn)  # audio tags for newly generated files
        if anki_up and not args.no_sync:
            reconcile.run(conn, dry_run=False, allow_retire=args.allow_retire)
        print(f"  exported {lexicon.export_jsonl(conn)}")
    return 0


def cmd_movie(args) -> int:
    from .commands import movie

    return movie.run(args.list)


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

    if args.doctor:
        return leech.doctor(unsuspend=args.unsuspend)
    return leech.run(deck=args.deck, threshold=args.threshold,
                     update_config=args.update_config, dry_run=args.dry_run)


def cmd_audit(args) -> int:
    from .commands import audit

    return audit.run(decks=args.deck, limit=args.limit, workers=args.workers,
                     only_problems=args.only_problems, out=args.out)


def cmd_review(args) -> int:
    from .commands import review

    if args.action == "approve":
        return review.approve(lemmas=args.lemmas, all_=args.all)
    return review.run(limit=args.limit)


def cmd_share(args) -> int:
    from .commands import share

    return share.run(decks=args.deck, out=args.out)


def build_parser() -> argparse.ArgumentParser:
    run_cfg = settings.run
    p = argparse.ArgumentParser(prog="flashcards", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")

    sub.add_parser("check", help="Validate lists.toml + plan.toml.").set_defaults(func=cmd_check)

    sp = sub.add_parser("kaikki", help="Import / inspect the Kaikki dictionary.")
    sp.add_argument("action", nargs="?", choices=["import", "status"], default="status")
    sp.set_defaults(func=cmd_kaikki)

    sub.add_parser("lexicon", help="Resolve lists to root words; export JSONL.").set_defaults(func=cmd_lexicon)

    sp = sub.add_parser("jobs", help="Show / retry / refresh the AI queue.")
    sp.add_argument("action", nargs="?", choices=["status", "retry", "refresh"], default="status")
    sp.add_argument("list", nargs="?", help="list id for `jobs refresh`")
    sp.set_defaults(func=cmd_jobs)

    sp = sub.add_parser("work", help="Work through the AI / image queue.")
    sp.add_argument("--kind", action="append", choices=["lexeme_enrich", "phrase_enrich", "verb_prompts", "disambiguate", "image"])
    sp.add_argument("--batches", type=int, default=None, help="Stop after N batches.")
    sp.add_argument("--no-images", action="store_true")
    sp.set_defaults(func=cmd_work)

    sub.add_parser("notes", help="Compute the desired Anki notes.").set_defaults(func=cmd_notes)
    sub.add_parser("plan", help="Dry run of `apply`.").set_defaults(func=cmd_plan)
    sp = sub.add_parser("apply", help="Sync Anki.")
    sp.add_argument("--allow-retire", action="store_true",
                    help="Allow retiring more unstudied notes than the [sync] limit.")
    sp.set_defaults(func=cmd_apply)

    sp = sub.add_parser("audio", help="ElevenLabs audio in study order.")
    sp.add_argument("--workers", type=int, default=run_cfg.audio_workers)
    sp.add_argument("--limit", type=int, default=run_cfg.audio_limit)
    sp.add_argument("--deck", action="append", metavar="DECK")
    sp.add_argument("--per-deck", type=int, default=None, metavar="N")
    sp.set_defaults(func=cmd_audio)

    sp = sub.add_parser("run", help="Full pipeline.")
    sp.add_argument("--audio-workers", type=int, default=run_cfg.audio_workers)
    sp.add_argument("--audio-limit", type=int, default=run_cfg.audio_limit)
    sp.add_argument("--no-ai", action="store_true", help="Skip the AI / image queue this run.")
    sp.add_argument("--no-images", action="store_true", help="Skip Codex images this run.")
    sp.add_argument("--no-sync", action="store_true", help="Don't touch Anki.")
    sp.add_argument("--allow-retire", action="store_true")
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
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_review)

    sp = sub.add_parser("share", help="Export ready notes as .apkg.")
    sp.add_argument("--deck", action="append", metavar="DECK")
    sp.add_argument("--out", type=Path, default=None)
    sp.set_defaults(func=cmd_share)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
