"""SQLite connection, schema and migrations.

v4 (word-first) tables:
  lexemes, senses          one record per root word; what it means on a card
  lists, list_items        what to learn and which roots each list holds
  ai_jobs, ai_cache        the durable AI queue; every AI answer
  form_prompts, word_facts English prompts for verb forms; fun facts
  mnemonics, mistakes      leech-doctor memory aids; practice mistakes
  v4_notes                 the notes Anki should hold (rebuilt every build)
  audits                   latest AI audit verdict per note

Operations tables (``OPERATIONS_SQL``): overrides (human edits), identity +
adoptions + retirements + sync_runs (what was sent to Anki), card_knowledge
(what you know), source_observations + identity_aliases (input lineage),
review_decisions, asset_manifest and metadata (checkpoints).

v3 tables (entries, verb_forms, noun_phrases, cards) are kept read-only so
studied notes from before v4 can be adopted.

Schema changes go through ``MIGRATIONS`` (tracked with ``PRAGMA user_version``,
backed up first). ``SCHEMA_SQL`` + ``_m5_integrity`` build a fresh database.
"""

from __future__ import annotations

import sqlite3
import json
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
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | running | done | failed | cancelled
    attempts    INTEGER NOT NULL DEFAULT 0,
    result      TEXT,
    error       TEXT,
    priority    INTEGER NOT NULL DEFAULT 1000,  -- lower runs first (study order)
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (kind, subject)
);
CREATE INDEX IF NOT EXISTS idx_ai_jobs_status ON ai_jobs(status, kind, priority);

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

