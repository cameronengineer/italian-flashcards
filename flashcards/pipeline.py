"""``./run.sh``: the whole pipeline, safe to rerun at any time.

A run is a fixed sequence of phases. Each is one function below, takes the
:class:`Run` it belongs to, and can be switched off by name (``--skip NAME``,
``--only NAME``):

    phase      does                                              console segment
    ─────────  ────────────────────────────────────────────────  ───────────────
    preflight  settings, dictionary, database backup + upgrade   1 Prepare
    resolve    your edits, lists → root words (when changed)     1 Prepare
    plan       what still needs Claude / Codex → the job queue   2 Cards
    build      the notes Anki should hold                        2 Cards
    publish    sync Anki with what is ready                      3 Anki
    enrich     Claude writes cards, Codex draws, in rounds;      4 Claude
               every round ends with build + publish
    audio      new recordings (capped per run)                   5 Audio & media
    compress   small copies of new images and audio              5 Audio & media
    export     lexicon/*.jsonl and one .apkg per deck            6 Anki · final sync

``build`` and ``publish`` run again after ``enrich`` and after ``audio``, so
Anki always ends up with what the run produced. ``preflight`` always runs.

Nothing is recalculated without a reason: unchanged lists aren't re-read,
finished AI work is never redone, and Anki only gets the differences.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from . import report
from .settings import settings

SEGMENTS = 6
#: Claude's job kinds, in the order the summary lists them.
_TEXT_KINDS = (
    "lexeme_enrich",
    "phrase_enrich",
    "verb_prompts",
    "disambiguate",
    "film_gloss",
    "fact_check",
    "noun_number",
    "cue_check",
)

#: Why a card waits, in the words the summary uses (first match wins).
_WAITING_FOR = (
    ("Claude", ("awaiting AI", "missing content", "awaiting verification")),
    ("an image", ("image",)),
)


def _waiting(conn) -> dict[str, int]:
    """waiting-for → number of cards (each card counted once)."""
    out: dict[str, int] = defaultdict(int)
    for (blocked,) in conn.execute("SELECT blocked_by FROM v4_notes WHERE ready=0"):
        reasons = set((blocked or "").split(","))
        label = next((name for name, keys in _WAITING_FOR if reasons & set(keys)), "other reasons")
        out[label] += 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _cards_line(conn) -> tuple[str, str]:
    ready, total = conn.execute("SELECT sum(ready), count(*) FROM v4_notes").fetchone()
    ready = ready or 0
    waiting = _waiting(conn)
    dormant = conn.execute(
        "SELECT count(*) FROM v4_notes WHERE ready=1 AND instr(' '||tags||' ', ' fc::dormant ')>0"
    ).fetchone()[0]
    first = f"{ready - dormant:,} notes ready to study"
    if dormant:
        first += f" · {dormant:,} dormant"
    first += f" · {total - ready:,} notes waiting"
    second = " · ".join(f"{n:,} for {what}" for what, n in waiting.items())
    return first, second


def _todo(conn) -> str:
    from .queue import count_label, pending_counts

    counts = pending_counts(conn, include_failed=True)
    parts = [count_label(k, counts[k]) for k in _TEXT_KINDS if counts.get(k)]
    return " · ".join(parts)


#: Every phase that can be switched off, in the order a run reaches it.
PHASES = ("resolve", "plan", "build", "publish", "enrich", "audio", "compress", "export")


@dataclass(frozen=True)
class RunOptions:
    """What one run may do. Built from settings.toml [run] and the command line."""

    images: int | None = None  # new Codex images: 0 = none, None = no limit
    audio: int | None = None  # new ElevenLabs recordings: 0 = none, None = no limit
    audio_workers: int = 3
    batches: int | None = 20  # Claude batches per round
    once: bool = False  # stop after one round
    keep_old: bool = False  # keep obsolete notes in Anki
    skip: frozenset = frozenset()  # names from PHASES

    @classmethod
    def from_args(cls, args) -> "RunOptions":
        """From the ``run`` subcommand's arguments (--no-ai / --no-sync are shorthands)."""
        skip = set(getattr(args, "skip", None) or ())
        only = set(getattr(args, "only", None) or ())
        unknown = (skip | only) - set(PHASES)
        if unknown:
            raise ValueError(f"unknown phase(s) {sorted(unknown)}; choose from {', '.join(PHASES)}")
        if only:
            skip |= set(PHASES) - only
        if getattr(args, "no_ai", False):
            skip |= {"enrich", "audio"}
        if getattr(args, "no_sync", False):
            skip.add("publish")
        return cls(
            images=args.images,
            audio=args.audio,
            audio_workers=args.audio_workers,
            batches=args.batches,
            once=args.once,
            keep_old=args.keep_old,
            skip=frozenset(skip),
        )

    def on(self, phase: str) -> bool:
        return phase not in self.skip


