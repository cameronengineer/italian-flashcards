"""``build`` command — load sources, ingest entries, emit cards.

Passes:

  1. Load + validate ``sources.json``.
  2. Ingest: ``mode.ingest(source, ctx)`` fills ``entries`` (+ verb_forms /
     noun_phrases) and records which entries are still in each input.
  3. Retire entries that left their input; restore ones that came back.
  4. Materialise: ``mode.cards()`` → :class:`~flashcards.cards.Card` objects.
  5. Facts: ask for fun facts about card words not yet in ``word_facts``.
  6. Write ``cards`` (two rows per Card) and re-sort each deck.
"""

from __future__ import annotations

import random
import sqlite3
import threading
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Callable

import genanki

from .. import facts
from ..ai import AI
from ..cards import Card
from ..db import connect, init_schema, transaction
from ..modes import get as get_mode
from ..settings import settings
from ..sources import Source, decks_for, load, summarise, validate
from ..util import md5_hex, print_banner


@dataclass
class PipelineContext:
    """Shared state passed to every mode."""

    conn: sqlite3.Connection
    db_lock: threading.Lock
    workers: int
    ai: AI
    #: Source ids/filenames whose enrichment is re-run (``build --refresh``).
    refresh: frozenset[str] = frozenset()
    cards: list[Card] = field(default_factory=list)
    #: (source_key, mode) → (natural_ids still present in the input, complete?)
    _live: dict[tuple[str, str], tuple[set[str], bool]] = field(default_factory=dict)

    def refreshing(self, source: Source) -> bool:
        return source.id in self.refresh or source.path.name in self.refresh

    def mark_live(self, source_key: str, mode: str, natural_ids: set[str], *, complete: bool) -> None:
        """Record which entries are still in a source's input after ingest.

        ``complete=False`` means some rows couldn't be resolved (AI failure),
        so we can't tell which entries are gone — nothing gets retired.
        """
        prev_ids, prev_complete = self._live.get((source_key, mode), (set(), True))
        self._live[(source_key, mode)] = (prev_ids | natural_ids, prev_complete and complete)

    def add_card(self, card: Card, source: Source) -> None:
        """Queue a card, applying the source's audio / image / facts switches."""
        card = card.without_media(audio=source.audio, image=source.image)
        if not (source.facts and settings.facts.enabled):
            card = replace(card, fact_word=None)
        self.cards.append(card)


def _apply_retirement(ctx: PipelineContext) -> dict[str, tuple[int, int]]:
    """Set ``entries.retired`` from the live sets recorded during ingest.

    Returns ``{source_key/mode: (retired, restored)}`` for reporting.
    """
    changes: dict[str, tuple[int, int]] = {}
    for (source_key, mode), (live, complete) in ctx._live.items():
        rows = ctx.conn.execute(
            "SELECT id, natural_id, retired FROM entries WHERE source_path = ? AND mode = ?",
            (source_key, mode),
        ).fetchall()
        retire, restore = [], []
        for r in rows:
            if r["natural_id"] in live:
                if r["retired"]:
                    restore.append((r["id"],))
            elif complete and not r["retired"]:
                retire.append((r["id"],))
        ctx.conn.executemany("UPDATE entries SET retired = 1 WHERE id = ?", retire)
        ctx.conn.executemany("UPDATE entries SET retired = 0 WHERE id = ?", restore)
        if retire or restore:
            changes[f"{source_key} [{mode}]"] = (len(retire), len(restore))
    ctx.conn.commit()
    return changes


