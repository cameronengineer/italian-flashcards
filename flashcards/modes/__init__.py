"""Mode plugins. Every mode subclasses :class:`~flashcards.modes.base.Mode`
and only declares its rows, enrichment task, children and cards; the shared
ingest / retirement / card flow lives in the base class.

Adding a mode: write the subclass, then register it here.
"""

from __future__ import annotations

from .base import Mode
from .avere import AvereMode
from .gloss import GlossMode
from .noun import NounMode
from .subtlex import SubtlexMode
from .verb import VerbMode

MODES: dict[str, Mode] = {
    m.name: m for m in (GlossMode(), AvereMode(), VerbMode(), NounMode(), SubtlexMode())
}


def get(name: str) -> Mode:
    try:
        return MODES[name]
    except KeyError as exc:
        raise ValueError(f"Unknown mode {name!r}. Available: {sorted(MODES)}") from exc
