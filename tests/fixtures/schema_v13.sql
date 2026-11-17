-- The database layout at schema version 13 (the last one with the v3 tables),
-- as the code at commit 3e6520e created it. Used to test the upgrade.
CREATE TABLE adoptions (
    key TEXT NOT NULL, direction TEXT NOT NULL, note_id INTEGER NOT NULL,
    profile TEXT NOT NULL, PRIMARY KEY(key, direction, profile)
);
CREATE TABLE ai_cache (
    cache_key      TEXT PRIMARY KEY,
    response_json  TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE ai_jobs (
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
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, fingerprint TEXT, force_refresh INTEGER NOT NULL DEFAULT 0, owner TEXT, started_at TEXT, next_retry_at TEXT,
    UNIQUE (kind, subject)
);
CREATE TABLE asset_manifest (
    filename TEXT PRIMARY KEY, subject TEXT NOT NULL, kind TEXT NOT NULL,
    spec_hash TEXT NOT NULL, checksum TEXT NOT NULL, metadata TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE audits (
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
CREATE TABLE card_knowledge (
    key TEXT NOT NULL, direction TEXT NOT NULL, card_id INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('unknown','learning','known')),
    interval INTEGER NOT NULL, lapses INTEGER NOT NULL, reviews INTEGER NOT NULL,
    suspended INTEGER NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(key, direction, card_id)
);
CREATE TABLE cards (
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
CREATE TABLE cue_fixes (lexeme_id TEXT PRIMARY KEY, prompt TEXT NOT NULL, source_hint TEXT NOT NULL DEFAULT '', english TEXT NOT NULL, hint TEXT);
CREATE TABLE entries (
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
CREATE TABLE "entry_lexeme" (
    entry_id    TEXT PRIMARY KEY,
    lexeme_id   TEXT NOT NULL
, FOREIGN KEY (entry_id) REFERENCES entries(id) ON DELETE CASCADE, FOREIGN KEY (lexeme_id) REFERENCES lexemes(id) ON DELETE RESTRICT);
CREATE TABLE film_glosses (
    key         TEXT PRIMARY KEY,           -- md5 of the line as shown (lexicon.film_line_key)
    italian     TEXT NOT NULL,
    words       TEXT NOT NULL,              -- JSON [{"it": …, "en": …}], in line order
    model       TEXT,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE form_exclusions (lexeme_id TEXT NOT NULL REFERENCES lexemes(id), tense TEXT NOT NULL,person TEXT NOT NULL,source_id TEXT,reason TEXT NOT NULL, PRIMARY KEY(lexeme_id,tense,person));
CREATE TABLE "form_prompts" (
    lexeme_id   TEXT NOT NULL,
    tense       TEXT NOT NULL,
    person      TEXT NOT NULL,
    english     TEXT NOT NULL, source_id TEXT,
    PRIMARY KEY (lexeme_id, tense, person)
, FOREIGN KEY (lexeme_id) REFERENCES lexemes(id) ON DELETE CASCADE);
CREATE TABLE identity (
    key          TEXT PRIMARY KEY,
    guid         TEXT NOT NULL,
    kind         TEXT NOT NULL,             -- v4 | legacy
    first_seen   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
, note_id INTEGER, profile TEXT, card_ids TEXT);
CREATE TABLE identity_aliases (
    old_key TEXT PRIMARY KEY, new_key TEXT NOT NULL, reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE legacy_notes (
    note_id     INTEGER PRIMARY KEY,
    sort_key    TEXT NOT NULL,
    cards       TEXT NOT NULL               -- JSON card ids
);
CREATE TABLE lexemes (
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
    status           TEXT NOT NULL DEFAULT 'new',   -- new | ready
    issues           TEXT,                  -- verification disagreements (JSON)
    provenance       TEXT,                  -- field → source (JSON)
    updated_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
, source_hash TEXT, ignored TEXT, key_list TEXT, card_number TEXT, card_english TEXT);
CREATE TABLE "list_items" (
    list_id     TEXT NOT NULL,
    lexeme_id   TEXT NOT NULL,
    rank        INTEGER NOT NULL,           -- order within the list
    raw         TEXT,                       -- the row as written in the source
    hint        TEXT,                       -- the source's own English gloss
    context     TEXT,                       -- JSON: count, first_seen, example (movies)
    PRIMARY KEY (list_id, lexeme_id)
, FOREIGN KEY (list_id) REFERENCES lists(id) ON DELETE CASCADE, FOREIGN KEY (lexeme_id) REFERENCES lexemes(id) ON DELETE RESTRICT);
CREATE TABLE lists (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    deck        TEXT NOT NULL,
    settings    TEXT
);
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE mistakes (
    id          TEXT PRIMARY KEY,           -- md5 of the Italian chunk
    italian     TEXT NOT NULL,
    english     TEXT NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE mnemonics (
    key         TEXT PRIMARY KEY,
    mnemonic    TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE noun_phrases (
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
CREATE TABLE overrides (
    lexeme_id TEXT NOT NULL REFERENCES lexemes(id), field TEXT NOT NULL,
    value_json TEXT NOT NULL, author TEXT NOT NULL DEFAULT 'human', reason TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (lexeme_id, field)
);
CREATE TABLE retirements (
    key TEXT PRIMARY KEY, reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE "senses" (
    lexeme_id   TEXT NOT NULL,
    idx         INTEGER NOT NULL,
    prompt      TEXT NOT NULL,              -- English side of the card
    hint        TEXT,                       -- short disambiguator
    register    TEXT,                       -- formal / colloquial / vulgar …
    note        TEXT,                       -- usage note (back of card)
    also        TEXT,                       -- other accepted Italian answers
    provenance  TEXT, active INTEGER NOT NULL DEFAULT 1, source_id TEXT, context TEXT, features TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (lexeme_id, idx)
, FOREIGN KEY (lexeme_id) REFERENCES lexemes(id) ON DELETE CASCADE);
CREATE TABLE source_observations (
    list_id TEXT NOT NULL REFERENCES lists(id) ON DELETE CASCADE, row_key TEXT NOT NULL,
    lexeme_id TEXT NOT NULL REFERENCES lexemes(id), raw TEXT NOT NULL, payload TEXT NOT NULL,
    source_hash TEXT NOT NULL, generation TEXT NOT NULL, PRIMARY KEY(list_id, row_key)
);
CREATE TABLE sync_runs (
    id TEXT PRIMARY KEY, profile TEXT NOT NULL, plan_json TEXT NOT NULL,
    status TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE v4_notes (
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
CREATE TABLE verb_forms (
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
CREATE TABLE word_facts (
    word           TEXT PRIMARY KEY,      -- facts.normalise(word)
    has_fact       INTEGER NOT NULL,
    kind           TEXT,                  -- tasks.FACT_KINDS
    fact           TEXT,
    confidence     REAL,
    model          TEXT,
    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
, checked TEXT, checked_text TEXT);
CREATE INDEX idx_ai_jobs_status ON ai_jobs(status, kind, priority);
CREATE INDEX idx_cards_deck ON cards(deck);
CREATE INDEX idx_cards_entry ON cards(entry_id);
CREATE INDEX idx_cards_sort ON cards(deck, sort_order);
CREATE INDEX idx_entries_deck ON entries(deck);
CREATE INDEX idx_entries_mode ON entries(mode);
CREATE UNIQUE INDEX idx_entries_source_mode_natural
    ON entries(source_path, mode, natural_id);
CREATE UNIQUE INDEX idx_lexemes_lemma_pos ON lexemes(lemma, pos);
CREATE INDEX idx_list_items_lexeme ON list_items(lexeme_id);
CREATE INDEX idx_noun_phrases_entry ON noun_phrases(entry_id);
CREATE INDEX idx_source_observations_lexeme ON source_observations(lexeme_id,list_id,row_key);
CREATE INDEX idx_verb_forms_entry ON verb_forms(entry_id);
CREATE TRIGGER validate_ai_jobs_INSERT BEFORE INSERT ON ai_jobs WHEN NEW.status NOT IN ('pending','running','done','failed','cancelled') BEGIN SELECT RAISE(ABORT, 'Invalid ai_jobs status'); END;
CREATE TRIGGER validate_ai_jobs_UPDATE BEFORE UPDATE ON ai_jobs WHEN NEW.status NOT IN ('pending','running','done','failed','cancelled') BEGIN SELECT RAISE(ABORT, 'Invalid ai_jobs status'); END;
CREATE TRIGGER validate_lexemes_INSERT BEFORE INSERT ON lexemes WHEN NEW.status NOT IN ('new','ready','needs_review') BEGIN SELECT RAISE(ABORT, 'Invalid lexemes status'); END;
CREATE TRIGGER validate_lexemes_UPDATE BEFORE UPDATE ON lexemes WHEN NEW.status NOT IN ('new','ready','needs_review') BEGIN SELECT RAISE(ABORT, 'Invalid lexemes status'); END;
PRAGMA user_version = 13;