@dataclass
class Run:
    """The state one run carries from phase to phase."""

    options: RunOptions
    run_id: str
    run_file: Path
    started: float
    waited_before: float
    code: int = 0
    summary: dict = field(default_factory=dict)
    anki: dict = field(default_factory=lambda: defaultdict(int))  # what the syncs changed
    written: dict = field(default_factory=lambda: defaultdict(int))  # job kind → items written
    failed: dict = field(default_factory=lambda: defaultdict(int))  # job kind → failed attempts
    publishing: bool = True  # off once Anki refuses a sync
    signature: str | None = None  # of the notes at the last successful sync
    anki_backed_up: bool = False
    pending_rejections: int = 0
    lists: list = field(default_factory=list)

    def skipped(self, phase: str) -> str:
        return f"skipped ({phase} is switched off for this run)"


# ── publish ────────────────────────────────────────────────────────────────


def publish(run: Run, conn, *, final: bool = False) -> str:
    """Push what's ready to Anki; returns a one-line result."""
    from . import backup, notes, reconcile
    from .anki import invoke

    if not run.options.on("publish"):
        return "skipped (publish is switched off for this run)"
    if not run.publishing:
        return "skipped"
    signature = notes.signature(conn)
    if not final and signature == run.signature:
        return "up to date"
    try:
        invoke("version")
    except RuntimeError:
        return "Anki isn't open — skipped (open Anki, then run again)"
    if not run.anki_backed_up:
        # Once per run, before the first change: every deck, note, card and review.
        saved = backup.anki_snapshot(invoke("getActiveProfile"))
        run.anki_backed_up = True
        if saved:
            report.line(f"Anki backed up → backups/anki/{saved.name} (all decks + review history)")
    t = time.monotonic()
    try:
        plan = reconcile.run(conn, allow_retire=not run.options.keep_old)
    except RuntimeError as exc:
        run.publishing = False
        run.code = 1
        return f"stopped: {exc}"
    run.signature = signature if not plan.rejected else None
    run.anki["added"] += plan.added
    run.anki["updated"] += len(plan.update)
    run.anki["replaced"] += plan.retired
    run.anki["rejected"] += len(plan.rejected)
    run.anki["suspended"] += plan.suspended
    run.anki["resumed"] += plan.resumed
    run.anki["moved"] += sum(len(cards) for cards, _deck in plan.move)
    run.pending_rejections = len(plan.rejected)
    return f"{plan.result()}  ({report.duration(time.monotonic() - t)})"


# ── preflight, resolve ─────────────────────────────────────────────────────


def preflight(run: Run) -> bool:
    """Settings, dictionary, database backup. False = stop before touching anything."""
    from . import backup, kaikki, lists

    defs = lists.load()
    errors = lists.validate(defs)
    if errors:
        report.item("settings", "problems in lists.toml / plan.toml:")
        for e in errors:
            report.item("", f"- {e}")
        run.code = 1
        return False
    run.lists = defs
    report.item("settings", f"lists.toml + plan.toml OK ({report.plural(len(defs), 'list')})")
    if not kaikki.available():
        report.item("dictionary", "downloading Wiktionary (first run only, ~10 minutes) …")
        kaikki.import_stream()
    report.item("dictionary", "ready")
    backup_path = backup.snapshot("before run")
    report.item("backup", backup_path.name if backup_path else "database unchanged since the last backup")
    return True


