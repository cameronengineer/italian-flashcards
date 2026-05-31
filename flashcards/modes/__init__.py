"""Mode plugins. Each mode knows how to turn a Source into entries + downstream
tables.

The registry is intentionally tiny — adding a new mode is a one-line change
here once you've written the module.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..sources import Source


@runtime_checkable
class Mode(Protocol):
    """Minimal interface every mode plugin implements."""

    name: str

    def ingest(self, source: Source, ctx) -> int:
        """Read the source, populate ``entries`` (+ any mode-specific tables).

        ``ctx`` is a ``PipelineContext`` (see ``flashcards.commands.build``).
        Returns the number of new entries added.
        """
        ...

    def materialise(self, source: Source, ctx) -> int:
        """Read this source's entries and emit ``cards`` rows. Returns count."""
        ...


from .gloss import GlossMode
from .avere import AvereMode
from .verb import VerbMode
from .noun import NounMode
from .subtlex import SubtlexMode

MODES: dict[str, Mode] = {
    "gloss": GlossMode(),
    "avere": AvereMode(),
    "verb": VerbMode(),
    "noun": NounMode(),
    "subtlex": SubtlexMode(),
}


def get(name: str) -> Mode:
    try:
        return MODES[name]
    except KeyError as exc:
        raise ValueError(
            f"Unknown mode {name!r}. Available: {sorted(MODES)}"
        ) from exc
