"""``gloss`` mode — the default. One CSV row → one entry → one card pair.

The English side is the CSV's own gloss (``enrich: false``) or an AI gloss
with disambiguation / usage notes (``enrich: true``, the default).
"""

from __future__ import annotations

import sqlite3
from typing import Iterable

from ..cards import Card, labels
from ..sources import Source
from ..tasks import GLOSS
from .base import Mode


class GlossMode(Mode):
    name = "gloss"
    meta = GLOSS
    default_hint = "Italian vocabulary entry."

    def uses_ai(self, source: Source) -> bool:
        return source.enrich

    def type_label(self, source: Source) -> str:
        """``front_pill`` if set, else derived from the file name."""
        if source.front_pill:
            return source.front_pill
        stem = source.path.stem
        for prefix in ("italian_", "italian-", "it_", "it-"):
            if stem.lower().startswith(prefix):
                stem = stem[len(prefix):]
        return stem.replace("_", " ").rstrip("s") or "item"

    def cards(self, source: Source, conn: sqlite3.Connection) -> Iterable[Card]:
        type_label = self.type_label(source)
        for e in self.live_entries(conn, source):
            yield Card(
                entry_id=e["id"],
                natural_key=f"gloss:{e['id']}",
                deck=source.deck,
                english=e["english"],
                italian=e["italian"],
                labels=labels(type=type_label, extra=source.label_pill),
                audio_text=e["italian"],
                image_text=e["italian"],
                fact_word=e["italian"],
            )
