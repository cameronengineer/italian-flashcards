"""``learnt`` command — export every word you've graduated to review.

A card counts as learnt once it is a review (or relearning) card and not
suspended. Covers every deck holding pipeline notes; output is a text table
per deck plus a summary.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from ..anki import learnt_pairs
from ..util import print_banner, table

DEFAULT_OUTPUT = Path(tempfile.gettempdir()) / "italian-flashcards-learnt.txt"


def run(*, deck: str | None = None, output: Path | None = None) -> int:
    print_banner("learnt — export learnt words")
    output = output or DEFAULT_OUTPUT
    try:
        by_deck = learnt_pairs(deck)
    except RuntimeError as exc:
        print(f"  ERROR: {exc}")
        return 1
    sections = []
    for name, pairs in by_deck.items():
        if pairs:
            sections.append(f"{name}\n{'=' * len(name)}\n"
                            + table(["Italian", "English"], [list(p) for p in pairs])
                            + f"\n{len(pairs)} word(s) learnt\n")
    output.write_text("\n".join(sections), encoding="utf-8")
    counts = [[name, len(pairs)] for name, pairs in by_deck.items()]
    print(table(["Deck", "Learnt"], counts, total=["TOTAL", sum(c for _, c in counts)]))
    print(f"\nWritten to {output}")
    return 0