def _materialise_cards(
    ctx: PipelineContext,
    shown_facts: dict[str, tuple[str, str]],
    source_keys: list[str] | None = None,
) -> int:
    """Write ``ctx.cards`` into the cards table (two rows per Card).

    Cards are re-derived every build, so existing rows are wiped first —
    all of them, or (with ``--source``) only the selected sources' rows.
    sort_order is rewritten in ``_resort_cards``.
    """
    if source_keys is None:
        ctx.conn.execute("DELETE FROM cards")
    else:
        placeholders = ",".join("?" * len(source_keys))
        ctx.conn.execute(
            f"DELETE FROM cards WHERE entry_id IN "
            f"(SELECT id FROM entries WHERE source_path IN ({placeholders}))",
            source_keys,
        )
    rows = []
    for c in ctx.cards:
        fact, kind = shown_facts.get(facts.normalise(c.fact_word), (None, None)) if c.fact_word else (None, None)
        common = (c.labels, c.details, c.audio_text, c.image_text, fact, kind)
        rows.append((c.entry_id, c.natural_key, "en_to_it", c.deck, c.english, c.italian,
                     *common, genanki.guid_for(c.natural_key, "en_to_it")))
        rows.append((c.entry_id, c.natural_key, "it_to_en", c.deck, c.italian, c.english,
                     *common, genanki.guid_for(c.natural_key, "it_to_en")))
    before = ctx.conn.total_changes
    ctx.conn.executemany(
        """
        INSERT OR IGNORE INTO cards (
            entry_id, natural_key, direction, deck, front_text, back_highlight,
            front_labels, back_text, audio_text, image_text, fact, fact_kind,
            sort_order, guid
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
        """,
        rows,
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
                   COALESCE(e.frequency_rank, 999999) AS freq_rank,
                   COALESCE(e.rowid, 0) AS entry_rowid
            FROM cards c
            LEFT JOIN entries e ON c.entry_id = e.id
            WHERE c.deck = ?
            ORDER BY c.natural_key, c.direction
            """,
            (deck,),
        ).fetchall()
        groups: dict[str, list] = defaultdict(list)
        for r in rows:
            groups[r["entry_id"]].append(r)
        sorted_groups = sorted(
            groups.values(),
            # Tie-break on the entry's insertion order, not card ids: card
            # ids are reassigned on every build, entry rowids aren't.
            key=lambda g: (-max(r["zipf"] for r in g), min(r["freq_rank"] for r in g), g[0]["entry_rowid"]),
        )
        n = len(sorted_groups)
        w = deck_windows.get(deck, default_window)
        # Seeded per deck so the order is reproducible: an unchanged deck
        # exports identical notes, and re-running doesn't reshuffle the Anki
        # new-card queue every time.
        rng = random.Random(md5_hex(deck))
        if w > 1:
            for i in range(n):
                j = rng.randint(i, min(i + w - 1, n - 1))
                sorted_groups[i], sorted_groups[j] = sorted_groups[j], sorted_groups[i]
        pass_a, pass_b, extras = [], [], []
        for g in sorted_groups:
            # Rows arrive ordered by (natural_key, direction) so the shuffle
            # input never depends on SQLite's query plan.
            shuffled = sorted(g, key=lambda r: r["direction"])
            # When shuffle is off (window == 0), preserve the deterministic
            # within-group ordering by direction so en→it always comes first.
            if w > 0:
                rng.shuffle(shuffled)
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
    """Deck name → shuffle_window, expanded over every deck each source writes."""
    return {deck: s.shuffle_window for s in sources for deck in decks_for(s)}


def run(
    *,
    workers: int = 10,
    select: list[str] | None = None,
    skip_ai: bool = False,
    refresh: list[str] | None = None,
    on_source: Callable[[Source], None] | None = None,
) -> dict:
    """Load → ingest → retire → materialise → facts → write + sort.

    ``failed_sources`` in the returned dict is non-empty if any source's
    ingest raised. Callers (especially ``cmd_run``) MUST consult it before
    any destructive downstream step like AnkiConnect sync.
    """
    print_banner("build — load sources and populate cards")

    sources, parse_errors = load()
    errors = validate(sources, parse_errors)
    if errors:
        print("Source config errors:")
        for e in errors:
            print(f"  - {e}")
        raise SystemExit(1)

    # Shuffle windows come from the full manifest: the re-sort touches every
    # deck, including ones not selected with --source.
    windows = _deck_window_map(sources)
    source_keys: list[str] | None = None
    if select:
        sources = [s for s in sources if s.id in select or s.path.name in select]
        if not sources:
            print(f"No source matches {select}.")
            raise SystemExit(1)
        source_keys = [s.key for s in sources]

    print(summarise(sources))

    conn = connect()
    init_schema(conn)
    lock = threading.Lock()
    ctx = PipelineContext(
        conn=conn,
        db_lock=lock,
        workers=workers,
        ai=AI(conn, lock),
        refresh=frozenset(refresh or ()),
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

    retired = _apply_retirement(ctx)
    for label, (n_retired, n_restored) in sorted(retired.items()):
        print(f"  {label}: retired {n_retired}, restored {n_restored} (rows removed from / re-added to input)")

    print_banner("materialise — emit cards from entries / verb_forms / noun_phrases")
    for s in sources:
        n = get_mode(s.mode).materialise(s, ctx)
        print(f"  materialise [{s.mode:<7}] {s.id:<45} +{n}")

    if not skip_ai and settings.facts.enabled:
        words: dict[str, str] = {}
        for c in ctx.cards:
            if c.fact_word:
                words.setdefault(c.fact_word, c.english)
        facts.generate(ctx, words)

    print_banner("write cards table + re-sort")
    with transaction(conn):
        written = _materialise_cards(ctx, facts.shown(conn), source_keys)
    with_fact = conn.execute("SELECT COUNT(*) FROM cards WHERE fact IS NOT NULL").fetchone()[0]
    print(f"  wrote {written} card rows ({with_fact} with a fun fact)")
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
