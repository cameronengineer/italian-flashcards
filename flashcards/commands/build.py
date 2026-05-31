"""``build`` command — discover sources, ingest into entries, materialise cards.

Three logical passes:

  1. Discover sources from ``inputs/`` and validate the config.
  2. For each source: ``mode.ingest(source, ctx)`` — populate ``entries`` and
     mode-specific tables (verb_forms, noun_phrases).
  3. For each source: ``mode.materialise(source, ctx)`` — emit rows into ``cards``.
  4. Sort cards by frequency / order with a sliding-window shuffle per deck.
"""

from __future__ import annotations

import random
import sqlite3
import threading
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable

import genanki

from ..db import connect, init_schema, transaction
from ..modes import get as get_mode
from ..paths import OPENROUTER_KEY_FILE
from ..sources import Source, load, summarise, validate
from ..util import load_key_file, print_banner


@dataclass
class PipelineContext:
    """Shared mutable state passed to every mode invocation."""

    conn: sqlite3.Connection
    db_lock: threading.Lock
    workers: int
    _api_key: str | None = None
    _card_buffer: list[tuple] | None = None

    def __post_init__(self) -> None:
        if self._card_buffer is None:
            object.__setattr__(self, "_card_buffer", [])

    def api_key(self) -> str:
        if self._api_key is None:
            self._api_key = load_key_file(OPENROUTER_KEY_FILE)
        return self._api_key

    def add_card_pair(
        self,
        *,
        entry_id: str,
        natural_key: str,
        deck: str,
        front_text: str,
        back_highlight: str,
        back_text: str | None,
        front_labels: str | None,
        audio_text: str | None,
        image_text: str | None,
    ) -> int:
        """Queue both en→it and it→en cards. Returns 2 if both new.

        Cards land in a buffer rather than the DB immediately so we can
        rewrite ordering in one transaction at the end of the pipeline.
        """
        guid_en = genanki.guid_for(natural_key, "en_to_it")
        guid_it = genanki.guid_for(natural_key, "it_to_en")
        self._card_buffer.append((
            entry_id, natural_key, "en_to_it", deck,
            front_text, front_labels, back_highlight, back_text,
            audio_text, image_text, guid_en,
        ))
        self._card_buffer.append((
            entry_id, natural_key, "it_to_en", deck,
            back_highlight, front_labels, front_text, back_text,
            audio_text, image_text, guid_it,
        ))
        return 2


def _materialise_cards(ctx: PipelineContext) -> int:
    """Flush ctx._card_buffer into the cards table in one batch INSERT.

    The cards table is wiped first because card_items are re-derived from
    entries every build; sort_order is rewritten in ``_resort_cards``.
    """
    if not ctx._card_buffer:
        ctx.conn.execute("DELETE FROM cards")
        ctx.conn.commit()
        return 0
    ctx.conn.execute("DELETE FROM cards")
    before = ctx.conn.total_changes
    ctx.conn.executemany(
        """
        INSERT OR IGNORE INTO cards (
            entry_id, natural_key, direction, deck,
            front_text, front_labels, back_highlight, back_text,
            audio_text, image_text, sort_order, guid
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
        """,
        ctx._card_buffer,
    )
    ctx.conn.commit()
    return ctx.conn.total_changes - before


def _resort_cards(
    conn: sqlite3.Connection,
    *,
    deck_windows: dict[str, int],
    default_window: int = 50,
) -> dict[str, int]:
    """Frequency-aware sliding-window shuffle per deck.

    Within each deck:
      1. Group by entry_id (en+it pair).
      2. Sort by frequency descending (entries with no freq go last in insertion order).
      3. Sliding-window shuffle of groups — window size is per-deck.
      4. Emit en→it then it→en so the two directions are never adjacent.

    ``deck_windows`` maps deck name → shuffle_window. ``default_window`` is
    the fallback when a deck name isn't in the map (defensive).
    """
    decks = [r["deck"] for r in conn.execute("SELECT DISTINCT deck FROM cards").fetchall()]
    deck_counts: dict[str, int] = {}
    updates: list[tuple[int, int]] = []
    current = 1
    for deck in sorted(decks):
        rows = conn.execute(
            """
            SELECT c.id, c.entry_id, c.direction, c.guid, c.natural_key,
                   COALESCE(e.zipf, 0) AS zipf,
                   COALESCE(e.frequency_rank, 999999) AS freq_rank
            FROM cards c
            LEFT JOIN entries e ON c.entry_id = e.id
            WHERE c.deck = ?
            """,
            (deck,),
        ).fetchall()
        groups: dict[str, list] = defaultdict(list)
        for r in rows:
            groups[r["entry_id"]].append(r)
        sorted_groups = sorted(
            groups.values(),
            key=lambda g: (-max(r["zipf"] for r in g), min(r["freq_rank"] for r in g), min(r["id"] for r in g)),
        )
        n = len(sorted_groups)
        w = deck_windows.get(deck, default_window)
        if w > 1:
            for i in range(n):
                j = random.randint(i, min(i + w - 1, n - 1))
                sorted_groups[i], sorted_groups[j] = sorted_groups[j], sorted_groups[i]
        pass_a, pass_b, extras = [], [], []
        for g in sorted_groups:
            shuffled = list(g)
            # When shuffle is off (window == 0), preserve the deterministic
            # within-group ordering by direction so en→it always comes first.
            if w > 0:
                random.shuffle(shuffled)
            if shuffled:
                pass_a.append(shuffled[0])
            if len(shuffled) >= 2:
                pass_b.append(shuffled[1])
            if len(shuffled) > 2:
                extras.extend(shuffled[2:])
        ordered = pass_a + pass_b + extras
        deck_counts[deck] = len(ordered)
        for row in ordered:
            updates.append((current, row["id"]))
            current += 1
    if updates:
        conn.executemany("UPDATE cards SET sort_order = ? WHERE id = ?", updates)
        conn.commit()
    return deck_counts


