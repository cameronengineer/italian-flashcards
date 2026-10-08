"""The shared base every mode inherits.

A mode answers four questions; :class:`Mode` does everything else.

1. **Rows** — what are the input rows? (default: the source CSV)
2. **Enrichment** — which AI task turns a row into an entry, and which
   extra columns does the answer fill? (``meta``, ``resolve``, ``columns``)
3. **Children** — anything generated per entry afterwards? (verb forms,
   noun phrases: ``children``)
4. **Cards** — which :class:`~flashcards.cards.Card` objects does a live
   entry produce? (``cards``)

The shared ingest flow (``ingest_rows``) is identical for every mode:

* duplicate rows collapse (first occurrence wins);
* rows already in the DB are matched by ``row_key`` — no AI call;
* new rows, rows whose CSV gloss changed, and every row under
  ``build --refresh`` go through the mode's enrichment task;
* which entries are still in the input is recorded for retirement (only
  when every row resolved — an AI failure retires nothing).
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable, Iterable

from .. import csvio
from ..ai import Task
from ..cards import Card, merge_english
from ..sources import Source
from ..tasks import TaskSpec
from ..util import entry_id


class Mode:
    #: Stored in ``entries.mode``; also the registry key.
    name: str = ""
    #: Enrichment task for new / edited rows. ``None`` → rows are stored as-is.
    meta: TaskSpec | None = None
    #: Prompt context when the source has no ``prompt_hint``.
    default_hint: str = ""

    # ── Hooks ─────────────────────────────────────────────────────────────
    def rows(self, source: Source) -> list[csvio.CsvRow]:
        return csvio.read(source.path)

    def row_key(self, row: csvio.CsvRow) -> str:
        """The natural id a row maps to *before* any AI call."""
        return row.italian

    def uses_ai(self, source: Source) -> bool:
        return self.meta is not None

    def meta_task(self, source: Source, row: csvio.CsvRow) -> Task:
        assert self.meta is not None
        return self.meta.task(
            {"italian": row.italian, "english_hint": row.english},
            context=source.prompt_hint or self.default_hint or None,
        )

    def resolve(self, row: csvio.CsvRow, result: dict | None) -> str | None:
        """Natural id once enriched; ``None`` drops the row."""
        if result is not None and not result.get("valid", True):
            return None
        return self.row_key(row) or None

    def english(self, row: csvio.CsvRow, result: dict | None) -> tuple[str, float] | None:
        """(english, confidence) for the entry; ``None`` drops the row."""
        if result is None:
            return (row.english, 1.0) if row.english else None
        if not result.get("valid", True):
            return None
        english = merge_english(result) or row.english
        return (english, float(result.get("confidence", 1.0))) if english else None

    def columns(self, row: csvio.CsvRow, result: dict | None, natural_id: str) -> dict[str, Any]:
        """Mode-specific ``entries`` columns (verb / noun metadata)."""
        return {}

    def children(self, source: Source, ctx) -> None:
        """Second pass once entries exist (verb forms, noun phrases)."""

    def cards(self, source: Source, conn: sqlite3.Connection) -> Iterable[Card]:
        raise NotImplementedError

    # ── Shared flow ───────────────────────────────────────────────────────
    def ingest(self, source: Source, ctx) -> int:
        inserted, _resolved = self.ingest_rows(source, self.rows(source), ctx)
        return inserted

    def ingest_rows(self, source: Source, rows: list[csvio.CsvRow], ctx) -> tuple[int, dict[str, str]]:
        """Enrich + store rows. Returns (inserted, {input text → natural_id})."""
        key = source.key
        unique: dict[str, csvio.CsvRow] = {}
        for r in rows:
            unique.setdefault(self.row_key(r), r)
        rows = list(unique.values())

        # natural_id → the CSV gloss the entry was built from (None for rows
        # ingested before edits were tracked).
        existing: dict[str, str | None] = {
            r["natural_id"]: r["input_english"]
            for r in ctx.conn.execute(
                "SELECT natural_id, input_english FROM entries WHERE source_path = ? AND mode = ?",
                (key, self.name),
            )
        }
        baseline = [r for r in rows if self.row_key(r) in existing and existing[self.row_key(r)] is None]
        if baseline:
            ctx.conn.executemany(
                "UPDATE entries SET input_english = ? WHERE source_path = ? AND mode = ? AND natural_id = ?",
                [(r.english, key, self.name, self.row_key(r)) for r in baseline],
            )
            ctx.conn.commit()
            for r in baseline:
                existing[self.row_key(r)] = r.english

        refresh = ctx.refreshing(source)
        resolved: dict[str, str] = {}
        pending: list[csvio.CsvRow] = []
        edited: list[csvio.CsvRow] = []
        for r in rows:
            k = self.row_key(r)
            if k in existing:
                resolved[r.italian] = k
                if refresh or existing[k] != r.english:
                    edited.append(r)
            else:
                pending.append(r)

        inserted = 0
        complete = True
        work = pending + edited
        if work:
            if self.uses_ai(source):
                results: Iterable = ctx.ai.run_many(
                    work, lambda r: self.meta_task(source, r),
                    workers=ctx.workers,
                    label=f"{self.name}/{source.id}",
                    describe=lambda r: r.italian,
                    refresh=refresh,
                )
            else:
                results = ((r, None) for r in work)
            for r, result in results:
                if isinstance(result, Exception):
                    complete = False
                    continue
                k = self.row_key(r)
                with ctx.db_lock:
                    if k in existing:
                        self._update(ctx.conn, source, r, result, k)
                    else:
                        nid = self.resolve(r, result)
                        if nid is not None:
                            inserted += self._insert(ctx.conn, source, r, result, nid)
                            resolved[r.italian] = nid
                    ctx.conn.commit()

        ctx.mark_live(key, self.name, set(resolved.values()), complete=complete)
        self.children(source, ctx)
        return inserted, resolved

    def materialise(self, source: Source, ctx) -> int:
        n = 0
        for card in self.cards(source, ctx.conn):
            ctx.add_card(card, source)
            n += 2
        return n

    # ── Helpers for subclasses ────────────────────────────────────────────
    def live_entries(self, conn: sqlite3.Connection, source: Source) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM entries WHERE source_path = ? AND mode = ? AND retired = 0 ORDER BY rowid",
            (source.key, self.name),
        ).fetchall()

    def generate(
        self,
        ctx,
        *,
        label: str,
        items: list,
        make_task: Callable[[Any], Task],
        store: Callable[[sqlite3.Connection, Any, dict], None],
        describe: Callable[[Any], str],
    ) -> None:
        """Run a per-item AI task and store each answer (used by ``children``)."""
        if not items:
            return
        for item, result in ctx.ai.run_many(
            items, make_task, workers=ctx.workers, label=label, describe=describe,
        ):
            if isinstance(result, Exception):
                continue
            with ctx.db_lock:
                store(ctx.conn, item, result)
                ctx.conn.commit()

    def _insert(self, conn, source: Source, row, result, natural_id: str) -> int:
        got = self.english(row, result)
        if got is None:
            return 0
        english, confidence = got
        cols = {
            "id": entry_id(source.key, self.name, natural_id),
            "source_path": source.key,
            "natural_id": natural_id,
            "mode": self.name,
            "deck": source.deck,
            "italian": natural_id,
            "english": english,
            "input_english": row.english,
            "confidence": confidence,
            **self.columns(row, result, natural_id),
        }
        names = ", ".join(cols)
        marks = ", ".join("?" * len(cols))
        cursor = conn.execute(
            f"INSERT OR IGNORE INTO entries ({names}) VALUES ({marks})", tuple(cols.values())
        )
        return cursor.rowcount

    def _update(self, conn, source: Source, row, result, natural_id: str) -> None:
        """Apply a changed CSV gloss (or a refresh) to an existing entry.

        Only the English side changes; the entry id — and so every GUID — stays.
        """
        got = self.english(row, result)
        if got is None:
            conn.execute(
                "UPDATE entries SET input_english = ? WHERE source_path = ? AND mode = ? AND natural_id = ?",
                (row.english, source.key, self.name, natural_id),
            )
            return
        english, confidence = got
        conn.execute(
            "UPDATE entries SET english = ?, confidence = ?, input_english = ? "
            "WHERE source_path = ? AND mode = ? AND natural_id = ?",
            (english, confidence, row.english, source.key, self.name, natural_id),
        )