def migrate(run: Run, conn, before: int | None) -> None:
    from .db import init_schema

    init_schema(conn)
    after = conn.execute("PRAGMA user_version").fetchone()[0]
    if before is not None and after != before:
        report.item("database", f"upgraded to schema v{after} (backed up first)")


def resolve(run: Run, conn) -> None:
    """Your edits, then lists → root words (skipped when nothing they depend on changed)."""
    from . import lexicon, queue
    from .commands.movie import write_word_list
    from .paths import PROJECT_ROOT

    if not run.options.on("resolve"):
        report.item("lists", run.skipped("resolve"))
        return
    edits = lexicon.import_human_edits(conn)
    if edits:
        report.item("your edits", f"{report.plural(edits, 'field')} marked human in lexicon/lexemes.jsonl")
    t = time.monotonic()
    stats = lexicon.sync_if_changed(conn)
    words, nlists = conn.execute(
        "SELECT count(DISTINCT lexeme_id), count(DISTINCT list_id) FROM list_items"
    ).fetchone()
    if stats is None:
        report.item("lists", f"unchanged · {words:,} words in {nlists} lists")
    else:
        new = sum(s.get("new_lexemes", 0) for s in stats.values())
        report.item(
            "lists",
            f"re-read · {words:,} words in {nlists} lists ({new:,} new)  ({report.duration(time.monotonic() - t)})",
        )
    if stats and stats.get("_removed"):
        gone = stats["_removed"]
        report.item(
            "removed",
            f"lists {', '.join(gone['lists'])} — their obsolete notes leave Anki "
            f"({gone['cards']:,} marked; includes studied notes)",
        )
    run.summary["stages"]["lexicon"] = stats or "unchanged"
    for film in run.lists:
        words_csv = film.path.parent / "words.csv" if film.kind == "movie" and film.path else None
        if words_csv and film.path.exists() and (stats is not None or not words_csv.exists()):
            report.item("film words", str(write_word_list(film).relative_to(PROJECT_ROOT)))
    retried = queue.retry_failed(conn, include_deferred=True, transient_only=True)
    if retried:
        report.item("retry", f"{report.plural(retried, 'job')} from the previous run (timeouts, cooldowns)")


# ── plan, build ────────────────────────────────────────────────────────────


def plan(run: Run, conn) -> None:
    from . import queue

    if run.options.on("plan"):
        queue.plan(conn)


def build(run: Run, conn) -> None:
    from . import notes

    if run.options.on("build"):
        notes.build(conn)


# ── enrich ─────────────────────────────────────────────────────────────────


def enrich(run: Run, conn) -> None:
    """Claude writes the remaining cards and Codex draws, in rounds. Each round
    ends with build + publish; usage limits and retry cooldowns are waited out."""
    from . import lexicon, queue

    options = run.options
    todo = _todo(conn)
    report.item("to write", todo or "nothing")
    if todo:
        report.item("cue groups", "compare words sharing an English prompt; batched after word writing")
    if options.images == 0:
        report.item("images", "off ([run] image_limit = 0)")
    elif options.images:
        report.item("images", f"up to {options.images} this run (Codex)")
    images_left = options.images  # per run, across rounds; None = no limit
    if not options.on("enrich"):
        report.item("", run.skipped("enrich"))
    elif not todo and not (options.images != 0 and queue.pending_counts(conn).get("image")):
        pass
    else:
        round_no = 0
        while True:
            round_no += 1
            result = queue.drain(
                conn, kinds=queue.ORDER, max_batches=options.batches, image_limit=images_left
            )
            done_now = sum(r["done"] for r in result.values())
            for kind, counts in result.items():
                run.written[kind] += counts["done"]
                run.failed[kind] += counts["failed"]
            if images_left is not None:
                images_left = max(0, images_left - sum(result.get("image", {}).values()))
            build(run, conn)
            if options.on("export"):
                lexicon.export_jsonl(conn)
            if done_now:
                report.line(
                    f"── round {round_no}: {report.plural(done_now, 'item')} written · Anki: {publish(run, conn)}"
                )
            if options.once:
                break
            if not done_now:
                remaining = queue.backlog(conn)
                delay = remaining["retry_in_seconds"]
                if delay is not None:
                    report.line(
                        f"{report.plural(remaining['cooling_down'], 'job')} cooling down; "
                        f"retrying in {report.duration(delay + 1)} (Ctrl-C is safe)"
                    )
                    if queue.STOP.wait(delay + 1):
                        raise KeyboardInterrupt
                    continue
                break
        left = _todo(conn)
        report.line(f"stopping: {left} still to write (run again to carry on)" if left else "all written")
    run.summary["stages"]["work"] = {"written": dict(run.written), "failed_attempts": dict(run.failed)}


