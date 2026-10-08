"""SQLite connection + schema for the flashcards pipeline.

Tables, all created by ``init_schema``:

  entries        — one row per ingested item (a CSV row or a SUBTLEX lemma)
  verb_forms     — conjugated forms per verb entry (46 across 8 tenses)
  noun_phrases   — definite + one chosen phrase family per noun entry
  cards          — materialised Anki cards, en↔it, with stable GUIDs
  word_facts     — one "did you know?" answer per distinct word
  audits         — latest AI audit verdict per card (``flashcards audit``)
  ai_cache       — every AI answer, keyed by model + task + messages

Frequency information lives on ``entries.frequency_rank`` / ``entries.zipf``;
the SUBTLEX builder fills those in.

Schema changes go through ``MIGRATIONS`` (tracked with ``PRAGMA user_version``)
so existing databases pick up new columns. ``SCHEMA_SQL`` is always the latest
shape and is used verbatim for brand-new databases.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .paths import DB_PATH

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

-- ── ENTRIES: one row per logical "thing we know about" ─────────────────
CREATE TABLE IF NOT EXISTS entries (
    id                   TEXT PRIMARY KEY,
    source_path          TEXT NOT NULL,           -- relative to inputs/
    natural_id           TEXT NOT NULL,
    mode                 TEXT NOT NULL,           -- gloss|avere|verb|noun (subtlex stores verb/noun)
    deck                 TEXT NOT NULL,
    italian              TEXT NOT NULL,
    english              TEXT NOT NULL,
    input_english        TEXT,                    -- CSV gloss the row was built from (edit detection)
    confidence           REAL,

    -- Verb fields (only filled when mode='verb')
    infinitive           TEXT,
    auxiliary            TEXT,
    past_participle      TEXT,
    is_reflexive         INTEGER NOT NULL DEFAULT 0,

    -- Noun fields (only filled when mode='noun')
    singular             TEXT,
    plural               TEXT,
    gender               TEXT,
    definite_singular    TEXT,
    definite_plural      TEXT,
    indefinite_singular  TEXT,

    -- Optional frequency info from SUBTLEX (used for sort order)
    frequency_rank       INTEGER,
    zipf                 REAL,

    -- 1 when the item is no longer in its source input (kept, not deleted,
    -- so restoring the row restores the same note and review history)
    retired              INTEGER NOT NULL DEFAULT 0,

    created_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_source_mode_natural
    ON entries(source_path, mode, natural_id);
CREATE INDEX IF NOT EXISTS idx_entries_mode ON entries(mode);
CREATE INDEX IF NOT EXISTS idx_entries_deck ON entries(deck);


-- ── VERB FORMS: per-verb 22-form conjugation table ─────────────────────
CREATE TABLE IF NOT EXISTS verb_forms (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id     TEXT NOT NULL,
    tense        TEXT NOT NULL,            -- grammar.TENSES
    person       TEXT NOT NULL,            -- io|tu|lui_lei|noi|voi|loro|Lei
    polarity     TEXT NOT NULL DEFAULT 'positive',
    italian      TEXT NOT NULL,
    english      TEXT NOT NULL,
    labels       TEXT,
    card_key     TEXT NOT NULL,            -- natural_key of the cards it produces
    FOREIGN KEY (entry_id) REFERENCES entries(id) ON DELETE CASCADE,
    UNIQUE(entry_id, tense, person, polarity)
);
CREATE INDEX IF NOT EXISTS idx_verb_forms_entry ON verb_forms(entry_id);


-- ── NOUN PHRASES: per-noun definite/indefinite/etc. forms ──────────────
CREATE TABLE IF NOT EXISTS noun_phrases (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id     TEXT NOT NULL,
    phrase_type  TEXT NOT NULL,            -- definite|indefinite|articulated_preposition|demonstrative|possessive
    number       TEXT NOT NULL,            -- singular|plural
    preposition  TEXT,
    italian      TEXT NOT NULL,
    english      TEXT NOT NULL,
    labels       TEXT,
    card_key     TEXT NOT NULL,            -- natural_key of the cards it produces
    FOREIGN KEY (entry_id) REFERENCES entries(id) ON DELETE CASCADE,
    UNIQUE(entry_id, phrase_type, number, preposition)
);
CREATE INDEX IF NOT EXISTS idx_noun_phrases_entry ON noun_phrases(entry_id);


-- ── CARDS: the final Anki notes (one row per direction) ─────────────────
CREATE TABLE IF NOT EXISTS cards (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id        TEXT NOT NULL,
    natural_key     TEXT NOT NULL,
    direction       TEXT NOT NULL,        -- en_to_it | it_to_en
    deck            TEXT NOT NULL,
    front_text      TEXT NOT NULL,
    front_labels    TEXT,
    back_highlight  TEXT NOT NULL,
    back_text       TEXT,                 -- details line (dictionary form / grammar)
    audio_text      TEXT,
    image_text      TEXT,
    fact            TEXT,                 -- fun fact shown on the back, if any
    fact_kind       TEXT,
    sort_order      INTEGER NOT NULL,
    guid            TEXT NOT NULL UNIQUE,
    FOREIGN KEY (entry_id) REFERENCES entries(id) ON DELETE CASCADE,
    UNIQUE(natural_key, direction)
);
CREATE INDEX IF NOT EXISTS idx_cards_deck ON cards(deck);
CREATE INDEX IF NOT EXISTS idx_cards_entry ON cards(entry_id);
CREATE INDEX IF NOT EXISTS idx_cards_sort ON cards(deck, sort_order);


-- ── AI_CACHE: every AI answer, keyed by model + task + messages ─────────
CREATE TABLE IF NOT EXISTS ai_cache (
    cache_key      TEXT PRIMARY KEY,
    response_json  TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);


-- ── WORD_FACTS: one fun-fact answer per distinct word ──────────────────
CREATE TABLE IF NOT EXISTS word_facts (
    word           TEXT PRIMARY KEY,      -- facts.normalise(word)
    has_fact       INTEGER NOT NULL,
    kind           TEXT,                  -- tasks.FACT_KINDS
    fact           TEXT,
    confidence     REAL,
    model          TEXT,
    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ── AUDITS: latest AI audit per card ─────────────────────────────────
CREATE TABLE IF NOT EXISTS audits (
    natural_key    TEXT NOT NULL,
    direction      TEXT NOT NULL,
    card_hash      TEXT NOT NULL,         -- content audited (stale if the card changed)
    verdict        TEXT NOT NULL,         -- pass | warn | fail
    severity       INTEGER,
    categories     TEXT,
    issues         TEXT,
    suggestion     TEXT,
    model          TEXT,
    audited_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (natural_key, direction)
);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    """Open a connection with sensible defaults (Row factory, FKs, WAL).

    ``check_same_thread=False`` is set because we share one connection across a
    worker pool. Concurrent access is serialised by ``threading.Lock`` higher up
    the stack (``PipelineContext.db_lock``).
    """
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _m1_portable_identity(conn: sqlite3.Connection) -> None:
    """Make identity independent of the repo location and of row IDs.

    * ``entries.source_path``: absolute path → path relative to ``inputs/``.
      ``entries.id`` is left untouched, so existing card GUIDs don't change.
    * ``entries.retired`` and ``entries.input_english`` columns.
    * Unique key becomes ``(source_path, mode, natural_id)`` so a SUBTLEX lemma
      can exist as both a verb and a noun (essere, potere, dovere).
    * ``verb_forms.card_key`` / ``noun_phrases.card_key``: persisted card
      natural keys. Existing rows get the legacy ``verb_form:<rowid>`` /
      ``noun_phrase:<rowid>`` keys (same GUIDs); new rows get semantic keys.
    """
    for (eid, sp) in conn.execute("SELECT id, source_path FROM entries").fetchall():
        marker = "/inputs/"
        if marker in sp:
            rel = sp[sp.rindex(marker) + len(marker):]
            conn.execute("UPDATE entries SET source_path = ? WHERE id = ?", (rel, eid))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(entries)")}
    if "retired" not in cols:
        conn.execute("ALTER TABLE entries ADD COLUMN retired INTEGER NOT NULL DEFAULT 0")
    if "input_english" not in cols:
        # NULL for legacy rows: the next ingest records the current CSV gloss
        # as the baseline, and only later CSV edits are propagated.
        conn.execute("ALTER TABLE entries ADD COLUMN input_english TEXT")
    conn.execute("DROP INDEX IF EXISTS idx_entries_source_natural")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_source_mode_natural "
        "ON entries(source_path, mode, natural_id)"
    )
    for table, prefix in (("verb_forms", "verb_form"), ("noun_phrases", "noun_phrase")):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if "card_key" not in cols:
            # SQLite can't add a NOT NULL column without a default; the
            # default is immediately overwritten for every existing row.
            conn.execute(f"ALTER TABLE {table} ADD COLUMN card_key TEXT NOT NULL DEFAULT ''")
        conn.execute(
            f"UPDATE {table} SET card_key = '{prefix}:' || id WHERE card_key = ''"
        )


def _m2_facts_and_audits(conn: sqlite3.Connection) -> None:
    """Fun facts on cards + persisted audit verdicts.

    ``cards`` gains ``fact`` / ``fact_kind``; the ``word_facts`` and
    ``audits`` tables are created by ``SCHEMA_SQL`` (IF NOT EXISTS) right
    after the migrations run.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(cards)")}
    for name in ("fact", "fact_kind"):
        if name not in cols:
            conn.execute(f"ALTER TABLE cards ADD COLUMN {name} TEXT")