def _deck_window_map(sources: list[Source]) -> dict[str, int]:
    """Build a deck → shuffle_window map by expanding each source's decks.

    For subtlex sources, all four deck fields (``deck``, ``infinitive_deck``,
    ``extras["noun_deck"]``, ``phrases_deck``) are required;
    ``sources.validate()`` enforces this so the lookups below are safe.
    """
    from ..grammar import TENSE_DISPLAY

    windows: dict[str, int] = {}
    for s in sources:
        w = s.shuffle_window
        if s.mode == "verb":
            for tense_display in TENSE_DISPLAY.values():
                windows[f"{s.deck} {tense_display}"] = w
            if s.infinitive_deck:
                windows[s.infinitive_deck] = w
        elif s.mode == "noun":
            windows[s.deck] = w
            if s.phrases_deck:
                windows[s.phrases_deck] = w
        elif s.mode == "subtlex":
            # Subtlex expands to verb + noun virtual sources. All four deck
            # fields are required and validated upstream.
            verb_deck = s.deck
            noun_deck = s.extras["noun_deck"]
            for tense_display in TENSE_DISPLAY.values():
                windows[f"{verb_deck} {tense_display}"] = w
            windows[s.infinitive_deck] = w
            windows[noun_deck] = w
            windows[s.phrases_deck] = w
        else:
            windows[s.deck] = w
    return windows


def run(
    *,
    workers: int = 10,
    select: list[str] | None = None,
    skip_ai: bool = False,
    on_source: Callable[[Source], None] | None = None,
) -> dict:
    """Discover → ingest → materialise → re-sort. Returns a summary dict.

    ``failed_sources`` in the returned dict is non-empty if any source's
    ingest pass raised. Callers (especially ``cmd_run``) MUST consult it
    before any destructive downstream step like AnkiConnect sync.
    """
    print_banner("build — load sources and populate cards")

    sources, parse_errors = load()
    errors = validate(sources, parse_errors)
    if errors:
        print("Source config errors:")
        for e in errors:
            print(f"  - {e}")
        raise SystemExit(1)

    if select:
        sources = [s for s in sources if s.id in select or s.path.name in select]

    print(summarise(sources))

    conn = connect()
    init_schema(conn)
    ctx = PipelineContext(
        conn=conn,
        db_lock=threading.Lock(),
        workers=workers,
    )

    ingest_counts: dict[str, int] = {}
    failed_sources: list[str] = []
    if not skip_ai:
        for s in sources:
            if on_source:
                on_source(s)
            mode = get_mode(s.mode)
            try:
                n = mode.ingest(s, ctx)
                ingest_counts[s.id] = n
                print(f"  ingest [{s.mode:<7}] {s.id:<45} +{n}")
            except Exception as exc:  # noqa: BLE001
                failed_sources.append(s.id)
                print(f"  ingest [{s.mode:<7}] {s.id:<45} FAILED: {exc}")

    print_banner("materialise — emit cards from entries / verb_forms / noun_phrases")
    for s in sources:
        mode = get_mode(s.mode)
        n = mode.materialise(s, ctx)
        print(f"  materialise [{s.mode:<7}] {s.id:<45} +{n}")

    print_banner("write cards table + re-sort")
    with transaction(conn):
        written = _materialise_cards(ctx)
    print(f"  wrote {written} card rows")
    windows = _deck_window_map(sources)
    deck_counts = _resort_cards(conn, deck_windows=windows)
    print(f"  sorted {sum(deck_counts.values())} cards across {len(deck_counts)} decks")
    for deck, count in sorted(deck_counts.items()):
        w = windows.get(deck, 50)
        note = "no shuffle" if w == 0 else f"window={w}"
        print(f"    {deck:<48} {count:>5}  {note}")

    if failed_sources:
        print(f"\nWARNING: {len(failed_sources)} source(s) failed ingest: {failed_sources}")
        print("Skip sync (`--no-sync`) or fix the failing source before re-running.")

    conn.close()
    return {
        "ingest_counts": ingest_counts,
        "failed_sources": failed_sources,
        "cards_written": written,
        "deck_counts": deck_counts,
    }
