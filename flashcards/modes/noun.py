"""``noun`` mode — full noun-phrase pipeline.

Ingest: CSV row → AI metadata (gender, singular/plural, articles) →
``entries``; then definite phrases (always) plus one extra phrase family
picked deterministically per noun (indefinite, articulated preposition,
demonstrative or possessive) → ``noun_phrases``.

Cards: definite phrases → ``deck``; everything else → ``phrases_deck``.
The fun fact goes on the definite singular card.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Iterable

from .. import csvio
from ..cards import Card, join_details, labels, with_article
from ..grammar import NOUN_PHRASE_OPTIONS
from ..sources import Source
from ..tasks import NOUN_META, NOUN_PHRASES
from ..util import clean_text
from .base import Mode
from .verb import _lemma

GENDER_LABEL = {"masculine": "masculine", "feminine": "feminine", "both": "masculine / feminine"}


def _select_phrase(singular: str, plural: str) -> tuple[str, str]:
    combined = f"{singular}|{plural}"
    digest = hashlib.md5(combined.encode("utf-8")).hexdigest()
    idx = int(digest[:8], 16) % len(NOUN_PHRASE_OPTIONS)
    return NOUN_PHRASE_OPTIONS[idx]


class NounMode(Mode):
    name = "noun"
    meta = NOUN_META
    default_hint = "Italian noun."

    # ── Ingest hooks ──────────────────────────────────────────────────────
    def row_key(self, row: csvio.CsvRow) -> str:
        return row.italian.strip().lower()

    def resolve(self, row, result) -> str | None:
        return _lemma(result, row) if result is not None else self.row_key(row)

    def columns(self, row, result, natural_id: str) -> dict:
        singular = clean_text(result["singular"], natural_id).lower() or natural_id
        return {
            "singular": singular,
            "plural": clean_text(result["plural"], singular).lower(),
            "gender": result["gender"],
            "definite_singular": clean_text(result["definite_singular"]).lower(),
            "definite_plural": clean_text(result["definite_plural"]).lower(),
            "indefinite_singular": clean_text(result["indefinite_singular"]).lower(),
        }

    def children(self, source: Source, ctx) -> None:
        """Generate phrases for every live noun that has none yet."""
        entries = ctx.conn.execute(
            """
            SELECT e.id, e.italian, e.english, e.singular, e.plural, e.gender,
                   e.definite_singular, e.definite_plural, e.indefinite_singular
            FROM entries e
            WHERE e.source_path = ? AND e.mode = 'noun' AND e.retired = 0
              AND e.singular IS NOT NULL AND e.singular != ''
              AND e.definite_singular IS NOT NULL AND e.definite_singular != ''
              AND NOT EXISTS (SELECT 1 FROM noun_phrases p WHERE p.entry_id = e.id)
            """,
            (source.key,),
        ).fetchall()
        self.generate(
            ctx,
            label=f"noun-phrases/{source.id}",
            items=list(entries),
            make_task=self._phrases_task,
            store=lambda conn, e, result: self._insert_phrases(conn, e["id"], result),
            describe=lambda e: e["italian"],
        )

    def _phrases_task(self, e):
        phrase_type, key = _select_phrase(e["singular"] or "", e["plural"] or "")
        has_plural = bool(e["plural"])
        needed = ["definite singular (e.g. 'il cane', 'la casa')"]
        if has_plural:
            needed.append("definite plural (e.g. 'i cani', 'le case')")
        extra = {
            "indefinite": "indefinite {n} (e.g. 'un cane' / 'dei cani')",
            "articulated_preposition": f"articulated preposition '{key}' {{n}}",
            "demonstrative": f"demonstrative '{key}' {{n}}",
            "possessive": f"possessive '{key}' {{n}}",
        }[phrase_type]
        needed.append(extra.format(n="singular"))
        if has_plural:
            needed.append(extra.format(n="plural"))
        spec = f"Build exactly these {len(needed)} phrase(s):\n" + "\n".join(
            f"      {i}. {p}" for i, p in enumerate(needed, start=1)
        )
        return NOUN_PHRASES.task(
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
            rules=(spec,),
        )

    def _insert_phrases(self, conn, entry_id_value: str, result: dict) -> int:
        inserted = 0
        for phrase in result.get("phrases", []):
            italian = clean_text(phrase.get("italian"))
            english = clean_text(phrase.get("english"))
            if not italian or not english:
                continue
            usage_note = clean_text(phrase.get("usage_note"))
            if usage_note:
                english = f"{english} [{usage_note}]"
            preposition = clean_text(phrase.get("preposition")) or None
            card_key = (
                f"noun_phrase:{entry_id_value}:{phrase['phrase_type']}:"
                f"{phrase['number']}:{preposition or ''}"
            )
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO noun_phrases (
                    entry_id, phrase_type, number, preposition, italian, english, card_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (entry_id_value, phrase["phrase_type"], phrase["number"],
                 preposition, italian, english, card_key),
            )
            inserted += cursor.rowcount
        return inserted

    # ── Cards ─────────────────────────────────────────────────────────────
    def cards(self, source: Source, conn: sqlite3.Connection) -> Iterable[Card]:
        for r in conn.execute(
            """
            SELECT p.card_key, p.entry_id, p.phrase_type, p.number, p.preposition,
                   p.italian, p.english, e.singular, e.gender, e.definite_singular
            FROM noun_phrases p
            JOIN entries e ON p.entry_id = e.id
            WHERE e.source_path = ? AND e.mode = 'noun' AND e.retired = 0
            ORDER BY e.rowid, p.phrase_type, p.number
            """,
            (source.key,),
        ):
            definite = r["phrase_type"] == "definite"
            base = with_article(r["definite_singular"], r["singular"])
            is_base_card = definite and r["number"] == "singular"
            yield Card(
                entry_id=r["entry_id"],
                natural_key=r["card_key"],
                deck=source.deck if definite else (source.phrases_deck or source.deck),
                english=r["english"],
                italian=r["italian"],
                labels=labels(type="noun", phrase=r["phrase_type"], preposition=r["preposition"],
                              number=r["number"], extra=source.label_pill),
                details=join_details(
                    GENDER_LABEL.get(r["gender"] or ""),
                    None if is_base_card else base,
                ),
                audio_text=r["italian"],
                image_text=r["singular"],
                fact_word=r["singular"] if is_base_card else None,
            )
