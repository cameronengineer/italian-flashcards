"""SQLite connection and schema.

``SCHEMA_SQL`` is the whole schema, as a new database gets it:

  lexemes, senses            one record per root word; what it means on a card
  lists, list_items          what to learn and which roots each list holds
  source_observations        every input row, as read (lineage for meanings)
  overrides                  your edits, applied over AI and dictionary data
  ai_jobs, ai_cache          the durable AI queue; every AI answer
  form_prompts, form_exclusions, word_facts, film_glosses, cue_fixes
                             what Claude wrote
  mnemonics, mistakes        leech-doctor memory aids; practice mistakes
  v4_notes                   the notes Anki should hold (rebuilt every build)
  identity, retirements, sync_runs, card_knowledge
                             what was sent to Anki, and what you know
  audits                     latest AI audit verdict per note
  asset_manifest, metadata   media provenance; checkpoints

An existing database is upgraded by ``MIGRATIONS`` (tracked with
``PRAGMA user_version``, backed up first). The chain starts at version 13:
that was the last version of the old layout, which still carried the tables
of the pre-lexicon pipeline ("v3").
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .paths import DB_PATH

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

-- ── LEXEMES: one record per root word (lemma + part of speech) ─────────
CREATE TABLE IF NOT EXISTS lexemes (
    id               TEXT PRIMARY KEY,      -- md5("lex::" lemma "::" pos)
    lemma            TEXT NOT NULL,         -- dictionary form, standard spelling
    pos              TEXT NOT NULL,         -- noun|verb|adj|adv|pron|conj|prep|intj|num|det|phrase|…
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
    status           TEXT NOT NULL DEFAULT 'new',   -- new | ready (card text written)
    issues           TEXT,                  -- Claude's doubts about the dictionary data (JSON)
    provenance       TEXT,                  -- field → source (JSON)
    updated_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ignored          TEXT,                  -- Claude's reason this entry should not be a card
    key_list         TEXT,                  -- the list whose copy of the word keeps the plain note key
    card_number      TEXT,                  -- nouns: 'singular' | 'plural' | 'both' (noun_number task)
    card_english     TEXT                   -- English for a plural-only card
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_lexemes_lemma_pos ON lexemes(lemma, pos);

-- ── SENSES: what a lexeme means on a card (idx 0 is the main meaning) ──
CREATE TABLE IF NOT EXISTS senses (
    lexeme_id   TEXT NOT NULL,
    idx         INTEGER NOT NULL,
    prompt      TEXT NOT NULL,              -- English side of the card
    hint        TEXT,                       -- short disambiguator
    register    TEXT,                       -- formal / colloquial / vulgar …
    note        TEXT,                       -- usage note (back of card)
    also        TEXT,                       -- other accepted Italian answers
    provenance  TEXT,
    active      INTEGER NOT NULL DEFAULT 1, -- 0 = archived meaning
    source_id   TEXT,                       -- the dictionary / list evidence this meaning cites
    context     TEXT,
    features    TEXT NOT NULL DEFAULT '{}', -- meaning-specific grammar (JSON)
    PRIMARY KEY (lexeme_id, idx),
    FOREIGN KEY (lexeme_id) REFERENCES lexemes(id) ON DELETE CASCADE
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
    PRIMARY KEY (list_id, lexeme_id),
    FOREIGN KEY (list_id) REFERENCES lists(id) ON DELETE CASCADE,
    FOREIGN KEY (lexeme_id) REFERENCES lexemes(id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_list_items_lexeme ON list_items(lexeme_id);
CREATE TABLE IF NOT EXISTS source_observations (
    list_id TEXT NOT NULL REFERENCES lists(id) ON DELETE CASCADE, row_key TEXT NOT NULL,
    lexeme_id TEXT NOT NULL REFERENCES lexemes(id), raw TEXT NOT NULL, payload TEXT NOT NULL,
    source_hash TEXT NOT NULL, generation TEXT NOT NULL, PRIMARY KEY(list_id, row_key)
);
CREATE INDEX IF NOT EXISTS idx_source_observations_lexeme
    ON source_observations(lexeme_id,list_id,row_key);

-- ── OVERRIDES: your edits, always applied over generated values ────────
CREATE TABLE IF NOT EXISTS overrides (
    lexeme_id TEXT NOT NULL REFERENCES lexemes(id), field TEXT NOT NULL,
    value_json TEXT NOT NULL, author TEXT NOT NULL DEFAULT 'human', reason TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (lexeme_id, field)
);

-- ── AI_JOBS: durable queue for every AI / image task ───────────────────
CREATE TABLE IF NOT EXISTS ai_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,              -- see queue.ORDER
    subject     TEXT NOT NULL,              -- lexeme id / group key
    payload     TEXT,                       -- JSON input snapshot
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | running | done | failed | cancelled
    attempts    INTEGER NOT NULL DEFAULT 0,
    result      TEXT,
    error       TEXT,
    priority    INTEGER NOT NULL DEFAULT 1000,  -- lower runs first (study order)
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    fingerprint TEXT,                       -- hash of the job's inputs: changed inputs reopen it
    force_refresh INTEGER NOT NULL DEFAULT 0,
    owner       TEXT,
    started_at  TEXT,
    next_retry_at TEXT,
    UNIQUE (kind, subject)
);
CREATE INDEX IF NOT EXISTS idx_ai_jobs_status ON ai_jobs(status, kind, priority);

-- ── AI_CACHE: every AI answer, keyed by model + task + messages ─────────
CREATE TABLE IF NOT EXISTS ai_cache (
    cache_key      TEXT PRIMARY KEY,
    response_json  TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ── What Claude wrote ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS form_prompts (
    lexeme_id   TEXT NOT NULL,
    tense       TEXT NOT NULL,
    person      TEXT NOT NULL,
    english     TEXT NOT NULL,
    source_id   TEXT,
    PRIMARY KEY (lexeme_id, tense, person),
    FOREIGN KEY (lexeme_id) REFERENCES lexemes(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS form_exclusions (
    lexeme_id TEXT NOT NULL REFERENCES lexemes(id), tense TEXT NOT NULL, person TEXT NOT NULL,
    source_id TEXT, reason TEXT NOT NULL, PRIMARY KEY(lexeme_id,tense,person)
);
CREATE TABLE IF NOT EXISTS word_facts (
    word           TEXT PRIMARY KEY,      -- facts.normalise(word)
    has_fact       INTEGER NOT NULL,
    kind           TEXT,                  -- tasks.FACT_KINDS
    fact           TEXT,
    confidence     REAL,
    model          TEXT,
    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    checked        TEXT,                  -- fact check: keep | fix | drop
    checked_text   TEXT
);
CREATE TABLE IF NOT EXISTS film_glosses (
    key         TEXT PRIMARY KEY,           -- md5 of the line as shown (lexicon.film_line_key)
    italian     TEXT NOT NULL,
    words       TEXT NOT NULL,              -- JSON [{"it": …, "en": …}], in line order
    model       TEXT,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS cue_fixes (
    lexeme_id TEXT PRIMARY KEY, prompt TEXT NOT NULL, source_hint TEXT NOT NULL DEFAULT '',
    english TEXT NOT NULL, hint TEXT
);
CREATE TABLE IF NOT EXISTS mistakes (
    id          TEXT PRIMARY KEY,           -- md5 of the Italian chunk
    italian     TEXT NOT NULL,
    english     TEXT NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS mnemonics (
    key         TEXT PRIMARY KEY,
    mnemonic    TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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

-- ── What was sent to Anki, and what you know ───────────────────────────
CREATE TABLE IF NOT EXISTS identity (
    key          TEXT PRIMARY KEY,
    guid         TEXT NOT NULL,
    kind         TEXT NOT NULL,
    first_seen   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    note_id      INTEGER,                   -- NULL once the note has left Anki
    profile      TEXT
);
CREATE TABLE IF NOT EXISTS retirements (
    key TEXT PRIMARY KEY, reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sync_runs (
    id TEXT PRIMARY KEY, profile TEXT NOT NULL, plan_json TEXT NOT NULL,
    status TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS card_knowledge (
    key TEXT NOT NULL, direction TEXT NOT NULL, card_id INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('unknown','learning','known')),
    interval INTEGER NOT NULL, lapses INTEGER NOT NULL, reviews INTEGER NOT NULL,
    suspended INTEGER NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(key, direction, card_id)
);

-- ── AUDITS: latest AI audit per note ───────────────────────────────────
CREATE TABLE IF NOT EXISTS audits (
    natural_key    TEXT NOT NULL,         -- the note key
    direction      TEXT NOT NULL,         -- always 'note'
    card_hash      TEXT NOT NULL,         -- content audited (stale if the note changed)
    verdict        TEXT NOT NULL,         -- pass | warn | fail
    severity       INTEGER,
    categories     TEXT,
    issues         TEXT,
    suggestion     TEXT,
    model          TEXT,
    audited_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (natural_key, direction)
);

-- ── Media provenance and checkpoints ───────────────────────────────────
CREATE TABLE IF NOT EXISTS asset_manifest (
    filename TEXT PRIMARY KEY, subject TEXT NOT NULL, kind TEXT NOT NULL,
    spec_hash TEXT NOT NULL, checksum TEXT NOT NULL, metadata TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

#: status column → allowed values, enforced by triggers.
_STATUSES = {
    "lexemes": "'new','ready'",
    "ai_jobs": "'pending','running','done','failed','cancelled'",
}


def _status_triggers(conn: sqlite3.Connection) -> None:
    for table, values in _STATUSES.items():
        for event in ("INSERT", "UPDATE"):
            conn.execute(f"DROP TRIGGER IF EXISTS validate_{table}_{event}")
            conn.execute(
                f"CREATE TRIGGER validate_{table}_{event} BEFORE {event} ON {table} "
                f"WHEN NEW.status NOT IN ({values}) BEGIN SELECT RAISE(ABORT, 'Invalid {table} status'); END"
            )


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    """Open a connection with sensible defaults (Row factory, FKs, WAL).

    ``check_same_thread=False`` because one connection is shared with worker
    threads; callers serialise access with a ``threading.Lock``.
    """
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def execute_statements(conn, script: str) -> None:
    """Unlike executescript, preserve the caller's transaction."""
    statement = ""
    for line in script.splitlines(True):
        statement += line
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ""
    if statement.strip():
        raise ValueError("Incomplete schema SQL")


