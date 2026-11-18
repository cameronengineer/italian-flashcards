"""Upgrading the old database layout (schema v13, with the v3 tables)."""

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from flashcards import db, lexicon, paths
from flashcards.commands import doctor

OLD_SCHEMA = (Path(__file__).parent / "fixtures" / "schema_v13.sql").read_text(encoding="utf-8")


def old_database():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(OLD_SCHEMA)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO lexemes(id,lemma,pos,display,image_key,status) VALUES('x','casa','noun','la casa','casa','ready')"
    )
    conn.execute("INSERT INTO lists(id,kind,title,deck) VALUES('l','csv','L','Italian::L')")
    conn.execute("INSERT INTO list_items(list_id,lexeme_id,rank) VALUES('l','x',1)")
    conn.execute("INSERT INTO senses(lexeme_id,idx,prompt) VALUES('x',0,'house')")
    conn.execute(
        "INSERT INTO entries(id,source_path,natural_id,mode,deck,italian,english,singular) "
        "VALUES('e','a.csv','casa','noun','Italian - Old','una casa','house','la casa vecchia')"
    )
    conn.execute("INSERT INTO entry_lexeme VALUES('e','x')")
    conn.execute(
        "INSERT INTO cards(entry_id,natural_key,direction,deck,front_text,back_highlight,sort_order,guid) "
        "VALUES('e','gloss:e','it_to_en','Italian - Old','casa','house',1,'g')"
    )
    conn.execute("INSERT INTO identity(key,guid,kind,note_id) VALUES('vocab:x:0','','v4',7)")
    conn.execute("INSERT INTO identity(key,guid,kind) VALUES('gloss:e|it_to_en','g','legacy')")
    conn.execute("INSERT INTO adoptions VALUES('vocab:x:0','Recognition',5,'Test')")
    conn.execute("INSERT INTO retirements(key,reason) VALUES('gloss:e|it_to_en','list removed')")
    conn.execute("INSERT INTO retirements(key,reason) VALUES('vocab:gone:0','list removed')")
    conn.execute("INSERT INTO metadata VALUES('legacy_index','{}')")
    conn.commit()
    return conn


def layout(conn):
    return {
        r["name"]: [c[1] for c in conn.execute(f"PRAGMA table_info({r['name']})")]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


class UpgradeTests(unittest.TestCase):
    def upgraded(self, images=()):
        conn = old_database()
        self.addCleanup(conn.close)
        with (
            patch("flashcards.backup.snapshot"),
            patch.object(lexicon, "image_exists", side_effect=lambda key: key in images),
            patch.object(
                lexicon,
                "existing_image",
                side_effect=lambda *texts: next((t for t in texts if t in images), None),
            ),
        ):
            db.init_schema(conn)
        return conn

    def test_v3_tables_go_and_everything_else_stays(self):
        conn = self.upgraded()
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        tables = set(layout(conn))
        self.assertFalse(
            tables
            & {
                "entries",
                "cards",
                "verb_forms",
                "noun_phrases",
                "entry_lexeme",
                "legacy_notes",
                "adoptions",
                "identity_aliases",
            }
        )
        self.assertEqual(conn.execute("SELECT prompt FROM senses").fetchone()[0], "house")
        self.assertEqual(
            [tuple(r) for r in conn.execute("SELECT key,note_id FROM identity")], [("vocab:x:0", 7)]
        )
        self.assertEqual([r[0] for r in conn.execute("SELECT key FROM retirements")], ["vocab:gone:0"])
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_upgraded_layout_equals_a_new_database(self):
        conn = self.upgraded()
        fresh = sqlite3.connect(":memory:")
        fresh.row_factory = sqlite3.Row
        self.addCleanup(fresh.close)
        db.init_schema(fresh)
        upgraded = layout(conn)
        self.assertEqual(upgraded, layout(fresh))
        triggers = "SELECT name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
        self.assertEqual(
            [tuple(r) for r in conn.execute(triggers)], [tuple(r) for r in fresh.execute(triggers)]
        )

    def test_image_made_for_a_v3_entry_is_found_one_last_time(self):
        # The root's own key has no image, but the v3 entry's text does.
        conn = self.upgraded(images={"la casa vecchia"})
        self.assertEqual(conn.execute("SELECT image_key FROM lexemes").fetchone()[0], "la casa vecchia")

    def test_existing_image_key_is_never_changed(self):
        conn = self.upgraded(images={"casa", "la casa vecchia"})
        self.assertEqual(conn.execute("SELECT image_key FROM lexemes").fetchone()[0], "casa")

    def test_older_layouts_are_refused_with_instructions(self):
        conn = old_database()
        self.addCleanup(conn.close)
        conn.execute("PRAGMA user_version = 9")
        with self.assertRaisesRegex(ValueError, "3e6520e"):
            db.init_schema(conn)

    def test_old_status_value_is_no_longer_accepted(self):
        conn = self.upgraded()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE lexemes SET status='needs_review'")


class DoctorBeforeUpgradeTests(unittest.TestCase):
    """``doctor`` straight after ``git pull``, before ``./run.sh`` has upgraded."""

    def saved(self, version):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "database.sqlite"
        conn = old_database()
        conn.execute(f"PRAGMA user_version = {version}")
        with closing(sqlite3.connect(path)) as target:
            conn.backup(target)
        conn.close()
        return path

    def doctor(self, path):
        with (
            patch.object(paths, "DB_PATH", path),
            redirect_stdout(io.StringIO()) as out,
        ):
            code = doctor.run(check_media=False)
        return code, json.loads(out.getvalue())

    def test_older_schema_is_reported_as_due_and_left_alone(self):
        path = self.saved(13)
        before = path.read_bytes()
        code, result = self.doctor(path)
        self.assertEqual(code, 0)
        self.assertEqual(result["data"]["status"], "upgrade due")
        self.assertIn("./run.sh", result["data"]["detail"])
        self.assertEqual(path.read_bytes(), before)

    def test_newer_or_too_old_schema_fails(self):
        for version in (db.SCHEMA_VERSION + 1, 9):
            code, result = self.doctor(self.saved(version))
            self.assertEqual(code, 1, version)
            self.assertEqual(result["data"]["status"], "error", version)


if __name__ == "__main__":
    unittest.main()
