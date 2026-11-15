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
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

VERBOSE = True
WIDTH = 64
_PAD = " " * 6
EVENT_FILE: Path | None = None
_event_lock = threading.Lock()


def event(name: str, **data) -> None:
    """Durable diagnostics per run, without provider prompts or private source text."""
    if EVENT_FILE is None:
        return
    row = {"at": datetime.now(timezone.utc).isoformat(), "event": name, **data}
    with _event_lock:
        EVENT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with EVENT_FILE.open("a", encoding="utf-8") as file:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")


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


class Progress:
    """Timestamped progress lines for long media work, at most every ``every`` seconds:

    ``14:02  audio     1,250 / 5,985 · 3 failed · 41/min · ~1h 55m left``
    """

    def __init__(self, label: str, total: int, every: float = 60) -> None:
        self.label, self.total, self.every = label, total, every
        self.done = self.failed = 0
        self.started = self.said = time.monotonic()
        line(f"{label:<9} {total:,} to make …")

    def tick(self, ok: bool = True) -> None:
        self.done += 1
        self.failed += not ok
        now = time.monotonic()
        if now - self.said >= self.every and self.done < self.total:
            self.said = now
            line(self._status(now))

    def _status(self, now: float) -> str:
        per_second = self.done / max(now - self.started, 1e-9)
        left = (self.total - self.done) / per_second if per_second else 0
        return (
            f"{self.label:<9} {self.done:,} / {self.total:,}"
            + (f" · {self.failed:,} failed" if self.failed else "")
            + f" · {per_second * 60:,.0f}/min · ~{duration(left)} left"
        )


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
