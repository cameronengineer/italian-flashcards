"""Regression coverage for the stalled overnight run; no external providers."""

import json
import threading
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch

from flashcards import ai, cli, db, queue
from .support import database, lexeme


def phrase_result(ids):
    return {
        "phrases": [
            {"id": lid, "prompt": "hello", "hint": "", "note": "", "verified": True, "issues": []}
            for lid in ids
        ]
    }


def task_input(task):
    return json.loads(task.prompt.split("Input:\n", 1)[1])


class CacheValidationTests(unittest.TestCase):
    def job(self, c):
        lexeme(c, pos="phrase")
        queue._enqueue(c, "phrase_enrich", "x", 0, {"id": "x", "italian": "ciao"})
        c.commit()
        return c.execute("SELECT * FROM ai_jobs").fetchone()

    def test_wrong_ids_in_old_cache_are_refetched(self):
        c = database(self)
        job = self.job(c)
        task = queue.PHRASE_ENRICH.task({"phrases": [json.loads(job["payload"])]})
        client = ai.AI(c)
        client._cache_put(task.cache_key(), phrase_result(["foreign"]))
        with patch.object(ai, "_call", return_value=phrase_result(["x"])) as provider:
            self.assertEqual(queue._run_text_batch(client, "phrase_enrich", [job], c, threading.Lock()), 1)
        provider.assert_called_once()
        self.assertEqual(client._cache_get(task.cache_key())["phrases"][0]["id"], "x")
        self.assertEqual(c.execute("SELECT status FROM ai_jobs").fetchone()[0], "done")

    def test_bad_fresh_response_is_not_cached_or_applied(self):
        c = database(self)
        job = self.job(c)
        client = ai.AI(c)
        with patch.object(ai, "_call", return_value=phrase_result(["foreign"])):
            with self.assertRaises(ai.AIError):
                queue._run_text_batch(client, "phrase_enrich", [job], c, threading.Lock())
        self.assertEqual(c.execute("SELECT count(*) FROM ai_cache").fetchone()[0], 0)
        self.assertEqual(c.execute("SELECT count(*) FROM senses").fetchone()[0], 0)

    def test_corrupt_json_cache_is_a_miss(self):
        c = database(self)
        client = ai.AI(c)
        task = ai.Task("test", "system", "prompt", schema={"type": "object"})
        for value in ("{broken", "null"):
            c.execute(
                "INSERT OR REPLACE INTO ai_cache(cache_key,response_json) VALUES(?,?)",
                (task.cache_key(), value),
            )
            c.commit()
            with patch.object(ai, "_call", return_value={}) as provider:
                self.assertEqual(client.run(task), {})
                provider.assert_called_once()

    def test_successful_cache_avoids_provider(self):
        c = database(self)
        job = self.job(c)
        task = queue.PHRASE_ENRICH.task({"phrases": [json.loads(job["payload"])]})
        client = ai.AI(c)
        client._cache_put(task.cache_key(), phrase_result(["x"]))
        with patch.object(ai, "_call") as provider:
            queue._run_text_batch(client, "phrase_enrich", [job], c, threading.Lock())
        provider.assert_not_called()

    def test_missing_payload_fails_before_provider(self):
        c = database(self)
        job = self.job(c)
        c.execute("UPDATE ai_jobs SET payload=NULL")
        job = c.execute("SELECT * FROM ai_jobs").fetchone()
        with patch.object(ai, "_call") as provider:
            with self.assertRaisesRegex(ValueError, "has no input"):
                queue._run_text_batch(ai.AI(c), "phrase_enrich", [job], c, threading.Lock())
        provider.assert_not_called()


class QueueRecoveryTests(unittest.TestCase):
    def test_status_does_not_take_writer_lock_or_mutate_database(self):
        c = database(self)
        c.execute("INSERT INTO ai_jobs(kind,subject,status) VALUES('phrase_enrich','x','running')")
        c.commit()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "status.sqlite"
            target = db.connect(path)
            c.backup(target)
            target.close()
            before = path.read_bytes()
            with (
                patch("flashcards.paths.DB_PATH", path),
                patch("flashcards.runtime.writer_lock") as lock,
                patch.object(queue, "plan") as plan,
                patch.object(cli, "_conn") as mutable,
            ):
                self.assertEqual(cli.main(["jobs"]), 0)
                lock.assert_not_called()
                plan.assert_not_called()
                mutable.assert_not_called()
            self.assertEqual(before, path.read_bytes())

    def test_v5_migration_recovers_only_known_bugs_once(self):
        c = database(self)
        messages = [
            "the JSON object must be str, bytes or bytearray, not NoneType",
            "Provider must return exactly every requested verb form",
            "Provider must return exactly the requested IDs once each",
            "unrelated failure",
        ]
        for i, message in enumerate(messages):
            c.execute(
                "INSERT INTO ai_jobs(kind,subject,status,attempts,error,next_retry_at) "
                "VALUES('verb_prompts',?,'failed',3,?,'2099-01-01')",
                (str(i), message),
            )
        c.execute("PRAGMA user_version=5")
        c.commit()
        with patch("flashcards.backup.snapshot"):
            db.init_schema(c)
        self.assertEqual(
            c.execute(
                "SELECT count(*) FROM ai_jobs WHERE status='pending' AND attempts=0 AND next_retry_at IS NULL"
            ).fetchone()[0],
            3,
        )
        c.execute("UPDATE ai_jobs SET status='failed'")
        c.commit()
        db.init_schema(c)
        self.assertEqual(c.execute("SELECT count(*) FROM ai_jobs WHERE status='pending'").fetchone()[0], 0)
        index = list(c.execute("PRAGMA index_list(source_observations)"))
        self.assertIn("idx_source_observations_lexeme", [r[1] for r in index])

    def test_plan_repairs_ready_verb_and_cancels_unready_verb(self):
        c = database(self)
        lexeme(c, lid="ready", pos="verb", lemma="andare")
        lexeme(c, lid="new", pos="verb", lemma="venire", status="new")
        for lid in ("ready", "new"):
            c.execute("INSERT INTO ai_jobs(kind,subject) VALUES('verb_prompts',?)", (lid,))
        c.commit()
        with (
            patch(
                "flashcards.planning.card_plan",
                return_value={"roots": {"ready", "new"}, "verbs": {"ready", "new"}},
            ),
            patch.object(queue, "verb_forms", return_value={"presente": {"io": ("vado", "vado")}}),
            patch.object(queue, "image_exists", return_value=True),
        ):
            queue.plan(c)
        rows = {r["subject"]: r for r in c.execute("SELECT * FROM ai_jobs WHERE kind='verb_prompts'")}
        self.assertEqual(rows["new"]["status"], "cancelled")
        self.assertEqual(json.loads(rows["ready"]["payload"])["forms"], {"presente": {"io": "vado"}})
        self.assertEqual(rows["ready"]["status"], "pending")


