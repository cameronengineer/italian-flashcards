import json
import unittest
from unittest.mock import patch

from flashcards import codex, overrides, queue
from flashcards.settings import asset_limit
from .support import database, lexeme


class AssetLimitTests(unittest.TestCase):
    def test_limit_semantics(self):
        self.assertEqual(asset_limit(0), 0)
        self.assertEqual(asset_limit(3), 3)
        self.assertIsNone(asset_limit(-1))
        self.assertIsNone(asset_limit(None))

    def _image_jobs(self, conn, n):
        for i in range(n):
            lexeme(conn, lid=f"x{i}", lemma=f"casa{i}")
            queue._enqueue(conn, "image", f"x{i}", i, {"image_key": f"casa{i}"})
        conn.commit()

    def test_zero_images_never_starts_codex(self):
        c = database(self)
        self._image_jobs(c, 2)
        with (
            patch.object(codex, "logged_in") as login,
            patch.object(queue, "_run_image") as run,
            patch.object(queue, "plan"),
            patch("flashcards.lexicon.export_jsonl"),
        ):
            queue.drain(c, kinds=["image"], image_limit=0)
        login.assert_not_called()
        run.assert_not_called()

    def test_image_limit_caps_codex_calls(self):
        c = database(self)
        self._image_jobs(c, 3)
        with (
            patch.object(codex, "logged_in", return_value=True),
            patch.object(queue, "_run_image", return_value=True) as run,
            patch.object(queue, "plan"),
            patch("flashcards.lexicon.export_jsonl"),
        ):
            result = queue.drain(c, kinds=["image"], image_limit=2)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result["image"]["done"], 2)
        statuses = sorted(r[0] for r in c.execute("SELECT status FROM ai_jobs"))
        self.assertEqual(statuses, ["done", "done", "pending"])


class SenseIdentityTests(unittest.TestCase):
    def _active(self, conn):
        return [
            tuple(r)
            for r in conn.execute(
                "SELECT idx, prompt FROM senses WHERE lexeme_id='x' AND active=1 ORDER BY idx"
            )
        ]

    def test_reworded_prompt_keeps_its_note(self):
        c = database(self)
        lexeme(c)
        overrides.save_senses(
            c, "x", [{"prompt": "house", "source_id": "dictionary:dwelling"}], "claude:test"
        )
        overrides.save_senses(c, "x", [{"prompt": "home", "source_id": "dictionary:dwelling"}], "claude:test")
        self.assertEqual(self._active(c), [(0, "home")])

    def test_unchanged_prompts_keep_identity_when_reordered(self):
        c = database(self)
        lexeme(c)
        overrides.save_senses(c, "x", [{"prompt": "house"}, {"prompt": "family"}], "claude:test")
        overrides.save_senses(
            c, "x", [{"prompt": "family"}, {"prompt": "house"}, {"prompt": "home"}], "claude:test"
        )
        self.assertEqual(self._active(c), [(0, "house"), (1, "family"), (2, "home")])

    def test_removed_sense_is_archived_and_duplicates_collapse(self):
        c = database(self)
        lexeme(c)
        overrides.save_senses(c, "x", [{"prompt": "house"}, {"prompt": "family"}], "claude:test")
        overrides.save_senses(c, "x", [{"prompt": "family"}, {"prompt": "Family "}], "claude:test")
        self.assertEqual(self._active(c), [(1, "family")])
        archived = c.execute("SELECT prompt FROM senses WHERE lexeme_id='x' AND active=0").fetchall()
        self.assertEqual([r[0] for r in archived], ["house"])


class EtymologyChainTests(unittest.TestCase):
    def test_chain_is_fetched_once_and_does_not_reopen_the_job(self):
        import threading

        c = database(self)
        lexeme(c)
        c.execute("UPDATE lexemes SET in_kaikki=1")
        payload = {"id": "x", "italian": "la casa", "lemma": "casa", "pos": "noun"}
        queue._enqueue(c, "lexeme_enrich", "x", 1, payload)
        c.commit()
        job = c.execute("SELECT * FROM ai_jobs").fetchone()
        lock = threading.Lock()

        def source(*args):
            self.assertTrue(lock.acquire(blocking=False), "network work held the shared database lock")
            lock.release()
            return "From Latin casa, hut"

        with (
            patch.object(queue, "_kaikki_entry", return_value={"word": "casa"}),
            patch.object(queue.kaikki, "etymology_source", return_value=("la", "casa")),
            patch.object(queue.kaikki, "source_etymology", side_effect=source) as fetch,
            patch.object(queue.kaikki, "glosses", return_value=[]),
            patch.object(queue.kaikki, "version", return_value="v1"),
        ):
            [job] = queue._add_etymology_chains(c, [job], lock)
            self.assertEqual(json.loads(job["payload"])["etymology_source"], "From Latin casa, hut")
            queue._add_etymology_chains(c, [job], lock)
            fetch.assert_called_once()
            c.execute("UPDATE ai_jobs SET status='done'")
            rebuilt = {
                **queue._lexeme_payload(c, c.execute("SELECT * FROM lexemes").fetchone()),
                "dictionary_version": "v1",
            }
            self.assertFalse(queue._enqueue(c, "lexeme_enrich", "x", 1, rebuilt))
        self.assertEqual(c.execute("SELECT status FROM ai_jobs").fetchone()[0], "done")


class VerbPromptTests(unittest.TestCase):
    def test_omitted_duplicate_and_unrequested_forms_are_rejected(self):
        import threading

        c = database(self)
        lexeme(c, pos="verb", lemma="dovere")
        payload = {
            "id": "x",
            "infinitive": "dovere",
            "meaning": "must",
            "forms": {"presente": {"io": "devo"}, "imperativo": {"tu": "devi"}},
        }
        c.execute(
            "INSERT INTO ai_jobs(kind,subject,payload) VALUES('verb_prompts','x',?)", (json.dumps(payload),)
        )
        c.commit()
        job = c.execute("SELECT * FROM ai_jobs").fetchone()

        class Fake:
            def run(self, task, refresh=False, validate_result=None):
                return {
                    "verbs": [
                        {
                            "id": "x",
                            "prompts": [
                                {"tense": "presente", "person": "io", "english": "I must"},
                                {"tense": "presente", "person": "io", "english": "I have to"},
                                {"tense": "futuro_semplice", "person": "io", "english": "I will have to"},
                            ],
                        }
                    ]
                }

        with self.assertRaises(ValueError):
            queue._run_text_batch(Fake(), "verb_prompts", [job], c, threading.Lock())
        self.assertEqual(c.execute("SELECT count(*) FROM form_prompts").fetchone()[0], 0)
        self.assertEqual(c.execute("SELECT status FROM ai_jobs").fetchone()[0], "pending")
