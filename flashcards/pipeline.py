"""``./run.sh``: everything, in six segments, safe to rerun at any time.

1. Prepare   backup, settings, dictionary, your edits, lists (skipped when
             unchanged), retry of failed jobs
2. Cards     what Anki should hold, and what each waiting card waits for
3. Anki      sync what's ready now
4. Claude    write the remaining cards in rounds; Anki is synced after
             every round, and usage limits are waited out
5. Media     new audio (capped per run) and compression
6. Anki      final sync, one .apkg per deck in decks/, then a summary

Nothing is recalculated without a reason: unchanged lists aren't re-read,
finished AI work is never redone, and Anki only gets the differences.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from uuid import uuid4

from . import report
from .settings import settings

SEGMENTS = 6

#: Why a card waits, in the words the summary uses (first match wins).
_WAITING_FOR = (
    ("Claude", ("awaiting AI", "missing content", "awaiting verification", "awaiting verb prompts")),
    ("an image", ("image",)),
    ("the study horizon", ("outside study horizon",)),
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
    parts = [
        count_label(k, counts[k])
        for k in ("lexeme_enrich", "phrase_enrich", "verb_prompts", "disambiguate", "film_gloss")
        if counts.get(k)
    ]
    return " · ".join(parts)


def run(args) -> int:
    from . import backup, kaikki, lexicon, lists, notes, queue, reconcile, content_review
    from .anki import invoke
    from .commands import media, movie
    from .db import connect, init_schema
    from .paths import DB_PATH, PROJECT_ROOT
    from .runtime import atomic_json
    from .ai import _gate

    report.VERBOSE = False
    started = time.monotonic()
    waited_before = _gate.waited_seconds()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    run_file = PROJECT_ROOT / "audit_reports" / "runs" / f"{run_id}.json"
    previous_events = report.EVENT_FILE
    report.EVENT_FILE = run_file.with_suffix(".events.jsonl")
    report.header(f"Italian flashcards · {datetime.now():%a %d %b, %H:%M}")
    code = 0
    summary: dict = {"run_id": run_id, "started_at": datetime.now(timezone.utc).isoformat(), "stages": {}}
    anki_totals: dict[str, int] = defaultdict(int)
    written: dict[str, int] = defaultdict(int)
    failed: dict[str, int] = defaultdict(int)
    state = {"syncing": not args.no_sync, "signature": None}

    def sync(conn, *, final=False) -> str:
        """Push what's ready to Anki; returns a one-line result."""
        nonlocal code
        if not state["syncing"]:
            return "skipped" + (" (--no-sync)" if args.no_sync else "")
        signature = notes.signature(conn)
        if not final and signature == state["signature"]:
            return "up to date"
        try:
            invoke("version")
        except RuntimeError:
            return "Anki isn't open — skipped (open Anki, then run again)"
        if not state.get("anki_backup"):
            # Once per run, before the first change: every deck, note, card and review.
            saved = backup.anki_snapshot(invoke("getActiveProfile"))
            state["anki_backup"] = True
            if saved:
                report.line(f"Anki backed up → backups/anki/{saved.name} (all decks + review history)")
        t = time.monotonic()
        try:
            plan = reconcile.run(conn, allow_retire=not args.keep_old)
        except RuntimeError as exc:
            state["syncing"] = False
            code = 1
            return f"stopped: {exc}"
        state["signature"] = signature if not plan.rejected else None
        anki_totals["added"] += plan.added
        anki_totals["updated"] += len(plan.update)
        anki_totals["refreshed"] += len(plan.adopt)
        anki_totals["replaced"] += plan.retired
        anki_totals["rejected"] += len(plan.rejected)
        anki_totals["suspended"] += plan.suspended
        anki_totals["resumed"] += plan.resumed
        anki_totals["moved"] += sum(len(cards) for cards, _deck in plan.move)
        state["pending_rejections"] = len(plan.rejected)
        return f"{plan.result()}  ({report.duration(time.monotonic() - t)})"

    try:
        # ── 1. Prepare ───────────────────────────────────────────────────
        report.section(1, SEGMENTS, "Prepare")
        defs = lists.load()
        errors = lists.validate(defs)
        if errors:
            report.item("settings", "problems in lists.toml / plan.toml:")
            for e in errors:
                report.item("", f"- {e}")
            code = 1
            return code
        report.item("settings", f"lists.toml + plan.toml OK ({report.plural(len(defs), 'list')})")
        if not kaikki.available():
            report.item("dictionary", "downloading Wiktionary (first run only, ~10 minutes) …")
            kaikki.import_stream()
        report.item("dictionary", "ready")
        before = None
        if DB_PATH.exists():
            with closing(sqlite3.connect(DB_PATH)) as raw:
                before = raw.execute("PRAGMA user_version").fetchone()[0]
        backup_path = backup.snapshot("before run")
        report.item("backup", backup_path.name if backup_path else "database unchanged since the last backup")
        with closing(connect()) as conn:
            init_schema(conn)
            after = conn.execute("PRAGMA user_version").fetchone()[0]
            if before is not None and after != before:
                report.item("database", f"upgraded to schema v{after} (backed up first)")
            edits = lexicon.import_human_edits(conn)
            if edits:
                report.item(
                    "your edits", f"{report.plural(edits, 'field')} marked human in lexicon/lexemes.jsonl"
                )
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
            summary["stages"]["lexicon"] = stats or "unchanged"
            from .commands.movie import write_word_list

            for film in defs:
                words_csv = film.path.parent / "words.csv" if film.kind == "movie" and film.path else None
                if words_csv and film.path.exists() and (stats is not None or not words_csv.exists()):
                    report.item("film words", str(write_word_list(film).relative_to(PROJECT_ROOT)))
            retried = queue.retry_failed(conn, include_deferred=True)
            if retried:
                report.item(
                    "retry",
                    f"{report.plural(retried, 'job')} from the previous run, including retry cooldowns",
                )

            # ── 2. Cards ─────────────────────────────────────────────────
            report.section(2, SEGMENTS, "Cards")
            queue.plan(conn)
            notes.build(conn)
            first, second = _cards_line(conn)
            report.item("cards", first)
            if second:
                report.item("waiting", second)

            # ── 3. Anki ──────────────────────────────────────────────────
            report.section(3, SEGMENTS, "Anki")
            report.line("syncing what's ready …")
            report.line(sync(conn))
            lexicon.export_jsonl(conn)

            # ── 4. Claude ────────────────────────────────────────────────
            todo = _todo(conn)
            report.section(4, SEGMENTS, "Claude · writing cards")
            report.item("to write", todo or "nothing")
            if todo:
                report.item(
                    "cue groups", "compare words sharing an English prompt; batched after word writing"
                )
            if args.images == 0:
                report.item("images", "off ([run] image_limit = 0)")
            elif args.images:
                report.item("images", f"up to {args.images} this run (Codex)")
            images_left = args.images  # per run, across rounds; None = no limit
            if args.no_ai:
                report.item("", "skipped (--no-ai)")
            elif not todo and not (args.images != 0 and queue.pending_counts(conn).get("image")):
                pass
            else:
                round_no = 0
                while True:
                    round_no += 1
                    result = queue.drain(
                        conn, kinds=queue.ORDER, max_batches=args.batches, image_limit=images_left
                    )
                    done_now = sum(r["done"] for r in result.values())
                    for kind, counts in result.items():
                        written[kind] += counts["done"]
                        failed[kind] += counts["failed"]
                    if images_left is not None:
                        images_left = max(0, images_left - sum(result.get("image", {}).values()))
                    notes.build(conn)
                    lexicon.export_jsonl(conn)
                    if done_now:
                        report.line(
                            f"── round {round_no}: {report.plural(done_now, 'item')} written · Anki: {sync(conn)}"
                        )
                    if args.once:
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
                report.line(
                    f"stopping: {left} still to write (run again to carry on)" if left else "all written"
                )
            summary["stages"]["work"] = {"written": dict(written), "failed_attempts": dict(failed)}

        # ── 5. Media ─────────────────────────────────────────────────────
        report.section(5, SEGMENTS, "Audio & media")
        if args.audio != 0 and not args.no_ai:
            try:
                audio = media.generate_audio(workers=args.audio_workers, limit=args.audio)
                text = f"{report.plural(audio['generated'], 'new recording')}"
                if audio["failed"]:
                    text += f" · {audio['failed']} failed"
                    code = 1
                if audio.get("pending"):
                    text += f" · {audio['pending']:,} cards still without audio ([run] audio_limit = {args.audio})"
            except (FileNotFoundError, ValueError) as exc:  # no / empty .elevenlabs key
                audio, text, code = {"skipped": str(exc)}, f"skipped: {exc}", 1
            summary["stages"]["audio"] = audio
        else:
            text = "skipped (audio_limit = 0)" if args.audio == 0 else "skipped (--no-ai)"
        report.item("audio", text)
        compressed = media.compress(workers=settings.run.compress_workers)
        summary["stages"]["compression"] = compressed
        made = {k: v["done"] for k, v in compressed.items() if v.get("done")}
        if any(v.get("failed") for v in compressed.values()):
            code = 1
        report.item("compress", " · ".join(f"{n:,} {k}" for k, n in made.items()) or "nothing new")

        # ── 6. Anki (final) ──────────────────────────────────────────────
        report.section(6, SEGMENTS, "Anki · final sync")
        with closing(connect()) as conn:
            notes.build(conn)  # includes newly generated audio
            # Reconcile against fresh bulk reads of v4/adopted notes; keep the legacy
            # index. Local signatures cannot detect edits made in Anki during a run.
            report.line(sync(conn, final=True))
            lexicon.export_jsonl(conn)
            from .commands.share import export_decks

            t = time.monotonic()
            exported = export_decks(conn)
            text = (
                f"{report.plural(len(exported['written']), 'deck file')} updated in decks/"
                f" ({exported['unchanged']} unchanged)  ({report.duration(time.monotonic() - t)})"
            )
            if exported["missing_media"]:
                text += f" · {exported['missing_media']} media references missing; affected exports retained"
                code = 1
            report.item("decks", text)
            summary["stages"]["decks"] = {**exported, "written": [p.name for p in exported["written"]]}
            flagged = conn.execute(
                "SELECT count(*) FROM lexemes WHERE coalesce(issues,'[]') NOT IN ('[]','') "
                "AND id IN (SELECT lexeme_id FROM list_items)"
            ).fetchone()[0]
            audited = content_review.audit_coverage(conn)
            summary["audits"] = audited
            first, second = _cards_line(conn)
            waiting = _waiting(conn)
            left = _todo(conn)
            films = movie.headlines(conn)
            images_pending = queue.pending_counts(conn).get("image", 0)
            summary["pending"] = queue.pending_counts(conn)
            summary["backlog"] = queue.backlog(conn)
            summary["unresolved_image_failures"] = conn.execute(
                "SELECT count(*) FROM ai_jobs WHERE kind='image' AND status IN ('pending','failed') "
                "AND attempts>0 AND error IS NOT NULL"
            ).fetchone()[0]

        # ── Summary ──────────────────────────────────────────────────────
        print(flush=True)
        report.rule()
        print(f"  Finished in {report.duration(time.monotonic() - started)}", flush=True)
        provider_wait = _gate.waited_seconds() - waited_before
        if provider_wait >= 1:
            report.item(
                "Time",
                f"{report.duration(provider_wait)} waiting for Claude availability · "
                f"{report.duration(max(0, time.monotonic() - started - provider_wait))} other work",
            )
        anki = [
            f"+{anki_totals['added']:,} new notes" if anki_totals["added"] else "",
            f"{anki_totals['updated']:,} note updates" if anki_totals["updated"] else "",
            f"{anki_totals['replaced']:,} obsolete notes removed" if anki_totals["replaced"] else "",
            f"{anki_totals['suspended']:,} newly suspended" if anki_totals["suspended"] else "",
            f"{anki_totals['resumed']:,} dormant cards activated" if anki_totals["resumed"] else "",
            f"{anki_totals['moved']:,} cards moved" if anki_totals["moved"] else "",
            f"{anki_totals['rejected']:,} rejected attempts (retried)" if anki_totals["rejected"] else "",
        ]
        report.item(
            "Anki", " · ".join(x for x in anki if x) or ("no changes" if state["syncing"] else "not synced")
        )
        from .queue import count_label

        claude = " · ".join(count_label(k, n) for k, n in written.items() if n)
        bad = sum(failed.values())
        report.item(
            "Claude",
            (claude + " written" if claude else "nothing written")
            + (f" · {bad} failed item attempts (includes recovered retries)" if bad else ""),
        )
        report.item("Cards", first)
        report.item(
            "Audits",
            f"{audited['audited']:,}/{audited['publishable']:,} publishable notes have a matching audit; {audited['unaudited']:,} unaudited",
        )
        for line in films:
            report.item("Movie", line)
        if left:
            report.item("Still to do", f"{left} → run ./run.sh again; it carries on where it stopped")
        if summary["backlog"]["failed"]:
            report.item(
                "Retry limit",
                f"{summary['backlog']['failed']} jobs exhausted their retry budget; "
                "details are in this run's events log",
            )
        if flagged:
            report.item(
                "Flagged",
                f"Claude doubted the dictionary data for {report.plural(flagged, 'word')} "
                "(published anyway; `./run.sh review` lists them)",
            )
        if images_pending or waiting.get("an image"):
            reason = "off ([run] image_limit = 0)" if args.images == 0 else "pending"
            report.item("Images", f"{reason} · {waiting.get('an image', 0):,} notes wait for one")
        report.rule()
        report.item("Run log", str(run_file.relative_to(PROJECT_ROOT)))
        pending = sum(
            summary["pending"].get(k, 0)
            for k in ("lexeme_enrich", "phrase_enrich", "verb_prompts", "disambiguate", "film_gloss")
        )
        if summary["backlog"]["failed"] and not args.no_ai and code == 0:
            code = 1
        if failed.get("image") and summary["unresolved_image_failures"] and code == 0:
            code = 1
        if (pending and not args.no_ai or state.get("pending_rejections")) and code == 0:
            code = 2
        return code
    finally:
        report.VERBOSE = True
        summary["exit_code"] = code if sys.exc_info()[0] is None else 1
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
        summary["anki"] = dict(anki_totals)
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        summary["seconds"] = round(time.monotonic() - started, 3)
        summary["provider_wait_seconds"] = round(_gate.waited_seconds() - waited_before, 3)
        try:
            atomic_json(run_file, summary)
            atomic_json(PROJECT_ROOT / "audit_reports" / "run_latest.json", summary)
        finally:
            report.EVENT_FILE = previous_events
