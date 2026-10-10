import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch
from flashcards.db import init_schema


def database(test_case):
    # Fixture databases must never publish JSONL into the real workspace.
    scratch = tempfile.TemporaryDirectory()
    test_case.addCleanup(scratch.cleanup)
    export_dir = patch("flashcards.lexicon.LEXICON_DIR", Path(scratch.name))
    export_dir.start()
    test_case.addCleanup(export_dir.stop)
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    test_case.addCleanup(conn.close)
    init_schema(conn)
    return conn


def lexeme(conn, lid="x", *, status="ready", pos="noun", lemma="casa", forms=None):
    conn.execute(
        "INSERT INTO lexemes(id,lemma,pos,display,gender,plural,english_plural,status,forms_json,provenance) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            lid,
            lemma,
            pos,
            "la " + lemma if pos == "noun" else lemma,
            "feminine" if pos == "noun" else None,
            "case" if pos == "noun" else None,
            "houses" if pos == "noun" else None,
            status,
            forms,
            "{}",
        ),
    )
    conn.execute(
        "INSERT OR IGNORE INTO lists(id,kind,title,deck,settings) VALUES('test','csv','Test','Italian::Test','{\"shareable\":true}')"
    )
    conn.execute(
        "INSERT INTO list_items(list_id,lexeme_id,rank,raw,hint) VALUES('test',?,1,?,'house')", (lid, lemma)
    )
    conn.commit()


def desired(conn, *, key="vocab:x:0", ready=True):
    import json
    from flashcards.notes import FIELDS

    fields = {f: "" for f in FIELDS}
    fields.update(Key=key, Italian="casa", English="house", Recognition="1", Production="1")
    conn.execute(
        "INSERT INTO v4_notes(key,lexeme_id,card_type,deck,fields_json,fields_hash,tags,sort_order,ready) VALUES(?,?,?,?,?,?,?,?,?)",
        (key, "x", "vocab", "Italian::Test", json.dumps(fields), "h", "fc::owned", 1, ready),
    )
    conn.commit()
    return fields