# ── audio, compress ────────────────────────────────────────────────────────


def audio(run: Run) -> None:
    from .commands import media

    limit = run.options.audio
    if limit != 0 and run.options.on("audio"):
        try:
            made = media.generate_audio(workers=run.options.audio_workers, limit=limit)
            text = f"{report.plural(made['generated'], 'new recording')}"
            if made["failed"]:
                text += f" · {made['failed']} failed"
                run.code = 1
            if made.get("out_of_credit"):
                text += " · stopped: ElevenLabs credits used up (the rest is made next run)"
            if made.get("pending"):
                text += f" · {made['pending']:,} cards still without audio ([run] audio_limit = {limit})"
        except (FileNotFoundError, ValueError) as exc:  # no / empty .elevenlabs key
            made, text, run.code = {"skipped": str(exc)}, f"skipped: {exc}", 1
        run.summary["stages"]["audio"] = made
    else:
        text = "skipped (audio_limit = 0)" if limit == 0 else run.skipped("audio")
    report.item("audio", text)


def compress(run: Run) -> None:
    from .commands import media

    if not run.options.on("compress"):
        report.item("compress", run.skipped("compress"))
        return
    compressed = media.compress(workers=settings.run.compress_workers)
    run.summary["stages"]["compression"] = compressed
    made = {k: v["done"] for k, v in compressed.items() if v.get("done")}
    if any(v.get("failed") for v in compressed.values()):
        run.code = 1
    report.item("compress", " · ".join(f"{n:,} {k}" for k, n in made.items()) or "nothing new")


# ── export ─────────────────────────────────────────────────────────────────


def export(run: Run, conn) -> None:
    """lexicon/*.jsonl and one .apkg per deck in decks/."""
    from . import lexicon
    from .commands.share import export_decks

    if not run.options.on("export"):
        report.item("decks", run.skipped("export"))
        return
    lexicon.export_jsonl(conn)
    t = time.monotonic()
    exported = export_decks(conn)
    text = (
        f"{report.plural(len(exported['written']), 'deck file')} updated in decks/"
        f" ({exported['unchanged']} unchanged)  ({report.duration(time.monotonic() - t)})"
    )
    if exported["missing_media"]:
        text += f" · {exported['missing_media']} media references missing; affected exports retained"
        run.code = 1
    report.item("decks", text)
    run.summary["stages"]["decks"] = {**exported, "written": [p.name for p in exported["written"]]}


# ── report ─────────────────────────────────────────────────────────────────


def exit_code(run: Run) -> int:
    """0 = enabled work completed, 1 = an unresolved failure, 2 = work left."""
    summary, code = run.summary, run.code
    ai = run.options.on("enrich")
    pending = sum(summary["pending"].get(k, 0) for k in _TEXT_KINDS)
    if summary["backlog"]["failed"] and ai and code == 0:
        code = 1
    if run.failed.get("image") and summary["unresolved_image_failures"] and code == 0:
        code = 1
    if (pending and ai or run.pending_rejections) and code == 0:
        code = 2
    return code