#: Ordered schema migrations. Index ``i`` upgrades ``user_version`` i → i+1.
MIGRATIONS = [_m1_portable_identity, _m2_facts_and_audits]
SCHEMA_VERSION = len(MIGRATIONS)


def init_schema(conn: sqlite3.Connection) -> None:
    """Create a fresh schema, or migrate an existing DB to ``SCHEMA_VERSION``."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'entries'"
    ).fetchone()
    if not exists:
        conn.executescript(SCHEMA_SQL)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
        return
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < SCHEMA_VERSION:
        from .backup import snapshot

        snapshot(f"before migrating to schema v{SCHEMA_VERSION}")
    for i in range(version, SCHEMA_VERSION):
        with transaction(conn):
            MIGRATIONS[i](conn)
            conn.execute(f"PRAGMA user_version = {i + 1}")
        print(f"  migrated database schema to v{i + 1}")
    conn.executescript(SCHEMA_SQL)  # IF NOT EXISTS: adds any missing indexes/tables
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Explicit transaction context — BEGIN IMMEDIATE on enter."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def managed_decks(conn: sqlite3.Connection) -> list[str]:
    """Distinct deck names currently in ``cards`` — the single source of truth."""
    rows = conn.execute(
        "SELECT DISTINCT deck FROM cards ORDER BY deck"
    ).fetchall()
    return [r["deck"] for r in rows]
