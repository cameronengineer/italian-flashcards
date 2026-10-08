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
-- ════════════════════════ v4: word-first lexicon ════════════════════════

-- ── LEXEMES: one record per root word (lemma + part of speech) ─────────
CREATE TABLE IF NOT EXISTS lexemes (
    id               TEXT PRIMARY KEY,      -- md5("lex::" lemma "::" pos)
    lemma            TEXT NOT NULL,         -- dictionary form, standard spelling
    pos              TEXT NOT NULL,         -- noun|verb|adj|adv|pron|conj|prep|intj|num|det|phrase
    display          TEXT,                  -- how the card shows it ("la casa", "bello")
    gender           TEXT,
    plural           TEXT,
    english_plural   TEXT,
    forms_json       TEXT,                  -- adjective forms / verb info (JSON)
    irregular        INTEGER NOT NULL DEFAULT 0,
    ipa              TEXT,
    commons_audio    TEXT,                  -- Wikimedia Commons pronunciation URL
    etymology        TEXT,                  -- Wiktionary etymology text
    etymology_chain  TEXT,                  -- one hop down (e.g. French robinet)
    in_kaikki        INTEGER NOT NULL DEFAULT 0,
    zipf             REAL,
    freq_rank        INTEGER,
    cognate_rule     TEXT,
    cognate_score    REAL,
    image_key        TEXT,                  -- text whose md5 names the image file
    status           TEXT NOT NULL DEFAULT 'new',   -- new | ready | needs_review
    issues           TEXT,                  -- verification disagreements (JSON)
    provenance       TEXT,                  -- field → source (JSON)
    updated_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_lexemes_lemma_pos ON lexemes(lemma, pos);

-- ── SENSES: what a lexeme means on a card ──────────────────────────────
CREATE TABLE IF NOT EXISTS senses (
    lexeme_id   TEXT NOT NULL,
    idx         INTEGER NOT NULL,
    prompt      TEXT NOT NULL,              -- English side of the card
    hint        TEXT,                       -- short disambiguator
    register    TEXT,                       -- formal / colloquial / vulgar …
    note        TEXT,                       -- usage note (back of card)
    also        TEXT,                       -- other accepted Italian answers
    provenance  TEXT,
    PRIMARY KEY (lexeme_id, idx)
);

-- ── LISTS: what you want to learn (CILS levels, a movie, …) ─────────────
CREATE TABLE IF NOT EXISTS lists (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    deck        TEXT NOT NULL,
    settings    TEXT
);
CREATE TABLE IF NOT EXISTS list_items (
    list_id     TEXT NOT NULL,
    lexeme_id   TEXT NOT NULL,
    rank        INTEGER NOT NULL,           -- order within the list
    raw         TEXT,                       -- the row as written in the source
    hint        TEXT,                       -- the source's own English gloss
    context     TEXT,                       -- JSON: count, first_seen, example (movies)
    PRIMARY KEY (list_id, lexeme_id)
);
CREATE INDEX IF NOT EXISTS idx_list_items_lexeme ON list_items(lexeme_id);

-- legacy v3 entry → lexeme (for adopting studied notes)
CREATE TABLE IF NOT EXISTS entry_lexeme (
    entry_id    TEXT PRIMARY KEY,
    lexeme_id   TEXT NOT NULL
);

-- ── AI_JOBS: durable queue for every AI / image task ───────────────────
CREATE TABLE IF NOT EXISTS ai_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,              -- lexeme_enrich | verb_prompts | phrase_enrich | disambiguate | image
    subject     TEXT NOT NULL,              -- lexeme id / group key
    payload     TEXT,                       -- JSON input snapshot
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed
    attempts    INTEGER NOT NULL DEFAULT 0,
    result      TEXT,
    error       TEXT,
    priority    INTEGER NOT NULL DEFAULT 1000,  -- lower runs first (study order)
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (kind, subject)
);
CREATE INDEX IF NOT EXISTS idx_ai_jobs_status ON ai_jobs(status, kind, priority);

-- ── KNOWLEDGE: what you already know, read back from Anki ──────────────
CREATE TABLE IF NOT EXISTS knowledge (
    lexeme_id   TEXT NOT NULL,
    card_type   TEXT NOT NULL,
    state       TEXT NOT NULL,              -- unknown | learning | known
    interval    INTEGER,
    lapses      INTEGER,
    reviews     INTEGER,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (lexeme_id, card_type)
);

-- ── V4_NOTES: desired Anki notes (rebuilt every build) ─────────────────
CREATE TABLE IF NOT EXISTS v4_notes (
    key          TEXT PRIMARY KEY,          -- stable note identity, e.g. vocab:<lexeme>:0
    lexeme_id    TEXT,
    card_type    TEXT NOT NULL,
    deck         TEXT NOT NULL,
    fields_json  TEXT NOT NULL,
    fields_hash  TEXT NOT NULL,
    tags         TEXT NOT NULL,
    sort_order   INTEGER NOT NULL,
    ready        INTEGER NOT NULL,          -- content + image present
    blocked_by   TEXT,                      -- why not ready
    audio_text   TEXT                       -- what the audio says (no "/a" variants)
);

-- ── FORM_PROMPTS: English prompts for verb forms (verb_prompts task) ───
CREATE TABLE IF NOT EXISTS form_prompts (
    lexeme_id   TEXT NOT NULL,
    tense       TEXT NOT NULL,
    person      TEXT NOT NULL,
    english     TEXT NOT NULL,
    PRIMARY KEY (lexeme_id, tense, person)
);

-- ── MISTAKES: practice errors turned into cards ────────────────────────
CREATE TABLE IF NOT EXISTS mistakes (
    id          TEXT PRIMARY KEY,           -- md5 of the Italian chunk
    italian     TEXT NOT NULL,
    english     TEXT NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ── MNEMONICS: leech doctor output per note key ────────────────────────
CREATE TABLE IF NOT EXISTS mnemonics (
    key         TEXT PRIMARY KEY,
    mnemonic    TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ── IDENTITY: every note key ever sent to Anki (exported to git) ───────
CREATE TABLE IF NOT EXISTS identity (
    key          TEXT PRIMARY KEY,
    guid         TEXT NOT NULL,
    kind         TEXT NOT NULL,             -- v4 | legacy
    first_seen   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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


def _m3_lexicon(conn: sqlite3.Connection) -> None:
    """v4 word-first tables (lexemes, senses, lists, ai_jobs, knowledge,
    v4_notes, identity). They are all ``CREATE … IF NOT EXISTS`` in
    ``SCHEMA_SQL``, which runs right after the migrations; nothing to alter."""


def _m4_note_audio(conn: sqlite3.Connection) -> None:
    """v4_notes.audio_text (databases that got v4_notes before it existed)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(v4_notes)")}
    if cols and "audio_text" not in cols:
        conn.execute("ALTER TABLE v4_notes ADD COLUMN audio_text TEXT")


#: Ordered schema migrations. Index ``i`` upgrades ``user_version`` i → i+1.
MIGRATIONS = [_m1_portable_identity, _m2_facts_and_audits, _m3_lexicon, _m4_note_audio]
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
