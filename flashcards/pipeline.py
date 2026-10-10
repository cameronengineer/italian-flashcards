"""``./run.sh``: everything, in six segments, safe to rerun at any time.

1. Prepare   backup, settings, dictionary, your edits, lists (skipped when
             unchanged), approvals from review.csv, retry of failed jobs
2. Cards     what Anki should hold, and what each waiting card waits for
3. Anki      sync what's ready now
4. Claude    write the remaining cards in rounds; Anki is synced after
             every round, and usage limits are waited out
5. Media     new audio (capped per run) and compression
6. Anki      final sync, then a summary of what happened and what's next

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

from . import report
from .settings import settings

SEGMENTS = 6

#: Why a card waits, in the words the summary uses (first match wins).
_WAITING_FOR = (
    ("your review", ("needs review", "failed audit")),
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
    first = f"{ready:,} ready to study · {total - ready:,} waiting"
    second = " · ".join(f"{n:,} for {what}" for what, n in waiting.items())
    return first, second


def _todo(conn) -> str:
    from .queue import KIND_NAME, pending_counts

    counts = pending_counts(conn)
    parts = [
        f"{counts[k]:,} {KIND_NAME[k]}"
        for k in ("lexeme_enrich", "phrase_enrich", "verb_prompts", "disambiguate")
        if counts.get(k)
    ]
    return " · ".join(parts)


def run(args) -> int:
    from . import backup, kaikki, lexicon, lists, notes, queue, reconcile, content_review
    from .anki import invoke
    from .commands import media, movie, review
    from .db import connect, init_schema
    from .paths import DB_PATH, PROJECT_ROOT
    from .runtime import atomic_json

    report.VERBOSE = False
    started = time.monotonic()
    report.header(f"Italian flashcards · {datetime.now():%a %d %b, %H:%M}")
    code = 0
    summary: dict = {"started_at": datetime.now(timezone.utc).isoformat(), "stages": {}}
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
            return 1
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
                    f"lists {', '.join(gone['lists'])} — their unstudied cards leave Anki "
                    f"({gone['cards']:,} marked; anything you've studied stays)",
                )
            summary["stages"]["lexicon"] = stats or "unchanged"
            corrected = content_review.apply_file(conn)
            if corrected:
                report.line(f"Applied {corrected} reviewed cue correction(s)")
            approved = review.apply_review_file(conn)
            if approved:
                report.item("review.csv", f"{report.plural(approved, 'word')} you approved — released")
            retried = queue.retry_failed(conn)
            if retried:
                report.item("retry", f"{report.plural(retried, 'job')} that failed last time")

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
                    if any(r["failed"] for r in result.values()):
                        code = 1
                    notes.build(conn)
                    lexicon.export_jsonl(conn)
                    if done_now:
                        report.line(
                            f"── round {round_no}: {report.plural(done_now, 'item')} written · Anki: {sync(conn)}"
                        )
                    if args.once or not done_now:
                        break
                left = _todo(conn)
                report.line(
                    f"stopping: {left} still to write (run again to carry on)" if left else "all written"
                )
            summary["stages"]["work"] = {"written": dict(written), "failed": dict(failed)}

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
            corrected = content_review.apply_file(conn)
            if corrected:
                report.line(f"Applied {corrected} reviewed cue correction(s)")
            approved = review.apply_review_file(conn)
            if approved:
                queue.plan(conn)
                report.line(f"review.csv: {approved} late approval(s) included in the final build")
                remaining = _todo(conn)
                if remaining:
                    report.line(f"Approved content may still need generation on the next run: {remaining}")
                    if code == 0:
                        code = 2
            notes.build(conn)  # includes late approvals and newly generated audio
            # Reconcile against fresh bulk reads of v4/adopted notes; keep the legacy
            # index. Local signatures cannot detect edits made in Anki during a run.
            report.line(sync(conn, final=True))
            lexicon.export_jsonl(conn)
            held = review.write_review_file(conn)
            cue_count = content_review.write_file(conn)
            grammar_count = content_review.write_grammar_file(conn)
            summary["grammar_review"] = grammar_count
            audited = content_review.audit_coverage(conn)
            summary["audits"] = audited
            summary["cue_review"] = cue_count
            first, second = _cards_line(conn)
            waiting = _waiting(conn)
            left = _todo(conn)
            films = movie.headlines(conn)
            images_pending = queue.pending_counts(conn).get("image", 0)
            summary["pending"] = queue.pending_counts(conn)

        # ── Summary ──────────────────────────────────────────────────────
        print(flush=True)
        report.rule()
        print(f"  Finished in {report.duration(time.monotonic() - started)}", flush=True)
        anki = [
            f"+{anki_totals['added']:,} new cards" if anki_totals["added"] else "",
            f"{anki_totals['updated']:,} updated" if anki_totals["updated"] else "",
            f"{anki_totals['replaced']:,} unstudied old cards removed" if anki_totals["replaced"] else "",
            f"{anki_totals['rejected']:,} rejected attempts (retried)" if anki_totals["rejected"] else "",
        ]
        report.item(
            "Anki", " · ".join(x for x in anki if x) or ("no changes" if state["syncing"] else "not synced")
        )
        from .queue import KIND_NAME

        claude = " · ".join(f"{n:,} {KIND_NAME[k]}" for k, n in written.items() if n)
        bad = sum(failed.values())
        report.item(
            "Claude",
            (claude + " written" if claude else "nothing written")
            + (f" · {bad} failed (retried next run)" if bad else ""),
        )
        report.item("Cards", first)
        report.item(
            "Audits",
            f"{audited['audited']:,}/{audited['publishable']:,} publishable notes have a matching audit; {audited['unaudited']:,} unaudited",
        )
        if grammar_count:
            report.item(
                "Grammar", f"{grammar_count} published/planned drills held; details in grammar_review.csv"
            )
        if cue_count:
            report.item(
                "Cue review",
                f"{cue_count} ambiguous cues in cue_review.csv; add a hint or accepted alternatives with a reason",
            )
        for line in films:
            report.item("Movie", line)
        if left:
            report.item("Still to do", f"{left} → run ./run.sh again; it carries on where it stopped")
        if held:
            report.item(
                "Needs you",
                f"{report.plural(held, 'word')} Claude disputed are holding back "
                f"{waiting.get('your review', 0):,} cards —",
            )
            report.item("", 'open review.csv, put "yes" in the approve column for the fine ones, run again')
        if images_pending or waiting.get("an image"):
            reason = "off ([run] image_limit = 0)" if args.images == 0 else "pending"
            report.item("Images", f"{reason} · {waiting.get('an image', 0):,} cards wait for one")
        report.rule()
        pending = sum(
            summary["pending"].get(k, 0)
            for k in ("lexeme_enrich", "phrase_enrich", "verb_prompts", "disambiguate")
        )
        if (pending and not args.no_ai or state.get("pending_rejections")) and code == 0:
            code = 2
        return code
    finally:
        report.VERBOSE = True
        summary["exit_code"] = code if sys.exc_info()[0] is None else 1
        summary["anki"] = dict(anki_totals)
        atomic_json(PROJECT_ROOT / "audit_reports" / "run_latest.json", summary)
