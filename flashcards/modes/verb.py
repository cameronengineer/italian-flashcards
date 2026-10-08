"""``verb`` mode — full verb pipeline.

Ingest: CSV row → AI metadata (lemma, auxiliary, past participle,
reflexive) → ``entries``; then the conjugated forms of every tense in
``grammar.TENSES`` → ``verb_forms`` (missing (tense, person) pairs are
backfilled on later runs).

Cards: one per form in its tense deck (``<deck> <Tense>``), plus one
infinitive card in ``infinitive_deck``. The fun fact goes on the infinitive.
"""

from __future__ import annotations

import sqlite3
from typing import Iterable

from .. import csvio
from ..cards import Card, join_details, labels
from ..grammar import TENSE_DISPLAY, TENSE_PERSONS, TENSES
from ..sources import Source
from ..tasks import VERB_FORMS, VERB_META
from ..util import clean_text
from .base import Mode


def _lemma(item: dict, r: csvio.CsvRow) -> str | None:
    """Natural id for an AI-resolved verb/noun row, or None if invalid.

    Corrupted AI output (control characters) falls back to the input word.
    """
    if not item.get("valid", True):
        return None
    fallback = r.italian.strip().lower()
    return clean_text(item.get("lemma"), fallback).lower() or fallback or None


class VerbMode(Mode):
    name = "verb"
    meta = VERB_META
    default_hint = "Italian verb infinitive."

    # ── Ingest hooks ──────────────────────────────────────────────────────
    def row_key(self, row: csvio.CsvRow) -> str:
        # natural_id is the lowercased lemma; match rows on the same form so
        # resumed runs don't re-ask the AI.
        return row.italian.strip().lower()

    def resolve(self, row, result) -> str | None:
        return _lemma(result, row) if result is not None else self.row_key(row)

    def columns(self, row, result, natural_id: str) -> dict:
        return {
            "infinitive": clean_text(result["infinitive"], natural_id).lower() or natural_id,
            "auxiliary": result["auxiliary"],
            "past_participle": clean_text(result["past_participle"]).lower(),
            "is_reflexive": 1 if result.get("is_reflexive") else 0,
        }

    def children(self, source: Source, ctx) -> None:
        """Backfill every (entry, tense) missing any of its persons.

        Only the missing tenses are requested, so adding a tense to
        ``TENSES`` conjugates it for existing verbs without paying for the
        stored ones. Filtered on ``mode = 'verb'``: SUBTLEX puts verbs and
        nouns under one source_path.
        """
        rows = ctx.conn.execute(
            """
            SELECT id, italian, english, infinitive, auxiliary, past_participle, is_reflexive
            FROM entries WHERE source_path = ? AND mode = 'verb' AND retired = 0
            """,
            (source.key,),
        ).fetchall()
        have: dict[str, set[tuple[str, str]]] = {}
        for f in ctx.conn.execute(
            """
            SELECT vf.entry_id, vf.tense, vf.person FROM verb_forms vf
            JOIN entries e ON e.id = vf.entry_id
            WHERE e.source_path = ? AND e.mode = 'verb'
            """,
            (source.key,),
        ):
            have.setdefault(f["entry_id"], set()).add((f["tense"], f["person"]))
        targets = []
        for row in rows:
            got = have.get(row["id"], set())
            missing = [t for t in TENSES if any((t, p) not in got for p in TENSE_PERSONS[t])]
            if missing:
                targets.append((row, missing))
        self.generate(
            ctx,
            label=f"verb-forms/{source.id}",
            items=targets,
            make_task=self._forms_task,
            store=lambda conn, item, result: self._insert_forms(conn, item[0]["id"], result),
            describe=lambda p: p[0]["italian"],
        )

    def _forms_task(self, target):
        e, tenses = target
        spec = "Generate exactly these (tense: persons) pairs:\n" + "\n".join(
            f"      {t}: {', '.join(TENSE_PERSONS[t])}" for t in tenses
        )
        return VERB_FORMS.task(
            {
                "lemma": e["italian"],
                "english": e["english"],
                "infinitive": e["infinitive"],
                "auxiliary": e["auxiliary"],
                "past_participle": e["past_participle"],
                "is_reflexive": bool(e["is_reflexive"]),
            },
            rules=(spec,),
        )

    def _insert_forms(self, conn, entry_id_value: str, result: dict) -> int:
        inserted = 0
        for form in result.get("forms", []):
            italian = clean_text(form.get("italian"))
            english = clean_text(form.get("english"))
            if not italian or not english:
                continue
            usage_note = clean_text(form.get("usage_note"))
            if usage_note:
                english = f"{english} [{usage_note}]"
            card_key = f"verb_form:{entry_id_value}:{form['tense']}:{form['person']}:positive"
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO verb_forms (
                    entry_id, tense, person, polarity, italian, english, card_key
                ) VALUES (?, ?, ?, 'positive', ?, ?, ?)
                """,
                (entry_id_value, form["tense"], form["person"], italian, english, card_key),
            )
            inserted += cursor.rowcount
        return inserted

    # ── Cards ─────────────────────────────────────────────────────────────
    def cards(self, source: Source, conn: sqlite3.Connection) -> Iterable[Card]:
        for r in conn.execute(
            """
            SELECT vf.card_key, vf.entry_id, vf.tense, vf.person,
                   vf.italian, vf.english, e.infinitive
            FROM verb_forms vf
            JOIN entries e ON vf.entry_id = e.id
            WHERE e.source_path = ? AND e.mode = 'verb' AND e.retired = 0
            ORDER BY e.rowid, vf.tense, vf.person
            """,
            (source.key,),
        ):
            yield Card(
                entry_id=r["entry_id"],
                natural_key=r["card_key"],
                deck=f"{source.deck} {TENSE_DISPLAY.get(r['tense'], r['tense'])}",
                english=r["english"],
                italian=r["italian"],
                labels=labels(type="verb", tense=r["tense"], subject=r["person"],
                              extra=source.label_pill),
                details=r["infinitive"],
                audio_text=r["italian"],
                image_text=r["infinitive"],
            )
        if not source.infinitive_deck:
            return
        for e in self.live_entries(conn, source):
            if not e["infinitive"]:
                continue
            aux = e["auxiliary"] if e["auxiliary"] in ("avere", "essere") else (
                "avere / essere" if e["auxiliary"] == "both" else None
            )
            yield Card(
                entry_id=e["id"],
                natural_key=f"verb_infinitive:{e['id']}",
                deck=source.infinitive_deck,
                english=e["english"],
                italian=e["infinitive"],
                labels=labels(type="verb", tense="infinitive", extra=source.label_pill),
                details=join_details(
                    f"auxiliary: {aux}" if aux else None,
                    f"past participle: {e['past_participle']}" if e["past_participle"] else None,
                    "reflexive" if e["is_reflexive"] else None,
                ),
                audio_text=e["infinitive"],
                image_text=e["infinitive"],
                fact_word=e["infinitive"],
            )
