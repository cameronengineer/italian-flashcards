"""``gloss`` mode — the default. One CSV row → one entry → one card pair.

The English text on the back may be the CSV's own gloss verbatim
(``enrich: false``) or an AI-enriched version with disambiguation /
usage-note suffixes (``enrich: true``, the default).
"""

from __future__ import annotations

import json
import sqlite3
import threading

from .. import csvio
from ..openrouter import (
    SCHEMA_GLOSS,
    cached_structured,
    merge_english,
)
from ..pool import run_pool
from ..sources import Source
from ..util import entry_id, md5_hex


class GlossMode:
    name = "gloss"

    # ── Ingest ────────────────────────────────────────────────────────────
    def ingest(self, source: Source, ctx) -> int:
        rows = csvio.read(source.path)
        if not rows:
            return 0

        sp = str(source.path.resolve())
        existing = self._existing(ctx.conn, sp)
        pending = [r for r in rows if r.italian not in existing]
        if not pending:
            return 0

        if not source.enrich:
            inserted = 0
            for r in pending:
                inserted += self._insert(ctx.conn, source, r, item=None)
            ctx.conn.commit()
            return inserted

        api_key = ctx.api_key()
        db_lock = ctx.db_lock
        inserted = 0

        def work(r: csvio.CsvRow) -> dict:
            return cached_structured(
                conn=ctx.conn,
                db_lock=db_lock,
                prompt=self._prompt(source, r),
                schema_name="gloss",
                schema=SCHEMA_GLOSS,
                api_key=api_key,
            )

        for r, result in run_pool(
            pending, work,
            workers=ctx.workers,
            label=f"gloss/{source.id}",
            describe=lambda r: r.italian,
        ):
            if isinstance(result, Exception):
                continue
            with db_lock:
                inserted += self._insert(ctx.conn, source, r, item=result)
                ctx.conn.commit()
        return inserted

    # ── Materialise ───────────────────────────────────────────────────────
    def materialise(self, source: Source, ctx) -> int:
        sp = str(source.path.resolve())
        rows = ctx.conn.execute(
            """
            SELECT id, natural_id, italian, english
            FROM entries
            WHERE source_path = ?
            ORDER BY rowid
            """,
            (sp,),
        ).fetchall()
        if not rows:
            return 0

        default_label = f"type: {self._type_label(source)}"
        labels = source.front_pill or default_label
        if source.label_pill:
            labels = f"{labels} | {source.label_pill}"

        inserted = 0
        for r in rows:
            image_text = r["italian"] if source.image else None
            audio_text = r["italian"] if source.audio else None
            inserted += ctx.add_card_pair(
                entry_id=r["id"],
                natural_key=f"gloss:{r['id']}",
                deck=source.deck,
                front_text=r["english"],
                back_highlight=r["italian"],
                back_text=None,
                front_labels=labels,
                audio_text=audio_text,
                image_text=image_text,
            )
        return inserted

    # ── Helpers ───────────────────────────────────────────────────────────
    def _type_label(self, source: Source) -> str:
        stem = source.path.stem
        for prefix in ("italian_", "italian-", "it_", "it-"):
            if stem.lower().startswith(prefix):
                stem = stem[len(prefix):]
        return stem.replace("_", " ").rstrip("s") or "item"

    def _existing(self, conn: sqlite3.Connection, source_path: str) -> set[str]:
        rows = conn.execute(
            "SELECT natural_id FROM entries WHERE source_path = ?",
            (source_path,),
        ).fetchall()
        return {r["natural_id"] for r in rows}

    def _prompt(self, source: Source, r: csvio.CsvRow) -> str:
        payload = json.dumps(
            {"italian": r.italian, "english_hint": r.english},
            ensure_ascii=False,
        )
        hint = source.prompt_hint or "Italian vocabulary entry."
        return (
            "You are enriching an Italian flashcard entry.\n\n"
            f"Context: {hint}\n\n"
            "Rules:\n"
            "  - english: concise, natural gloss for the flashcard back face. "
            "Preserve punctuation for exclamations.\n"
            "  - disambiguation: short parenthetical clarifier only when the "
            "Italian could be confused with another common word. Empty string "
            "otherwise.\n"
            "  - usage_note: very short label only if archaic, formal, vulgar, "
            "literary, regional, or colloquial. Empty string for ordinary "
            "modern usage.\n"
            "  - confidence: 0–1.\n"
            "  - valid=false only if the entry is clearly erroneous.\n\n"
            f"Item:\n{payload}"
        )

    def _insert(
        self,
        conn: sqlite3.Connection,
        source: Source,
        r: csvio.CsvRow,
        *,
        item: dict | None,
    ) -> int:
        if item is None:
            english = r.english
            confidence = 1.0
        elif not item.get("valid", True):
            return 0
        else:
            english = merge_english(item) or r.english
            confidence = float(item.get("confidence", 1.0))
        if not english:
            return 0

        sp = str(source.path.resolve())
        eid = entry_id(sp, r.italian)
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO entries (
                id, source_path, natural_id, mode, deck, italian, english,
                confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (eid, sp, r.italian, source.mode, source.deck, r.italian, english, confidence),
        )
        return cursor.rowcount