def _m14_remove_v3(conn: sqlite3.Connection) -> None:
    """Drop the pre-lexicon ("v3") tables and everything that existed to adopt
    their Anki notes.

    Two things are kept from them first:

    * images made for v3 entries are found one last time, so each root keeps
      pointing at its existing picture (``lexemes.image_key``);
    * the identities and decks of the v3 notes still in Anki go to
      ``v3_note_keys`` / metadata ``v3_decks``, which is all ``flashcards
      purge-v3`` needs to remove those notes. It drops them when it is done.
    """
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if {"entries", "entry_lexeme"} <= tables:
        from .lexicon import existing_image, image_exists

        texts: dict[str, list] = {}
        for r in conn.execute(
            "SELECT el.lexeme_id, e.italian, e.infinitive, e.singular "
            "FROM entry_lexeme el JOIN entries e ON e.id = el.entry_id"
        ):
            texts.setdefault(r[0], []).extend([r[1], r[2], r[3]])
        for r in conn.execute("SELECT id, lemma, display, image_key FROM lexemes").fetchall():
            if r["id"] in texts and not image_exists(r["image_key"]):
                key = existing_image(r["display"], r["lemma"], *texts[r["id"]])
                if key:
                    conn.execute("UPDATE lexemes SET image_key=? WHERE id=?", (key, r["id"]))
    conn.execute("CREATE TABLE IF NOT EXISTS v3_note_keys (sort_key TEXT PRIMARY KEY)")
    if "cards" in tables:
        conn.execute(
            "INSERT OR IGNORE INTO v3_note_keys SELECT DISTINCT natural_key || '|' || direction FROM cards"
        )
        decks = [r[0] for r in conn.execute("SELECT DISTINCT deck FROM cards ORDER BY deck")]
        conn.execute("INSERT OR REPLACE INTO metadata VALUES('v3_decks',?)", (json.dumps(decks),))
    # Children before parents: entry_lexeme, cards, verb_forms and noun_phrases reference entries.
    for table in (
        "entry_lexeme",
        "cards",
        "verb_forms",
        "noun_phrases",
        "entries",
        "legacy_notes",
        "adoptions",
        "identity_aliases",
    ):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    for table, column in (("lexemes", "source_hash"), ("identity", "card_ids")):
        if column in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            try:
                conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
            except sqlite3.OperationalError:
                pass  # SQLite older than 3.35: the unused column stays, harmlessly
    conn.execute("DELETE FROM identity WHERE kind != 'v4'")
    conn.execute("DELETE FROM retirements WHERE instr(key, '|') > 0")  # v3 note identities
    conn.execute("DELETE FROM metadata WHERE key IN ('legacy_index','card_plan','notes_generation')")
    _status_triggers(conn)


