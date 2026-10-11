import json
import threading
import unittest
from unittest.mock import patch

from flashcards import lexicon, notes, queue
from .support import database, lexeme

LINE = "Sono andato da Grana in due ore e 37 minuti. Ottima corsa."
WORDS = [
    {"it": "Sono", "en": "I-am"},
    {"it": "andato", "en": "gone"},
    {"it": "da", "en": "from"},
    {"it": "Grana", "en": "Grana"},
    {"it": "in", "en": "in"},
    {"it": "due", "en": "two"},
    {"it": "ore", "en": "hours"},
    {"it": "e", "en": "and"},
    {"it": "37", "en": "37"},
    {"it": "minuti.", "en": "minutes."},
    {"it": "Ottima", "en": "Excellent"},
    {"it": "corsa.", "en": "run."},
]


def film_word(conn, lid, lemma):
    lexeme(conn, lid, lemma=lemma)
    conn.execute(
        "UPDATE list_items SET context=? WHERE lexeme_id=?",
        (json.dumps({"example": f'"{LINE}"', "example_at": "1:49:33"}), lid),
    )
    conn.commit()


class FilmLineTests(unittest.TestCase):
    def test_the_line_on_the_card_loses_its_own_quotes(self):
        self.assertEqual(lexicon.film_line(f'  "{LINE}"\n'), LINE)

    def test_one_job_per_distinct_line_and_none_once_glossed(self):
        c = database(self)
        film_word(c, "a", "corsa")
        film_word(c, "b", "ottimo")  # same line, different word
        with (
            patch.object(queue, "_lexeme_payload", side_effect=lambda conn, lx: {"id": lx["id"]}),
            patch.object(queue.kaikki, "version", return_value="v"),
        ):
            queue.plan(c)
            jobs = c.execute("SELECT subject, payload FROM ai_jobs WHERE kind='film_gloss'").fetchall()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(json.loads(jobs[0]["payload"])["italian"], LINE)
            c.execute(
                "INSERT INTO film_glosses(key, italian, words) VALUES(?,?,?)",
                (lexicon.film_line_key(LINE), LINE, json.dumps(WORDS)),
            )
            c.execute("UPDATE ai_jobs SET status='done' WHERE kind='film_gloss'")
            queue.plan(c)
        self.assertEqual(
            c.execute("SELECT count(*) FROM ai_jobs WHERE kind='film_gloss' AND status='pending'").fetchone()[
                0
            ],
            0,
        )


class FilmGlossAnswerTests(unittest.TestCase):
    def job(self, c):
        key = lexicon.film_line_key(LINE)
        c.execute(
            "INSERT INTO ai_jobs(kind,subject,payload) VALUES('film_gloss',?,?)",
            (key, json.dumps({"id": key, "italian": LINE})),
        )
        c.commit()
        return key, c.execute("SELECT * FROM ai_jobs").fetchone()

    def run_batch(self, c, job, words):
        class Fake:
            def run(self, task, refresh=False, validate_result=None):
                result = {"lines": [{"id": job["subject"], "words": words}]}
                if validate_result:
                    validate_result(result)
                return result

        return queue._run_text_batch(Fake(), "film_gloss", [job], c, threading.Lock())

    def test_a_gloss_that_skips_a_word_is_rejected(self):
        c = database(self)
        _key, job = self.job(c)
        skipped = [w for w in WORDS if w["it"] != "andato"]
        with self.assertRaises(ValueError):
            self.run_batch(c, job, skipped)
        self.assertEqual(c.execute("SELECT count(*) FROM film_glosses").fetchone()[0], 0)

    def test_a_full_gloss_is_saved_and_shown_on_the_card(self):
        c = database(self)
        key, job = self.job(c)
        self.assertEqual(self.run_batch(c, job, WORDS), 1)
        self.assertEqual(json.loads(c.execute("SELECT words FROM film_glosses").fetchone()[0]), WORDS)
        film_word(c, "a", "corsa")
        field = notes._Builder(c).example_field("a")
        self.assertIn("“Sono andato", field)  # outer quotes from the subtitle are gone
        self.assertIn(
            "word for word: I-am gone from Grana in two hours and 37 minutes. Excellent run.", field
        )
        self.assertEqual(key, lexicon.film_line_key(f'"{LINE}"'))


class VersionBumpTests(unittest.TestCase):
    def test_plan_is_stable_and_a_task_version_bump_redoes_hints_and_film_lines(self):
        from dataclasses import replace

        from flashcards import overrides

        c = database(self)
        film_word(c, "a", "corsa")
        lexeme(c, "b", lemma="gara")
        for lid in ("a", "b"):  # two words, one English prompt → a hint group
            overrides.save_senses(c, lid, [{"prompt": "race", "hint": "given"}], "claude:test")
        c.commit()

        def pending(kind):
            return c.execute(
                "SELECT count(*) FROM ai_jobs WHERE kind=? AND status='pending'", (kind,)
            ).fetchone()[0]

        with (
            patch.object(queue, "_lexeme_payload", side_effect=lambda conn, lx: {"id": lx["id"]}),
            patch.object(queue.kaikki, "version", return_value="v"),
        ):
            queue.plan(c)
            self.assertEqual((pending("film_gloss"), pending("disambiguate")), (1, 1))
            c.execute(
                "INSERT INTO film_glosses(key, italian, words) VALUES(?,?,?)",
                (lexicon.film_line_key(LINE), LINE, json.dumps(WORDS)),
            )
            c.execute("UPDATE ai_jobs SET status='done'")
            queue.plan(c)
            queue.plan(c)
            self.assertEqual((pending("film_gloss"), pending("disambiguate")), (0, 0))
            bumped = replace(queue.settings, ai=replace(queue.settings.ai, task_version="999"))
            with patch.object(queue, "settings", bumped):
                queue.plan(c)
                # Words and film lines are redone first; hints depend on the new prompts.
                self.assertEqual((pending("lexeme_enrich"), pending("film_gloss")), (2, 1))
                c.execute("UPDATE lexemes SET status='ready'")  # the words come back from Claude
                c.execute("UPDATE ai_jobs SET status='done' WHERE kind='lexeme_enrich'")
                queue.plan(c)
                self.assertEqual(pending("disambiguate"), 1)