-- ── LEGACY_NOTES: weekly index of the v3 notes still in Anki ───────────
CREATE TABLE IF NOT EXISTS legacy_notes (
    note_id     INTEGER PRIMARY KEY,
    sort_key    TEXT NOT NULL,
    cards       TEXT NOT NULL               -- JSON card ids
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

    ``check_same_thread=False`` because one connection is shared with worker
    threads; callers serialise access with a ``threading.Lock``.
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
    for eid, sp in conn.execute("SELECT id, source_path FROM entries").fetchall():
        marker = "/inputs/"
        if marker in sp:
            rel = sp[sp.rindex(marker) + len(marker) :]
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
        conn.execute(f"UPDATE {table} SET card_key = '{prefix}:' || id WHERE card_key = ''")


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
    """v4 word-first tables (lexemes, senses, lists, ai_jobs, v4_notes,
    identity). They are all ``CREATE … IF NOT EXISTS`` in
    ``SCHEMA_SQL``, which runs right after the migrations; nothing to alter."""


def _m4_note_audio(conn: sqlite3.Connection) -> None:
    """v4_notes.audio_text (databases that got v4_notes before it existed)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(v4_notes)")}
    if cols and "audio_text" not in cols:
        conn.execute("ALTER TABLE v4_notes ADD COLUMN audio_text TEXT")


OPERATIONS_SQL = """
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS overrides (
    lexeme_id TEXT NOT NULL REFERENCES lexemes(id), field TEXT NOT NULL,
    value_json TEXT NOT NULL, author TEXT NOT NULL DEFAULT 'human', reason TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (lexeme_id, field)
);
CREATE TABLE IF NOT EXISTS source_observations (
    list_id TEXT NOT NULL REFERENCES lists(id) ON DELETE CASCADE, row_key TEXT NOT NULL,
    lexeme_id TEXT NOT NULL REFERENCES lexemes(id), raw TEXT NOT NULL, payload TEXT NOT NULL,
    source_hash TEXT NOT NULL, generation TEXT NOT NULL, PRIMARY KEY(list_id, row_key)
);
CREATE TABLE IF NOT EXISTS identity_aliases (
    old_key TEXT PRIMARY KEY, new_key TEXT NOT NULL, reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS adoptions (
    key TEXT NOT NULL, direction TEXT NOT NULL, note_id INTEGER NOT NULL,
    profile TEXT NOT NULL, PRIMARY KEY(key, direction, profile)
);
CREATE TABLE IF NOT EXISTS retirements (
    key TEXT PRIMARY KEY, reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sync_runs (
    id TEXT PRIMARY KEY, profile TEXT NOT NULL, plan_json TEXT NOT NULL,
    status TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS review_decisions (
    subject TEXT NOT NULL, content_hash TEXT NOT NULL, decision TEXT NOT NULL,
    reason TEXT NOT NULL, author TEXT NOT NULL DEFAULT 'human',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(subject, content_hash)
);
CREATE TABLE IF NOT EXISTS asset_manifest (
    filename TEXT PRIMARY KEY, subject TEXT NOT NULL, kind TEXT NOT NULL,
    spec_hash TEXT NOT NULL, checksum TEXT NOT NULL, metadata TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS card_knowledge (
    key TEXT NOT NULL, direction TEXT NOT NULL, card_id INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('unknown','learning','known')),
    interval INTEGER NOT NULL, lapses INTEGER NOT NULL, reviews INTEGER NOT NULL,
    suspended INTEGER NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(key, direction, card_id)
);
"""


def execute_statements(conn, script: str) -> None:
    """Unlike executescript, preserve the caller's transaction."""
    statement = ""
    for line in script.splitlines(True):
        statement += line
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ""
    if statement.strip():
        raise ValueError("Incomplete migration SQL")


def _m5_integrity(conn: sqlite3.Connection) -> None:
    """Operations tables, foreign keys, queue/identity columns, status checks,
    and human edits copied into ``overrides``."""
    additions = {
        "ai_jobs": {
            "fingerprint": "TEXT",
            "force_refresh": "INTEGER NOT NULL DEFAULT 0",
            "owner": "TEXT",
            "started_at": "TEXT",
            "next_retry_at": "TEXT",
        },
        "identity": {"note_id": "INTEGER", "profile": "TEXT", "card_ids": "TEXT"},
        "senses": {"active": "INTEGER NOT NULL DEFAULT 1", "source_id": "TEXT", "context": "TEXT"},
        "lexemes": {"source_hash": "TEXT"},
    }
    for table, cols in additions.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in cols.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    # Numeric v4 values were Anki note IDs, never actual GUIDs.
    conn.execute(
        "UPDATE identity SET note_id=CAST(guid AS INTEGER), guid='' "
        "WHERE kind='v4' AND guid != '' AND guid NOT GLOB '*[^0-9]*' AND note_id IS NULL"
    )
    relationships = {
        "senses": [("lexeme_id", "lexemes", "id", "CASCADE")],
        "list_items": [("list_id", "lists", "id", "CASCADE"), ("lexeme_id", "lexemes", "id", "RESTRICT")],
        "entry_lexeme": [
            ("entry_id", "entries", "id", "CASCADE"),
            ("lexeme_id", "lexemes", "id", "RESTRICT"),
        ],
        "form_prompts": [("lexeme_id", "lexemes", "id", "CASCADE")],
    }
    # Superseded by card_knowledge (per card and direction, read back on sync).
    conn.execute("DROP TABLE IF EXISTS knowledge")
    for table, refs in relationships.items():
        if list(conn.execute(f"PRAGMA foreign_key_list({table})")):
            continue
        for col, parent, pk, _action in refs:
            n = conn.execute(
                f"SELECT count(*) FROM {table} WHERE {col} NOT IN (SELECT {pk} FROM {parent})"
            ).fetchone()[0]
            if n:
                raise ValueError(
                    f"Migration stopped: {table} contains {n} orphan {col} values; restore/repair first"
                )
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
        indexes = [
            r[0]
            for r in conn.execute(
                "SELECT sql FROM sqlite_master WHERE tbl_name=? AND type='index' AND sql IS NOT NULL",
                (table,),
            )
        ]
        sql = sql.replace(f"CREATE TABLE {table}", f"CREATE TABLE {table}_new", 1)
        end = sql.rfind(")")
        constraints = "".join(
            f", FOREIGN KEY ({col}) REFERENCES {parent}({pk}) ON DELETE {action}"
            for col, parent, pk, action in refs
        )
        conn.execute(sql[:end] + constraints + sql[end:])
        conn.execute(f"INSERT INTO {table}_new SELECT * FROM {table}")
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
        for sql in indexes:
            conn.execute(sql)
    execute_statements(conn, OPERATIONS_SQL)
    for table, column, values in (
        ("lexemes", "status", "'new','ready','needs_review'"),
        ("ai_jobs", "status", "'pending','running','done','failed','cancelled'"),
    ):
        for event in ("INSERT", "UPDATE"):
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS validate_{table}_{event} BEFORE {event} ON {table} "
                f"WHEN NEW.{column} NOT IN ({values}) BEGIN SELECT RAISE(ABORT, 'Invalid {table} status'); END"
            )
    # Preserve existing human edits as a separate authority from regenerated data.
    for row in conn.execute("SELECT * FROM lexemes").fetchall():
        for field, source in json.loads(row["provenance"] or "{}").items():
            if source == "human" and field in row.keys():
                conn.execute(
                    "INSERT OR IGNORE INTO overrides(lexeme_id,field,value_json) VALUES(?,?,?)",
                    (row["id"], field, json.dumps(row[field])),
                )


def _m6_queue_recovery(conn: sqlite3.Connection) -> None:
    """Index source evidence and retry failures caused by repaired queue bugs once.

    Planning rebuilds eligible payloads and cancels obsolete jobs before dispatch.
    Keep their error/result evidence until a successful replacement is committed.
    """
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_source_observations_lexeme "
        "ON source_observations(lexeme_id,list_id,row_key)"
    )
    conn.execute(
        "UPDATE ai_jobs SET status='pending',attempts=0,next_retry_at=NULL,owner=NULL "
        "WHERE status='failed' AND error IN (?,?,?)",
        (
            "the JSON object must be str, bytes or bytearray, not NoneType",
            "Provider must return exactly every requested verb form",
            "Provider must return exactly the requested IDs once each",
        ),
    )


def _m7_meaning_contracts(conn):
    """Preserve keys/media; attach evidence and record every generated-form outcome."""
    if "features" not in {r[1] for r in conn.execute("PRAGMA table_info(senses)")}:
        conn.execute("ALTER TABLE senses ADD COLUMN features TEXT NOT NULL DEFAULT '{}'")
    if "source_id" not in {r[1] for r in conn.execute("PRAGMA table_info(form_prompts)")}:
        conn.execute("ALTER TABLE form_prompts ADD COLUMN source_id TEXT")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cue_overrides (key TEXT PRIMARY KEY, content_hash TEXT NOT NULL, "
        "hint TEXT NOT NULL, alternatives TEXT NOT NULL, reason TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS form_exclusions (lexeme_id TEXT NOT NULL REFERENCES lexemes(id), "
        "tense TEXT NOT NULL,person TEXT NOT NULL,source_id TEXT,reason TEXT NOT NULL, "
        "PRIMARY KEY(lexeme_id,tense,person))"
    )
    for row in conn.execute("SELECT lexeme_id,idx,source_id,context FROM senses").fetchall():
        refs = [
            dict(r)
            for r in conn.execute(
                "SELECT list_id,row_key,source_hash FROM source_observations WHERE lexeme_id=? ORDER BY list_id,row_key",
                (row["lexeme_id"],),
            )
        ]
        conn.execute(
            "UPDATE senses SET source_id=?,context=? WHERE lexeme_id=? AND idx=?",
            (
                row["source_id"] or f"legacy:{row['lexeme_id']}:{row['idx']}",
                row["context"]
                or json.dumps(
                    {
                        "status": "legacy meaning; source match not independently verified",
                        "observations": refs,
                    }
                ),
                row["lexeme_id"],
                row["idx"],
            ),
        )
    conn.execute(
        "UPDATE form_prompts SET source_id=(SELECT source_id FROM senses s "
        "WHERE s.lexeme_id=form_prompts.lexeme_id AND active=1 ORDER BY idx LIMIT 1)"
    )
    # New schemas/evidence do not regenerate all finished vocabulary. Re-record
    # fingerprints once. Only incomplete/unsafe verb responses are explicitly reopened.
    conn.execute("UPDATE ai_jobs SET fingerprint=NULL")
    from . import semantics

    for job in conn.execute("SELECT * FROM ai_jobs WHERE kind='verb_prompts' AND status='done'").fetchall():
        payload = json.loads(job["payload"] or "{}")
        requested = {(t, p) for t, ps in payload.get("forms", {}).items() for p in ps}
        prompts = {
            (r["tense"], r["person"])
            for r in conn.execute("SELECT * FROM form_prompts WHERE lexeme_id=?", (job["subject"],))
        }
        lx = conn.execute("SELECT * FROM lexemes WHERE id=?", (job["subject"],)).fetchone()
        sense = semantics.primary(conn, job["subject"])
        unsafe = lx and any(semantics.exclusion(lx, sense, t, p) for t, p in prompts)
        if requested != prompts or unsafe:
            conn.execute(
                "UPDATE ai_jobs SET status='pending',attempts=0,next_retry_at=NULL,error='Revalidate incomplete or unsuitable verb forms' WHERE id=?",
                (job["id"],),
            )
    conn.execute("DELETE FROM metadata WHERE key='notes_generation'")


#: Ordered schema migrations. Index ``i`` upgrades ``user_version`` i → i+1.
MIGRATIONS = [
    _m1_portable_identity,
    _m2_facts_and_audits,
    _m3_lexicon,
    _m4_note_audio,
    _m5_integrity,
    _m6_queue_recovery,
    _m7_meaning_contracts,
]
SCHEMA_VERSION = len(MIGRATIONS)


def init_schema(conn: sqlite3.Connection) -> None:
    """Create a fresh schema, or migrate an existing DB to ``SCHEMA_VERSION``."""
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'entries'").fetchone()
    if not exists:
        conn.executescript(SCHEMA_SQL)
        with transaction(conn):
            _m5_integrity(conn)
            _m6_queue_recovery(conn)
            _m7_meaning_contracts(conn)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
        return
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise ValueError(f"Database schema {version} is newer than supported {SCHEMA_VERSION}")
    if version < SCHEMA_VERSION:
        from .backup import snapshot

        snapshot(f"before migrating to schema v{SCHEMA_VERSION}", connection=conn)
    for i in range(version, SCHEMA_VERSION):
        with transaction(conn):
            MIGRATIONS[i](conn)
            conn.execute(f"PRAGMA user_version = {i + 1}")
        from . import report

        report.detail(f"  migrated database schema to v{i + 1}")
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
    rows = conn.execute("SELECT DISTINCT deck FROM cards ORDER BY deck").fetchall()
    return [r["deck"] for r in rows]
