"""``noun`` mode — full noun-phrase pipeline.

Per row:
  1. AI lookup of canonical singular/plural, gender, articles → entry.
  2. AI generation of definite phrases (always) + one randomly-picked extra
     phrase family (articulated preposition / demonstrative / possessive /
     indefinite) → noun_phrases.

Materialise:
  Definite phrases → ``source.deck``; everything else → ``source.phrases_deck``.
"""

from __future__ import annotations

import hashlib
import json

from .. import csvio
from ..grammar import NOUN_PHRASE_OPTIONS
from ..openrouter import (
    SCHEMA_NOUN,
    SCHEMA_NOUN_PHRASES,
    cached_structured,
    merge_english,
)
from ..pool import run_pool
from ..sources import Source
from ..util import entry_id


def _select_phrase(singular: str, plural: str) -> tuple[str, str]:
    combined = f"{singular}|{plural}"
    digest = hashlib.md5(combined.encode("utf-8")).hexdigest()
    idx = int(digest[:8], 16) % len(NOUN_PHRASE_OPTIONS)
    return NOUN_PHRASE_OPTIONS[idx]


class NounMode:
    name = "noun"

    def ingest(self, source: Source, ctx) -> int:
        rows = csvio.read(source.path)
        if not rows:
            return 0
        return self._ingest_rows(source, rows, ctx)

    def _ingest_rows(self, source, rows: list, ctx) -> int:
        sp = str(source.path.resolve())
        # natural_id is stored as the lowercased lemma; see _insert_entry.
        existing = {
            r["natural_id"]
            for r in ctx.conn.execute(
                "SELECT natural_id FROM entries WHERE source_path = ?", (sp,)
            ).fetchall()
        }
        pending = [r for r in rows if r.italian.strip().lower() not in existing]
        inserted_entries = 0

        if pending:
            api_key = ctx.api_key()
            db_lock = ctx.db_lock

            def meta_work(r: csvio.CsvRow) -> dict:
                return cached_structured(
                    conn=ctx.conn,
                    db_lock=db_lock,
                    prompt=self._meta_prompt(source, r),
                    schema_name="noun_meta",
                    schema=SCHEMA_NOUN,
                    api_key=api_key,
                )

            for r, result in run_pool(
                pending, meta_work,
                workers=ctx.workers,
                label=f"noun-meta/{source.id}",
                describe=lambda r: r.italian,
            ):
                if isinstance(result, Exception):
                    continue
                with db_lock:
                    inserted_entries += self._insert_entry(ctx.conn, source, r, item=result)
                    ctx.conn.commit()

        # Second pass: fill noun_phrases for any entry without rows. This
        # always runs (idempotent) so a previous failed AI run can resume.
        self._fill_phrases(source, ctx)
        return inserted_entries

    def _fill_phrases(self, source: Source, ctx) -> None:
        sp = str(source.path.resolve())
        entries = ctx.conn.execute(
            """
            SELECT e.id, e.italian, e.english, e.singular, e.plural, e.gender,
                   e.definite_singular, e.definite_plural, e.indefinite_singular
            FROM entries e
            WHERE e.source_path = ?
              AND e.singular IS NOT NULL AND e.singular != ''
              AND e.definite_singular IS NOT NULL AND e.definite_singular != ''
              AND NOT EXISTS (SELECT 1 FROM noun_phrases p WHERE p.entry_id = e.id)
            """,
            (sp,),
        ).fetchall()
        if not entries:
            return
        api_key = ctx.api_key()
        db_lock = ctx.db_lock

        def phrase_work(row) -> dict:
            return cached_structured(
                conn=ctx.conn,
                db_lock=db_lock,
                prompt=self._phrases_prompt(row),
                schema_name="noun_phrases",
                schema=SCHEMA_NOUN_PHRASES,
                api_key=api_key,
                timeout=90,
            )

        for row, result in run_pool(
            list(entries), phrase_work,
            workers=ctx.workers,
            label=f"noun-phrases/{source.id}",
            describe=lambda r: r["italian"],
        ):
            if isinstance(result, Exception):
                continue
            with db_lock:
                self._insert_phrases(ctx.conn, row["id"], result)
                ctx.conn.commit()

    # ── Materialise ───────────────────────────────────────────────────────
    def materialise(self, source: Source, ctx) -> int:
        sp = str(source.path.resolve())
        rows = ctx.conn.execute(
            """
            SELECT p.id AS phrase_id, p.entry_id, p.phrase_type, p.number,
                   p.preposition, p.italian, p.english, p.labels, e.singular
            FROM noun_phrases p
            JOIN entries e ON p.entry_id = e.id
            WHERE e.source_path = ?
            ORDER BY e.rowid, p.phrase_type, p.number
            """,
            (sp,),
        ).fetchall()
        inserted = 0
        for r in rows:
            deck = source.deck if r["phrase_type"] == "definite" else (
                source.phrases_deck or source.deck
            )
            labels = r["labels"]
            if source.label_pill:
                labels = f"{labels} | {source.label_pill}" if labels else source.label_pill
            inserted += ctx.add_card_pair(
                entry_id=r["entry_id"],
                natural_key=f"noun_phrase:{r['phrase_id']}",
                deck=deck,
                front_text=r["english"],
                back_highlight=r["italian"],
                back_text=None,
                front_labels=labels,
                audio_text=r["italian"] if source.audio else None,
                image_text=(r["singular"] if source.image else None),
            )
        return inserted

    # ── Helpers ───────────────────────────────────────────────────────────
    def _meta_prompt(self, source: Source, r: csvio.CsvRow) -> str:
        hint = source.prompt_hint or "Italian noun."
        payload = json.dumps({"italian": r.italian, "english_hint": r.english}, ensure_ascii=False)
        return (
            "Enrich an Italian noun entry.\n\n"
            f"Context: {hint}\n\n"
            "Rules:\n"
            "  - lemma and singular: canonical singular (lowercase).\n"
            "  - singular_english / plural_english: bare translations ('house' / 'houses').\n"
            "  - english: concise gloss, usually without article.\n"
            "  - definite_singular ∈ {il, lo, l', la, ''}.\n"
            "  - definite_plural ∈ {i, gli, le, ''}.\n"
            "  - indefinite_singular ∈ {un, uno, una, un', ''}.\n"
            "  - valid=false only if clearly not a real Italian noun.\n\n"
            f"Item:\n{payload}"
        )

    def _phrases_prompt(self, e) -> str:
        phrase_type, phrase_key = _select_phrase(e["singular"] or "", e["plural"] or "")
        has_plural = bool(e["plural"])
        phrases_needed = ["1. definite singular — e.g. 'il cane', 'la casa'"]
        if has_plural:
            phrases_needed.append("2. definite plural — e.g. 'i cani', 'le case'")
        if phrase_type == "indefinite":
            phrases_needed.append("3. indefinite singular — e.g. 'un cane', 'una casa'")
            if has_plural:
                phrases_needed.append("4. indefinite plural — e.g. 'dei cani', 'delle case'")
        elif phrase_type == "articulated_preposition":
            phrases_needed.append(f"3. articulated preposition '{phrase_key}' singular")
            if has_plural:
                phrases_needed.append(f"4. articulated preposition '{phrase_key}' plural")
        elif phrase_type == "demonstrative":
            phrases_needed.append(f"3. demonstrative '{phrase_key}' singular")
            if has_plural:
                phrases_needed.append(f"4. demonstrative '{phrase_key}' plural")
        elif phrase_type == "possessive":
            phrases_needed.append(f"3. possessive '{phrase_key}' singular")
            if has_plural:
                phrases_needed.append(f"4. possessive '{phrase_key}' plural")
        payload = json.dumps(
            {
                "lemma": e["italian"],
                "english": e["english"],
                "singular": e["singular"],
                "plural": e["plural"] if has_plural else None,
                "gender": e["gender"] or "unknown",
                "definite_singular": e["definite_singular"],
                "definite_plural": e["definite_plural"] if has_plural else None,
                "indefinite_singular": e["indefinite_singular"],
            },
            ensure_ascii=False,
        )
        return (
            "Generate Italian noun phrases for a flashcard database.\n"
            f"Build EXACTLY these {len(phrases_needed)} phrase(s):\n\n"
            + "\n".join(phrases_needed)
            + "\n\nRules:\n"
            "  - Use correct articles for the noun's gender / starting sound.\n"
            "  - For nouns with no plural, only generate singular phrases.\n"
            "  - definite: 'the …'; indefinite: 'a/an …' or 'some …'.\n"
            "  - articulated_preposition: natural prepositional phrase.\n"
            "  - demonstrative: 'this/these …' for questo, 'that/those …' for quello.\n"
            "  - possessive: my / your / his / her / our / your (pl) / their.\n"
            "  - labels: pipe-separated, e.g. 'phrase: definite | number: singular'.\n"
            "  - usage_note: empty unless archaic/formal/vulgar/literary/regional.\n\n"
            f"Input noun:\n{payload}"
        )

    def _insert_entry(self, conn, source: Source, r: csvio.CsvRow, *, item: dict) -> int:
        if not item.get("valid", True):
            return 0
        lemma = (item["lemma"] or "").strip().lower() or r.italian.strip().lower()
        singular = (item["singular"] or "").strip().lower() or lemma
        if not lemma:
            return 0
        english = merge_english(item) or r.english
        if not english:
            return 0
        sp = str(source.path.resolve())
        eid = entry_id(sp, lemma)
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO entries (
                id, source_path, natural_id, mode, deck, italian, english,
                confidence, singular, plural, gender,
                definite_singular, definite_plural, indefinite_singular
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                eid, sp, lemma, source.mode, source.deck, lemma, english,
                float(item.get("confidence", 1.0)), singular,
                (item["plural"] or "").strip().lower(),
                item["gender"],
                (item["definite_singular"] or "").strip().lower(),
                (item["definite_plural"] or "").strip().lower(),
                (item["indefinite_singular"] or "").strip().lower(),
            ),
        )
        return cursor.rowcount

    def _insert_phrases(self, conn, entry_id_value: str, result: dict) -> int:
        inserted = 0
        for phrase in result.get("phrases", []):
            italian = (phrase.get("italian") or "").strip()
            english = (phrase.get("english") or "").strip()
            if not italian or not english:
                continue
            usage_note = (phrase.get("usage_note") or "").strip()
            if usage_note:
                english = f"{english} [{usage_note}]"
            preposition = (phrase.get("preposition") or "").strip() or None
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO noun_phrases (
                    entry_id, phrase_type, number, preposition, italian, english, labels
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id_value, phrase["phrase_type"], phrase["number"],
                    preposition, italian, english, (phrase.get("labels") or "").strip(),
                ),
            )
            inserted += cursor.rowcount
        return inserted