#: The last schema version of the old layout. Older databases are upgraded to
#: it by the release that still had those migrations.
BASE_VERSION = 13
#: ``MIGRATIONS[i]`` upgrades ``user_version`` BASE_VERSION + i → BASE_VERSION + i + 1.
MIGRATIONS = [_m14_remove_v3]
SCHEMA_VERSION = BASE_VERSION + len(MIGRATIONS)


def init_schema(conn: sqlite3.Connection) -> None:
    """Create a fresh schema, or migrate an existing DB to ``SCHEMA_VERSION``."""
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'lexemes'").fetchone()
    if not exists:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'entries'").fetchone():
            raise ValueError(_TOO_OLD.format(version=conn.execute("PRAGMA user_version").fetchone()[0]))
        conn.executescript(SCHEMA_SQL)
        _status_triggers(conn)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
        return
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise ValueError(f"Database schema {version} is newer than supported {SCHEMA_VERSION}")
    if version < BASE_VERSION:
        raise ValueError(_TOO_OLD.format(version=version))
    if version < SCHEMA_VERSION:
        from .backup import snapshot

        snapshot(f"before migrating to schema v{SCHEMA_VERSION}", connection=conn)
    for i in range(version, SCHEMA_VERSION):
        with transaction(conn):
            MIGRATIONS[i - BASE_VERSION](conn)
            conn.execute(f"PRAGMA user_version = {i + 1}")
        from . import report

        report.detail(f"  migrated database schema to v{i + 1}")
    conn.executescript(SCHEMA_SQL)  # IF NOT EXISTS: adds any missing indexes/tables
    conn.commit()


_TOO_OLD = (
    "This database is at schema v{version}; this version upgrades from v13 or later. "
    "Check out commit 3e6520e (the last one with the older migrations), run ./run.sh "
    "once to bring the database to v13, then return to this version."
)


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