class ConcurrentQueueTests(unittest.TestCase):
    def jobs(self, c, n):
        for i in range(n):
            lid = f"x{i}"
            lexeme(c, lid=lid, pos="phrase", lemma=lid)
            queue._enqueue(c, "phrase_enrich", lid, i, {"id": lid, "italian": lid})
        c.commit()

    def test_calls_overlap_and_batch_limit_is_exact(self):
        c = database(self)
        self.jobs(c, 4)
        barrier = threading.Barrier(3, timeout=3)
        threads = set()

        def provider(task):
            threads.add(threading.get_ident())
            barrier.wait()  # fails if the queue accidentally becomes sequential
            return phrase_result([p["id"] for p in task_input(task)["phrases"]])

        config = replace(queue.settings, ai=replace(queue.settings.ai, concurrency=3))
        with (
            patch.object(queue, "settings", config),
            patch.object(queue, "plan") as plan,
            patch.dict(queue.BATCH, {"phrase_enrich": 1}),
            patch.object(ai, "_call", side_effect=provider),
        ):
            result = queue.drain(c, kinds=["phrase_enrich"], max_batches=3)
        self.assertEqual(result, {"phrase_enrich": {"done": 3, "failed": 0}})
        self.assertEqual(len(threads), 3)
        self.assertEqual(plan.call_count, 2)  # startup + once per wave, not once per batch
        self.assertEqual(
            [r[0] for r in c.execute("SELECT status FROM ai_jobs ORDER BY subject")],
            ["done", "done", "done", "pending"],
        )
        self.assertEqual(c.execute("SELECT count(*) FROM ai_jobs WHERE owner IS NOT NULL").fetchone()[0], 0)

    def test_failed_jobs_retry_individually(self):
        c = database(self)
        self.jobs(c, 3)
        c.execute("UPDATE ai_jobs SET attempts=1")
        c.commit()
        sizes = []

        def provider(task):
            ids = [p["id"] for p in task_input(task)["phrases"]]
            sizes.append(len(ids))
            return phrase_result(ids)

        with patch.object(queue, "plan"), patch.object(ai, "_call", side_effect=provider):
            result = queue.drain(c, kinds=["phrase_enrich"], max_batches=2)
        self.assertEqual(sizes, [1, 1])
        self.assertEqual(result["phrase_enrich"]["done"], 2)

    def test_one_failed_call_keeps_other_committed_results(self):
        c = database(self)
        self.jobs(c, 2)

        def provider(task):
            ids = [p["id"] for p in task_input(task)["phrases"]]
            if "x0" in ids:
                raise ai.AIError("bad response")
            return phrase_result(ids)

        with (
            patch.object(queue, "plan"),
            patch.dict(queue.BATCH, {"phrase_enrich": 1}),
            patch.object(ai, "_call", side_effect=provider),
        ):
            result = queue.drain(c, kinds=["phrase_enrich"], max_batches=2)
        self.assertEqual(result["phrase_enrich"], {"done": 1, "failed": 1})
        self.assertEqual(
            [r[0] for r in c.execute("SELECT status FROM ai_jobs ORDER BY subject")], ["pending", "done"]
        )
        self.assertEqual(c.execute("SELECT count(*) FROM senses").fetchone()[0], 1)

    def test_interrupt_joins_workers_before_releasing_jobs(self):
        c = database(self)
        self.jobs(c, 2)
        barrier = threading.Barrier(2, timeout=3)
        stopped = threading.Event()
        self.addCleanup(queue.STOP.clear)

        def provider(task):
            lid = task_input(task)["phrases"][0]["id"]
            barrier.wait()
            if lid == "x0":
                raise KeyboardInterrupt
            if not queue.STOP.wait(3):
                raise RuntimeError("cancellation was not propagated")
            stopped.set()
            raise ai.AIError("interrupted")

        with (
            patch.object(queue, "plan"),
            patch.dict(queue.BATCH, {"phrase_enrich": 1}),
            patch.object(ai, "_call", side_effect=provider),
        ):
            with self.assertRaises(KeyboardInterrupt):
                queue.drain(c, kinds=["phrase_enrich"], max_batches=2)
        self.assertTrue(stopped.is_set())
        rows = [tuple(r) for r in c.execute("SELECT status,owner,attempts FROM ai_jobs")]
        self.assertEqual(rows, [("pending", None, 0), ("pending", None, 0)])
        self.assertEqual(c.execute("SELECT count(*) FROM senses").fetchone()[0], 0)
