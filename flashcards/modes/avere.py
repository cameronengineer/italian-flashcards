"""``avere`` mode — fan one ``avere`` expression out into N conjugated cards.

Each ingested row produces 2 person-cards (chosen deterministically by the
entry's id so each expression always picks the same persons).
"""

from __future__ import annotations

import hashlib
import json

from ..grammar import (
    AVERE_CONJ,
    AVERE_PERSONS,
    AVERE_SUBJ_EN as SUBJ_EN,
    AVERE_SUBJ_LABEL as SUBJ_LABEL,
)
from ..openrouter import SCHEMA_GLOSS, cached_structured, merge_english
from ..pool import run_pool
from .. import csvio
from ..sources import Source
from ..util import entry_id


DEFAULT_CARDS_PER_EXPRESSION = 2


def _pick_persons(eid: str, count: int) -> list[str]:
    digest = hashlib.md5(eid.encode("utf-8")).digest()
    chosen: list[str] = []
    used: set[int] = set()
    for byte in digest:
        idx = byte % len(AVERE_PERSONS)
        if idx not in used:
            chosen.append(AVERE_PERSONS[idx])
            used.add(idx)
        if len(chosen) == count:
            break
    for i, person in enumerate(AVERE_PERSONS):
        if len(chosen) == count:
            break
        if i not in used:
            chosen.append(person)
            used.add(i)
    return chosen


def _avere_italian(person: str, expression: str) -> str:
    conj = AVERE_CONJ[person]
    expr = expression.strip()
    lower = expr.lower()
    if lower.startswith("non avere "):
        return f"non {conj} {expr[len('non avere '):]}"
    if lower.startswith("avere "):
        return f"{conj} {expr[len('avere '):]}"
    return f"{conj} {expr}"


def _conj_english(person: str, phrase: str) -> str:
    phrase = phrase.strip()
    if phrase == "be" or phrase.startswith("be "):
        rest = phrase[3:] if phrase.startswith("be ") else ""
        if person == "io":
            return f"am {rest}".strip()
        if person == "lui_lei":
            return f"is {rest}".strip()
        return f"are {rest}".strip()
    if phrase == "not be" or phrase.startswith("not be "):
        rest = phrase[7:] if phrase.startswith("not be ") else ""
        if person == "io":
            return f"am not {rest}".strip()
        if person == "lui_lei":
            return f"is not {rest}".strip()
        return f"are not {rest}".strip()
    if phrase.startswith("not "):
        rest = phrase[4:]
        return f"doesn't {rest}" if person == "lui_lei" else f"don't {rest}"
    words = phrase.split(" ", 1)
    first, remainder = words[0], (" " + words[1]) if len(words) > 1 else ""
    if person == "lui_lei":
        if first == "have":
            conj = "has"
        elif first.endswith(("s", "x", "z")):
            conj = first + "es"
        elif first.endswith("y") and len(first) > 1 and first[-2] not in "aeiou":
            conj = first[:-1] + "ies"
        else:
            conj = first + "s"
    else:
        conj = first
    return conj + remainder


def _avere_english(person: str, base_english: str) -> str:
    subject = SUBJ_EN[person]
    eng = base_english.strip()
    if not eng.lower().startswith("to "):
        return f"{subject}: {eng}"
    alternatives = [p.strip() for p in eng[3:].split(" / ")]
    parts: list[str] = []
    for alt in alternatives:
        if alt.lower().startswith("to "):
            alt = alt[3:]
        parts.append(_conj_english(person, alt))
    return f"{subject} {' / '.join(parts)}"


class AvereMode:
    name = "avere"

    def ingest(self, source: Source, ctx) -> int:
        rows = csvio.read(source.path)
        if not rows:
            return 0

        sp = str(source.path.resolve())
        existing = {
            r["natural_id"] for r in ctx.conn.execute(
                "SELECT natural_id FROM entries WHERE source_path = ?", (sp,)
            ).fetchall()
        }
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
                schema_name="avere_gloss",
                schema=SCHEMA_GLOSS,
                api_key=api_key,
            )

        for r, result in run_pool(
            pending, work,
            workers=ctx.workers,
            label=f"avere/{source.id}",
            describe=lambda r: r.italian,
        ):
            if isinstance(result, Exception):
                continue
            with db_lock:
                inserted += self._insert(ctx.conn, source, r, item=result)
                ctx.conn.commit()
        return inserted

    def materialise(self, source: Source, ctx) -> int:
        sp = str(source.path.resolve())
        rows = ctx.conn.execute(
            """
            SELECT id, italian, english
            FROM entries
            WHERE source_path = ?
            ORDER BY rowid
            """,
            (sp,),
        ).fetchall()
        # NOTE: ``sources.validate()`` pre-flights this value at discover/build
        # time; the runtime check below is defense-in-depth so direct
        # programmatic use of ``AvereMode.materialise`` still fails loudly on
        # a bad config rather than producing wrong-shaped output. Keep both.
        cards_per_expression = int(
            source.extras.get("cards_per_expression", DEFAULT_CARDS_PER_EXPRESSION)
        )
        if cards_per_expression < 1 or cards_per_expression > len(AVERE_PERSONS):
            raise ValueError(
                f"avere source {source.id!r}: cards_per_expression must be in "
                f"1..{len(AVERE_PERSONS)}, got {cards_per_expression}"
            )
        inserted = 0
        for r in rows:
            for person in _pick_persons(r["id"], cards_per_expression):
                italian = _avere_italian(person, r["italian"])
                english = _avere_english(person, r["english"])
                base_labels = f"type: avere expression | subject: {SUBJ_LABEL[person]}"
                if source.label_pill:
                    base_labels = f"{base_labels} | {source.label_pill}"
                inserted += ctx.add_card_pair(
                    entry_id=r["id"],
                    natural_key=f"avere:{r['id']}:{person}",
                    deck=source.deck,
                    front_text=english,
                    back_highlight=italian,
                    back_text=None,
                    front_labels=base_labels,
                    audio_text=italian if source.audio else None,
                    image_text=(r["italian"] if source.image else None),
                )
        return inserted

    def _prompt(self, source: Source, r: csvio.CsvRow) -> str:
        hint = source.prompt_hint or (
            "Italian 'avere' expression where English uses 'to be + adjective' "
            "or another verb (e.g. 'avere fame' = 'to be hungry')."
        )
        payload = json.dumps({"italian": r.italian, "english_hint": r.english}, ensure_ascii=False)
        return (
            "Enrich an Italian 'avere' expression.\n\n"
            f"Context: {hint}\n\n"
            "Rules:\n"
            "  - english: concise base form starting with 'to', e.g. 'to be hungry'.\n"
            "  - disambiguation: empty unless ambiguous.\n"
            "  - usage_note: very short label only if archaic/formal/vulgar/regional.\n"
            "  - valid=false only if clearly not an avere expression.\n\n"
            f"Item:\n{payload}"
        )

    def _insert(self, conn, source, r: csvio.CsvRow, *, item: dict | None) -> int:
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
                id, source_path, natural_id, mode, deck, italian, english, confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (eid, sp, r.italian, source.mode, source.deck, r.italian, english, confidence),
        )
        return cursor.rowcount
