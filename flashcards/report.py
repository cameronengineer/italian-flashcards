"""Console output for ``./run.sh``: numbered segments, one line per thing done.

    ════════════════════════════════════════════════════════
      Italian flashcards · Sat 10 Oct, 09:12
    ════════════════════════════════════════════════════════

    [1/6] Prepare
          backup       database.backup.20261010_091200.sqlite
          lists        unchanged · 5,010 words in 18 lists

    [4/6] Claude · writing cards
          09:13  words    ✓ 15  orribile, famoso, uh +12    48s · 1,634 left

A run switches ``VERBOSE`` off, which hides the detail single commands print
(tables, per-list counts, banners); everything else uses these helpers.
"""

from __future__ import annotations

import time

VERBOSE = True
WIDTH = 64
_PAD = " " * 6


def rule() -> None:
    print("═" * WIDTH, flush=True)


def header(title: str) -> None:
    print(flush=True)
    rule()
    print(f"  {title}", flush=True)
    rule()


def section(number: int, total: int, title: str, note: str = "") -> None:
    print(f"\n[{number}/{total}] {title}" + (f"   ({note})" if note else ""), flush=True)


def item(label: str, text: str) -> None:
    """``      lists        unchanged · 5,010 words``"""
    print(f"{_PAD}{label:<12} {text}", flush=True)


def line(text: str) -> None:
    """A timestamped progress line inside the current segment."""
    print(f"{_PAD}{time.strftime('%H:%M')}  {text}", flush=True)


def detail(text: str = "") -> None:
    """Shown by single commands; hidden during a run."""
    if VERBOSE:
        print(text, flush=True)


def duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def plural(n: int, singular: str, many: str | None = None) -> str:
    return f"{n:,} {singular if n == 1 else many or singular + 's'}"