def conclude(run: Run, conn) -> dict:
    """What the summary needs from the database, read once at the end."""
    from . import content_review, queue
    from .commands import movie

    listed = "AND id IN (SELECT lexeme_id FROM list_items)"
    first, _second = _cards_line(conn)
    run.summary["audits"] = audited = content_review.audit_coverage(conn)
    run.summary["pending"] = queue.pending_counts(conn)
    run.summary["backlog"] = queue.backlog(conn)
    run.summary["unresolved_image_failures"] = conn.execute(
        "SELECT count(*) FROM ai_jobs WHERE kind='image' AND status IN ('pending','failed') "
        "AND attempts>0 AND error IS NOT NULL"
    ).fetchone()[0]
    return {
        "flagged": conn.execute(
            "SELECT count(*) FROM lexemes WHERE coalesce(issues,'[]') NOT IN ('[]','') "
            f"AND ignored IS NULL {listed}"
        ).fetchone()[0],
        "ignored": conn.execute(
            f"SELECT count(*) FROM lexemes WHERE ignored IS NOT NULL {listed}"
        ).fetchone()[0],
        "audited": audited,
        "cards": first,
        "waiting": _waiting(conn),
        "left": _todo(conn),
        "films": movie.headlines(conn),
        "images_pending": queue.pending_counts(conn).get("image", 0),
    }


def print_summary(run: Run, facts: dict) -> None:
    from .ai import _gate
    from .paths import PROJECT_ROOT
    from .queue import count_label

    print(flush=True)
    report.rule()
    print(f"  Finished in {report.duration(time.monotonic() - run.started)}", flush=True)
    provider_wait = _gate.waited_seconds() - run.waited_before
    if provider_wait >= 1:
        report.item(
            "Time",
            f"{report.duration(provider_wait)} waiting for Claude availability · "
            f"{report.duration(max(0, time.monotonic() - run.started - provider_wait))} other work",
        )
    totals = run.anki
    anki = [
        f"+{totals['added']:,} new notes" if totals["added"] else "",
        f"{totals['updated']:,} note updates" if totals["updated"] else "",
        f"{totals['replaced']:,} obsolete notes removed" if totals["replaced"] else "",
        f"{totals['suspended']:,} newly suspended" if totals["suspended"] else "",
        f"{totals['resumed']:,} dormant cards activated" if totals["resumed"] else "",
        f"{totals['moved']:,} cards moved" if totals["moved"] else "",
        f"{totals['rejected']:,} rejected attempts (retried)" if totals["rejected"] else "",
    ]
    synced = run.publishing and run.options.on("publish")
    report.item("Anki", " · ".join(x for x in anki if x) or ("no changes" if synced else "not synced"))
    claude = " · ".join(count_label(k, n) for k, n in run.written.items() if n)
    bad = sum(run.failed.values())
    report.item(
        "Claude",
        (claude + " written" if claude else "nothing written")
        + (f" · {bad} failed item attempts (includes recovered retries)" if bad else ""),
    )
    report.item("Cards", facts["cards"])
    audited = facts["audited"]
    report.item(
        "Audits",
        f"{audited['audited']:,}/{audited['publishable']:,} publishable notes have a matching audit; {audited['unaudited']:,} unaudited",
    )
    for line in facts["films"]:
        report.item("Movie", line)
    if facts["left"]:
        report.item("Still to do", f"{facts['left']} → run ./run.sh again; it carries on where it stopped")
    if run.summary["backlog"]["failed"]:
        report.item(
            "Retry limit",
            f"{run.summary['backlog']['failed']} jobs exhausted their retry budget; "
            "details are in this run's events log",
        )
    if facts["flagged"]:
        report.item(
            "Flagged",
            f"Claude doubted the dictionary data for {report.plural(facts['flagged'], 'word')} "
            "(published anyway; `./run.sh review` lists them)",
        )
    if facts["ignored"]:
        report.item(
            "Ignored",
            f"{report.plural(facts['ignored'], 'word')} Claude judged not worth a card "
            "(not Italian, a name, a duplicate …; `./run.sh review` lists them)",
        )
    waiting = facts["waiting"]
    if facts["images_pending"] or waiting.get("an image"):
        reason = "off ([run] image_limit = 0)" if run.options.images == 0 else "pending"
        report.item("Images", f"{reason} · {waiting.get('an image', 0):,} notes wait for one")
    if run.options.skip:
        report.item("Switched off", ", ".join(p for p in PHASES if p in run.options.skip))
    report.rule()
    report.item("Run log", str(run.run_file.relative_to(PROJECT_ROOT)))


