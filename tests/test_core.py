import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flashcards import lists, lexicon, overrides, db, notes, quality
from flashcards.domain import ListDef, Item
from .support import database, lexeme, desired


class InputTests(unittest.TestCase):
    def test_article_boundaries(self):
        for word in ("lavoro", "uno", "una", "lei", "ics", "invece", "lasciare"):
            self.assertEqual(lists.split_article(word), (None, word))
        for text, article, word in [
            ("il cane", "il", "cane"),
            ("uno studente", "uno", "studente"),
            ("una studentessa", "una", "studentessa"),
            ("l’acqua", "l'", "acqua"),
        ]:
            self.assertEqual(lists.split_article(text), (article, word))

    def test_explicit_pos_and_variants(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "input.csv"
            p.write_text("italian,english\ni lunga,J\nvu / vi,V\nics,X\n")
            rows = lists.read(ListDef("letters", "csv", "Letters", "Italian::Letters", p, pos="letter"))
            self.assertEqual(
                [(r.lemma, r.pos) for r in rows], [("i lunga", "letter"), ("vu", "letter"), ("ics", "letter")]
            )

    def test_bad_csv_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bad.csv"
            for content in ("wrong,english\nx,y\n", "italian,english\nx,y,z\n", "italian,english\nx\n"):
                p.write_text(content)
                with self.assertRaises(ValueError):
                    lists._rows(p)

    def test_plan_validation(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "plan.toml"
            for body in (
                "new_cards_per_day = -1",
                "unexpected = true",
                '[cards]\npatterns = "false"',
                '[[priority]]\nlist="x"\nby="2026-99-99"',
            ):
                p.write_text(body)
                with self.assertRaises(ValueError):
                    lists.load_plan(p)


class IntegrityTests(unittest.TestCase):
    def test_foreign_keys_and_status(self):
        import sqlite3

        c = database(self)
        for sql in (
            "INSERT INTO senses(lexeme_id,idx,prompt) VALUES('missing',0,'bad')",
            "INSERT INTO lexemes(id,lemma,pos,status) VALUES('bad','bad','noun','typo')",
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute(sql)
            c.rollback()

    def test_future_schema_rejected(self):
        c = database(self)
        c.execute("PRAGMA user_version=999")
        with self.assertRaises(ValueError):
            db.init_schema(c)

    def test_human_overrides_and_sense_order(self):
        c = database(self)
        lexeme(c)
        overrides.set_override(c, "x", "display", "HUMAN")
        overrides.set_override(c, "x", "english_plural", "human houses")
        overrides.set_override(c, "x", "senses", [{"prompt": "home"}, {"prompt": "house"}])
        overrides.apply(c)
        c.execute("UPDATE lexemes SET display='generated'")
        overrides.apply(c)
        self.assertEqual(c.execute("SELECT display FROM lexemes").fetchone()[0], "HUMAN")
        first = {r["prompt"]: r["idx"] for r in c.execute("SELECT * FROM senses")}
        overrides.save_senses(c, "x", [{"prompt": "house"}, {"prompt": "home"}], "human")
        self.assertEqual(first, {r["prompt"]: r["idx"] for r in c.execute("SELECT * FROM senses")})
        with self.assertRaises(ValueError):
            overrides.set_override(c, "x", "unknown", "x")

    def test_sync_removes_memberships_and_preserves_overrides(self):
        c = database(self)
        lexeme(c)
        overrides.set_override(c, "x", "display", "HUMAN")
        overrides.apply(c)
        c.commit()
        l = ListDef("replacement", "numbers", "Replacement", "Italian::Replacement", None)
        rec = {
            "id": "x",
            "lemma": "casa",
            "pos": "noun",
            "display": "GENERATED",
            "provenance": "{}",
            "in_kaikki": 1,
            "irregular": 0,
        }
        with (
            tempfile.TemporaryDirectory() as d,
            patch("flashcards.paths.PROJECT_ROOT", Path(d)),
            patch.object(lists, "load", return_value=[l]),
            patch.object(lists, "validate", return_value=[]),
            patch.object(lists, "read", return_value=[Item("casa", "casa", "noun")]),
            patch.object(lexicon.kaikki, "available", return_value=True),
            patch.object(lexicon, "_resolve", return_value=rec),
        ):
            lexicon.sync(c)
            lexicon.sync(c)
        self.assertEqual(c.execute("SELECT display FROM lexemes").fetchone()[0], "HUMAN")
        self.assertEqual(c.execute("SELECT count(*) FROM list_items WHERE list_id='test'").fetchone()[0], 0)
        self.assertEqual(list(c.execute("PRAGMA foreign_key_check")), [])

    def test_failed_staging_does_not_change_memberships(self):
        c = database(self)
        lexeme(c)
        with (
            patch.object(lists, "load", return_value=[ListDef("x", "csv", "X", "Italian::X", None)]),
            patch.object(lists, "validate", return_value=[]),
            patch.object(lists, "read", side_effect=ValueError("bad source")),
            patch.object(lexicon.kaikki, "available", return_value=True),
        ):
            with self.assertRaises(ValueError):
                lexicon.sync(c)
        self.assertEqual(c.execute("SELECT count(*) FROM list_items").fetchone()[0], 1)

    def test_disputed_derivative_and_failed_audit_are_blocked(self):
        c = database(self)
        lexeme(
            c,
            status="needs_review",
            pos="verb",
            lemma="parlare",
            forms=json.dumps({"forms": {"presente": {"io": "parlo"}}}),
        )
        c.execute(
            "INSERT INTO form_prompts(lexeme_id,tense,person,english) VALUES('x','presente','io','I speak')"
        )
        c.commit()
        b = notes._Builder(c)
        with patch("flashcards.notes.full_form_verbs", return_value={"x"}):
            b.verb_form_notes()
        self.assertFalse(b.notes[0]["ready"])
        f = desired(c)
        c.execute("UPDATE lexemes SET status='ready'")
        c.execute(
            "INSERT INTO audits(natural_key,direction,card_hash,verdict) VALUES(?, 'note', ?, 'fail')",
            (f["Key"], quality.note_hash(f)),
        )
        self.assertIn("failed audit", quality.reasons(c, f["Key"], "x", f))

    def test_missing_image_blocks_by_default(self):
        c = database(self)
        lexeme(c)
        overrides.save_senses(c, "x", [{"prompt": "house"}], "human")
        c.commit()
        b = notes._Builder(c)
        b.vocab_and_phrases()
        self.assertFalse(b.notes[0]["ready"])
        self.assertIn("image", b.notes[0]["blocked_by"])

    def test_optional_images_do_not_block_valid_content(self):
        c = database(self)
        lexeme(c)
        overrides.save_senses(c, "x", [{"prompt": "house"}], "human")
        c.execute("""UPDATE lists SET settings='{"image_policy":"optional"}'""")
        c.commit()
        b = notes._Builder(c)
        b.vocab_and_phrases()
        self.assertTrue(b.notes[0]["ready"])
        self.assertEqual(b.notes[0]["fields"]["Image"], "")
