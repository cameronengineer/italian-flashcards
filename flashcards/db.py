"""SQLite connection + schema for the flashcards pipeline.

Five tables, all created by ``init_schema``:

  entries        — one row per ingested logical item (a CSV row or extracted lemma)
  verb_forms     — 22 conjugated forms per verb entry (mode=verb)
  noun_phrases   — definite + chosen phrase variants per noun entry (mode=noun)
  cards          — final materialised Anki cards, en↔it, with stable GUIDs
  ai_cache       — content-addressed cache for AI responses (so re-runs are free)

There is no ``input_words`` table. Frequency information that used to live there
now lives on ``entries.frequency_rank`` / ``entries.zipf``; the SUBTLEX builder
fills those in.
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
    source_path          TEXT NOT NULL,
    natural_id           TEXT NOT NULL,
    mode                 TEXT NOT NULL,           -- gloss|avere|verb|noun
    deck                 TEXT NOT NULL,
    italian              TEXT NOT NULL,
    english              TEXT NOT NULL,
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

    created_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_source_natural
    ON entries(source_path, natural_id);
CREATE INDEX IF NOT EXISTS idx_entries_mode ON entries(mode);
CREATE INDEX IF NOT EXISTS idx_entries_deck ON entries(deck);


-- ── VERB FORMS: per-verb 22-form conjugation table ─────────────────────
CREATE TABLE IF NOT EXISTS verb_forms (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id     TEXT NOT NULL,
    tense        TEXT NOT NULL,            -- presente|passato_prossimo|imperfetto|imperativo
    person       TEXT NOT NULL,            -- io|tu|lui_lei|noi|voi|loro|Lei
    polarity     TEXT NOT NULL DEFAULT 'positive',
    italian      TEXT NOT NULL,
    english      TEXT NOT NULL,
    labels       TEXT,
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
    back_text       TEXT,
    audio_text      TEXT,
    image_text      TEXT,
    sort_order      INTEGER NOT NULL,
    guid            TEXT NOT NULL UNIQUE,
    FOREIGN KEY (entry_id) REFERENCES entries(id) ON DELETE CASCADE,
    UNIQUE(natural_key, direction)
);
CREATE INDEX IF NOT EXISTS idx_cards_deck ON cards(deck);
CREATE INDEX IF NOT EXISTS idx_cards_entry ON cards(entry_id);
CREATE INDEX IF NOT EXISTS idx_cards_sort ON cards(deck, sort_order);


-- ── AI_CACHE: cache OpenRouter responses keyed by prompt+schema hash ───
CREATE TABLE IF NOT EXISTS ai_cache (
    cache_key      TEXT PRIMARY KEY,
    response_json  TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
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
