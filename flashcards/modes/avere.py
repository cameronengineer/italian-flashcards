"""``avere`` mode — fan one ``avere`` expression out into N conjugated cards.

Each ingested row produces 2 person-cards (chosen deterministically by the
entry's id so each expression always picks the same persons).
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Iterable

from ..grammar import (
    AVERE_CONJ,
    AVERE_PERSONS,
    AVERE_SUBJ_EN as SUBJ_EN,
)
from ..cards import Card, labels
from ..sources import Source
from ..tasks import AVERE_GLOSS
from .gloss import GlossMode


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


class AvereMode(GlossMode):
    """Same ingest as ``gloss`` (one entry per CSV row, optional AI gloss);
    only the enrichment task and the card fan-out differ."""

    name = "avere"
    meta = AVERE_GLOSS
    default_hint = (
        "Fixed Italian phrases using avere + noun where English uses 'to be + "
        "adjective' or another verb (e.g. 'avere fame' = 'to be hungry')."
    )

    def cards(self, source: Source, conn: sqlite3.Connection) -> Iterable[Card]:
        # NOTE: ``sources.validate()`` pre-flights this value; the runtime
        # check is defense-in-depth for programmatic use.
        cards_per_expression = int(
            source.extras.get("cards_per_expression", DEFAULT_CARDS_PER_EXPRESSION)
        )
        if cards_per_expression < 1 or cards_per_expression > len(AVERE_PERSONS):
            raise ValueError(
                f"avere source {source.id!r}: cards_per_expression must be in "
                f"1..{len(AVERE_PERSONS)}, got {cards_per_expression}"
            )
        for e in self.live_entries(conn, source):
            for person in _pick_persons(e["id"], cards_per_expression):
                italian = _avere_italian(person, e["italian"])
                yield Card(
                    entry_id=e["id"],
                    natural_key=f"avere:{e['id']}:{person}",
                    deck=source.deck,
                    english=_avere_english(person, e["english"]),
                    italian=italian,
                    labels=labels(type="avere expression", subject=person, extra=source.label_pill),
                    details=e["italian"],
                    audio_text=italian,
                    image_text=e["italian"],
                    fact_word=e["italian"],
                )