# ── the run ────────────────────────────────────────────────────────────────


def run(options) -> int:
    """Run the pipeline. ``options`` is a :class:`RunOptions` (or the ``run``
    subcommand's parsed arguments)."""
    from .ai import _gate
    from .db import connect
    from .paths import DB_PATH, PROJECT_ROOT
    from .runtime import atomic_json

    if not isinstance(options, RunOptions):
        options = RunOptions.from_args(options)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    this = Run(
        options=options,
        run_id=run_id,
        run_file=PROJECT_ROOT / "audit_reports" / "runs" / f"{run_id}.json",
        started=time.monotonic(),
        waited_before=_gate.waited_seconds(),
    )
    this.summary = {
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "skipped_phases": [p for p in PHASES if p in options.skip],
        "stages": {},
    }
    report.VERBOSE = False
    previous_events = report.EVENT_FILE
    report.EVENT_FILE = this.run_file.with_suffix(".events.jsonl")
    report.header(f"Italian flashcards · {datetime.now():%a %d %b, %H:%M}")
    try:
        report.section(1, SEGMENTS, "Prepare")
        before = None
        if DB_PATH.exists():
            with closing(sqlite3.connect(DB_PATH)) as raw:
                before = raw.execute("PRAGMA user_version").fetchone()[0]
        if not preflight(this):
            return this.code
        with closing(connect()) as conn:
            migrate(this, conn, before)
            resolve(this, conn)

            report.section(2, SEGMENTS, "Cards")
            plan(this, conn)
            build(this, conn)
            first, second = _cards_line(conn)
            report.item("cards", first)
            if second:
                report.item("waiting", second)

            report.section(3, SEGMENTS, "Anki")
            report.line("syncing what's ready …")
            report.line(publish(this, conn))
            if options.on("export"):
                from . import lexicon

                lexicon.export_jsonl(conn)

            report.section(4, SEGMENTS, "Claude · writing cards")
            enrich(this, conn)

        report.section(5, SEGMENTS, "Audio & media")
        audio(this)
        compress(this)

        report.section(6, SEGMENTS, "Anki · final sync")
        with closing(connect()) as conn:
            build(this, conn)  # includes newly generated audio
            # Reconcile against a fresh read of Anki: local signatures cannot detect
            # edits made in Anki during a run.
            report.line(publish(this, conn, final=True))
            export(this, conn)
            facts = conclude(this, conn)
        print_summary(this, facts)
        this.code = exit_code(this)
        return this.code
    finally:
        report.VERBOSE = True
        summary = this.summary
        summary["exit_code"] = this.code if sys.exc_info()[0] is None else 1
        summary["status"] = (
            "interrupted"
            if isinstance(sys.exc_info()[1], KeyboardInterrupt)
            else "failed"
            if summary["exit_code"] == 1
            else "pending"
            if summary["exit_code"] == 2
            else "complete"
        )
        if sys.exc_info()[1] is not None:
            summary["error"] = str(sys.exc_info()[1])
        summary["anki"] = dict(this.anki)
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        summary["seconds"] = round(time.monotonic() - this.started, 3)
        summary["provider_wait_seconds"] = round(_gate.waited_seconds() - this.waited_before, 3)
        try:
            atomic_json(this.run_file, summary)
            atomic_json(PROJECT_ROOT / "audit_reports" / "run_latest.json", summary)
        finally:
            report.EVENT_FILE = previous_events
