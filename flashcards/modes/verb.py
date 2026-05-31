"""``verb`` mode — full verb pipeline.

Ingest:
  CSV row → AI lookup (lemma, aux, past participle, reflexive) → ``entries``
  row. Then a second AI call generates the 22 conjugated forms which go into
  ``verb_forms``.

Materialise:
  One Anki card per ``verb_forms`` row in the appropriate tense deck, plus one
  infinitive card in ``source.infinitive_deck``.
"""

from __future__ import annotations

import json

from .. import csvio
from ..grammar import TENSE_DISPLAY
from ..openrouter import (
    SCHEMA_VERB,
    SCHEMA_VERB_FORMS,
    cached_structured,
    merge_english,
)
from ..pool import run_pool
from ..sources import Source
from ..util import entry_id


class VerbMode:
    name = "verb"

    # ── Ingest ────────────────────────────────────────────────────────────
    def ingest(self, source: Source, ctx) -> int:
        rows = csvio.read(source.path)
        if not rows:
            return 0
        return self._ingest_rows(source, rows, ctx)

    def _ingest_rows(self, source, rows: list, ctx) -> int:
        sp = str(source.path.resolve())
        # natural_id is stored as the lowercased lemma (see _insert_entry below).
        # Normalise the pending key to match, otherwise rows whose CSV casing
        # differs from the resolved lemma re-fire AI requests on resume.
        existing = {
            r["natural_id"]
            for r in ctx.conn.execute(
                "SELECT natural_id FROM entries WHERE source_path = ?", (sp,)
            ).fetchall()
        }
        pending = [r for r in rows if r.italian.strip().lower() not in existing]
        if not pending:
            return 0

        api_key = ctx.api_key()
        db_lock = ctx.db_lock

        def work(r: csvio.CsvRow) -> dict:
            return cached_structured(
                conn=ctx.conn,
                db_lock=db_lock,
                prompt=self._meta_prompt(source, r),
                schema_name="verb_meta",
                schema=SCHEMA_VERB,
                api_key=api_key,
            )

        inserted = 0
        for r, result in run_pool(
            pending, work,
            workers=ctx.workers,
            label=f"verb-meta/{source.id}",
            describe=lambda r: r.italian,
        ):
            if isinstance(result, Exception):
                continue
            with db_lock:
                inserted += self._insert_entry(ctx.conn, source, r, item=result)
                ctx.conn.commit()

        # Second pass: fill verb_forms for any entry without rows.
        self._fill_forms(source, ctx)
        return inserted

    def _fill_forms(self, source: Source, ctx) -> None:
        sp = str(source.path.resolve())
        entries = ctx.conn.execute(
            """
            SELECT e.id, e.italian, e.english, e.infinitive, e.auxiliary,
                   e.past_participle, e.is_reflexive
            FROM entries e
            WHERE e.source_path = ?
              AND NOT EXISTS (SELECT 1 FROM verb_forms vf WHERE vf.entry_id = e.id)
            """,
            (sp,),
        ).fetchall()
        if not entries:
            return
        api_key = ctx.api_key()
        db_lock = ctx.db_lock

        def work(row) -> dict:
            return cached_structured(
                conn=ctx.conn,
                db_lock=db_lock,
                prompt=self._forms_prompt(row),
                schema_name="verb_forms",
                schema=SCHEMA_VERB_FORMS,
                api_key=api_key,
                timeout=90,
            )

        for row, result in run_pool(
            list(entries), work,
            workers=ctx.workers,
            label=f"verb-forms/{source.id}",
            describe=lambda r: r["italian"],
        ):
            if isinstance(result, Exception):
                continue
            with db_lock:
                self._insert_forms(ctx.conn, row["id"], result)
                ctx.conn.commit()

    # ── Materialise ───────────────────────────────────────────────────────
    def materialise(self, source: Source, ctx) -> int:
        sp = str(source.path.resolve())
        inserted = 0

        # Per-form cards
        rows = ctx.conn.execute(
            """
            SELECT vf.id AS form_id, vf.entry_id, vf.tense, vf.person,
                   vf.italian, vf.english, vf.labels, e.infinitive
            FROM verb_forms vf
            JOIN entries e ON vf.entry_id = e.id
            WHERE e.source_path = ?
            ORDER BY e.rowid, vf.tense, vf.person
            """,
            (sp,),
        ).fetchall()
        for r in rows:
            deck = f"{source.deck} {TENSE_DISPLAY.get(r['tense'], r['tense'])}"
            labels = r["labels"]
            if source.label_pill:
                labels = f"{labels} | {source.label_pill}" if labels else source.label_pill
            inserted += ctx.add_card_pair(
                entry_id=r["entry_id"],
                natural_key=f"verb_form:{r['form_id']}",
                deck=deck,
                front_text=r["english"],
                back_highlight=r["italian"],
                back_text=r["infinitive"],
                front_labels=labels,
                audio_text=r["italian"] if source.audio else None,
                image_text=(r["infinitive"] if source.image else None),
            )

        # Infinitive cards
        if source.infinitive_deck:
            infs = ctx.conn.execute(
                """
                SELECT id, infinitive, english
                FROM entries
                WHERE source_path = ?
                  AND infinitive IS NOT NULL AND infinitive != ''
                ORDER BY rowid
                """,
                (sp,),
            ).fetchall()
            for r in infs:
                labels = "tense: infinitive"
                if source.label_pill:
                    labels = f"{labels} | {source.label_pill}"
                inserted += ctx.add_card_pair(
                    entry_id=r["id"],
                    natural_key=f"verb_infinitive:{r['id']}",
                    deck=source.infinitive_deck,
                    front_text=r["english"],
                    back_highlight=r["infinitive"],
                    back_text=None,
                    front_labels=labels,
                    audio_text=r["infinitive"] if source.audio else None,
                    image_text=(r["infinitive"] if source.image else None),
                )
        return inserted

    # ── Helpers ───────────────────────────────────────────────────────────
    def _meta_prompt(self, source: Source, r: csvio.CsvRow) -> str:
        hint = source.prompt_hint or "Italian verb infinitive."
        payload = json.dumps({"italian": r.italian, "english_hint": r.english}, ensure_ascii=False)
        return (
            "Enrich an Italian verb entry.\n\n"
            f"Context: {hint}\n\n"
            "Rules:\n"
            "  - lemma and infinitive: canonical Italian infinitive (lowercase).\n"
            "  - english: concise gloss starting with 'to', e.g. 'to go'.\n"
            "  - disambiguation: short parenthetical if ambiguous; else empty.\n"
            "  - usage_note: archaic/formal/vulgar/literary/regional if relevant; else empty.\n"
            "  - auxiliary: avere | essere | both | unknown.\n"
            "  - past_participle: masculine singular, lowercase.\n"
            "  - is_reflexive: true for reflexive verbs (e.g. trasferirsi).\n"
            "  - valid=false only if not a real Italian verb.\n\n"
            f"Item:\n{payload}"
        )

    def _forms_prompt(self, e) -> str:
        payload = json.dumps(
            {
                "lemma": e["italian"],
                "english": e["english"],
                "infinitive": e["infinitive"],
                "auxiliary": e["auxiliary"],
                "past_participle": e["past_participle"],
                "is_reflexive": bool(e["is_reflexive"]),
            },
            ensure_ascii=False,
        )
        return (
            "Generate Italian verb forms for a flashcard database.\n"
            "Generate exactly these forms: presente, passato_prossimo, imperfetto "
            "for io, tu, lui_lei, noi, voi, loro; imperativo for tu, Lei, noi, voi. "
            "No io imperative.\n\n"
            "Rules:\n"
            "  - Use the supplied auxiliary and past participle for passato_prossimo.\n"
            "  - Reflexive verbs include the correct reflexive pronouns.\n"
            "  - labels: pipe-separated, e.g. 'tense: presente | subject: io'.\n"
            "  - english: natural prompt, e.g. 'we speak / we are speaking', 'Speak!'.\n"
            "  - usage_note: very short label if archaic/formal/vulgar/literary/regional; else empty.\n\n"
            f"Input verb:\n{payload}"
        )

    def _insert_entry(self, conn, source: Source, r: csvio.CsvRow, *, item: dict) -> int:
        if not item.get("valid", True):
            return 0
        lemma = (item["lemma"] or "").strip().lower() or r.italian.strip().lower()
        if not lemma:
            return 0
        infinitive = (item["infinitive"] or "").strip().lower() or lemma
        english = merge_english(item) or r.english
        if not english:
            return 0
        sp = str(source.path.resolve())
        eid = entry_id(sp, lemma)
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO entries (
                id, source_path, natural_id, mode, deck, italian, english,
                confidence, infinitive, auxiliary, past_participle, is_reflexive
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                eid, sp, lemma, source.mode, source.deck, lemma, english,
                float(item.get("confidence", 1.0)), infinitive, item["auxiliary"],
                (item["past_participle"] or "").strip().lower(),
                1 if item.get("is_reflexive") else 0,
            ),
        )
        return cursor.rowcount

    def _insert_forms(self, conn, entry_id_value: str, result: dict) -> int:
        inserted = 0
        for form in result.get("forms", []):
            italian = (form.get("italian") or "").strip()
            english = (form.get("english") or "").strip()
            if not italian or not english:
                continue
            usage_note = (form.get("usage_note") or "").strip()
            if usage_note:
                english = f"{english} [{usage_note}]"
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO verb_forms (
                    entry_id, tense, person, polarity, italian, english, labels
                ) VALUES (?, ?, ?, 'positive', ?, ?, ?)
                """,
                (
                    entry_id_value, form["tense"], form["person"],
                    italian, english, (form.get("labels") or "").strip(),
                ),
            )
            inserted += cursor.rowcount
        return inserted
