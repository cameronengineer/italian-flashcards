"""Regressions from the completed 10–11 October run; no external services."""

import json
import sqlite3
import tempfile
import threading
import unittest
import zlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from flashcards import ai, kaikki, pipeline, queue, reconcile, report
from .support import database, lexeme
from .test_queue_performance import phrase_result, task_input


class DictionaryThreadTests(unittest.TestCase):
    def test_parallel_dictionary_reads_never_lose_or_mix_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "dictionary.sqlite"
            with closing(sqlite3.connect(path)) as conn:
                conn.executescript(kaikki.SCHEMA)
                for word in ("casa", "cane", "gatto", "libro"):
                    data = zlib.compress(json.dumps({"word": word, "senses": [word]}).encode())
                    conn.execute(
                        "INSERT INTO entries(word,plain,pos,data) VALUES(?,?,'noun',?)", (word, word, data)
                    )
                    conn.execute("INSERT INTO forms VALUES(?,?,?,'noun','lemma')", (word, word, word))
                conn.commit()
            barrier = threading.Barrier(8)

            def lookup(worker):
                barrier.wait(timeout=3)
                word = ("casa", "cane", "gatto", "libro")[worker % 4]
                for _ in range(200):
                    self.assertEqual(kaikki.entries(word), [{"word": word, "senses": [word]}])
                    self.assertEqual(kaikki.lemmas_for(word), [(word, "noun", "lemma")])

            kaikki._close_read_connection()
            with patch.object(kaikki, "KAIKKI_DB", path):
                try:
                    with ThreadPoolExecutor(max_workers=8) as pool:
                        list(pool.map(lookup, range(8)))
                finally:
                    kaikki._close_read_connection()


class BatchRecoveryTests(unittest.TestCase):
    def jobs(self, conn, n):
        for i in range(n):
            lid = f"x{i}"
            lexeme(conn, lid, pos="phrase", lemma=lid)
            queue._enqueue(conn, "phrase_enrich", lid, i, {"id": lid, "italian": lid})
        conn.commit()

    def test_valid_members_survive_missing_and_duplicate_answers(self):
        c = database(self)
        self.jobs(c, 3)
        # x0 is sound, x1 duplicated, x2 missing: only x0 may be published.
        with tempfile.TemporaryDirectory() as folder:
            event_file = Path(folder) / "run.events.jsonl"
            with (
                patch.object(queue, "plan"),
                patch.object(report, "EVENT_FILE", event_file),
                patch.object(ai, "_call", return_value=phrase_result(["x0", "x1", "x1"])),
            ):
                result = queue.drain(c, kinds=["phrase_enrich"], max_batches=1)
            self.assertEqual(result, {"phrase_enrich": {"done": 1, "failed": 2}})
            self.assertEqual(
                dict(c.execute("SELECT subject,status FROM ai_jobs")),
                {"x0": "done", "x1": "pending", "x2": "pending"},
            )
            self.assertEqual(c.execute("SELECT lexeme_id FROM senses").fetchall()[0][0], "x0")
            self.assertEqual(c.execute("SELECT count(*) FROM ai_cache").fetchone()[0], 0)
            event = json.loads(event_file.read_text().splitlines()[0])
            self.assertEqual((event["event"], event["saved"], len(event["errors"])), ("batch_failed", 1, 2))
            self.assertIn("PartialBatchError", event["traceback"])
        self.assertEqual(queue.retry_failed(c, include_deferred=True), 2)
        requested = []

        def provider(task):
            ids = [p["id"] for p in task_input(task)["phrases"]]
            requested.extend(ids)
            self.assertIn("Correct these validation failures", task.prompt)
            self.assertIn("only the CURRENT input IDs", task.prompt)
            return phrase_result(ids)

        with patch.object(queue, "plan"), patch.object(ai, "_call", side_effect=provider):
            result = queue.drain(c, kinds=["phrase_enrich"])
        self.assertCountEqual(requested, ["x1", "x2"])
        self.assertEqual(result, {"phrase_enrich": {"done": 2, "failed": 0}})
        self.assertEqual(c.execute("SELECT count(*) FROM ai_jobs WHERE status='done'").fetchone()[0], 3)

    def test_one_malformed_member_does_not_discard_valid_answer(self):
        c = database(self)
        self.jobs(c, 2)
        result = phrase_result(["x0", "x1"])
        del result["phrases"][1]["verified"]
        with patch.object(queue, "plan"), patch.object(ai, "_call", return_value=result):
            counts = queue.drain(c, kinds=["phrase_enrich"], max_batches=1)
        self.assertEqual(counts["phrase_enrich"], {"done": 1, "failed": 1})

    def test_partial_word_batch_still_enforces_source_evidence(self):
        c = database(self)
        output = []
        schema = queue.LEXEME_ENRICH.schema["properties"]["words"]["items"]["properties"]
        feature_schema = schema["senses"]["items"]["properties"]["features"]["properties"]
        for i in range(2):
            lid = f"w{i}"
            lexeme(c, lid, lemma=f"casa{i}")
            queue._enqueue(
                c,
                "lexeme_enrich",
                lid,
                i,
                {
                    "id": lid,
                    "italian": f"casa{i}",
                    "meaning_evidence": {"candidates": [{"source_id": "dictionary:house"}]},
                },
            )
            output.append(
                {
                    "id": lid,
                    "senses": [
                        {
                            "prompt": "house",
                            "hint": "",
                            "register": "",
                            "note": "",
                            "source_id": "dictionary:house" if i == 0 else "invented",
                            "features": {
                                k: [] if v["type"] == "array" else "" for k, v in feature_schema.items()
                            },
                        }
                    ],
                    "english_plural": "houses",
                    "verified": True,
                    "issues": [],
                    "fact": {
                        "has_fact": False,
                        "kind": schema["fact"]["properties"]["kind"]["enum"][0],
                        "text": "",
                        "confidence": 0,
                    },
                }
            )
        c.commit()
        with patch.object(queue, "plan"), patch.object(ai, "_call", return_value={"words": output}):
            result = queue.drain(c, kinds=["lexeme_enrich"], max_batches=1)
        self.assertEqual(result["lexeme_enrich"], {"done": 1, "failed": 1})
        self.assertEqual(
            [tuple(r) for r in c.execute("SELECT lexeme_id,source_id FROM senses")],
            [("w0", "dictionary:house")],
        )
        error = c.execute("SELECT error FROM ai_jobs WHERE subject='w1'").fetchone()[0]
        self.assertIn("supplied dictionary/source evidence", error)

    def test_cooldowns_are_visible_and_fresh_run_releases_them(self):
        c = database(self)
        self.jobs(c, 3)
        c.execute(
            "UPDATE ai_jobs SET error='bad result',attempts=2,next_retry_at='2099-01-01' WHERE subject='x0'"
        )
        c.execute("UPDATE ai_jobs SET error='bad result',attempts=3,status='failed' WHERE subject='x1'")
        c.execute("UPDATE ai_jobs SET status='done' WHERE subject='x2'")
        c.commit()
        self.assertEqual(queue.backlog(c)["cooling_down"], 1)
        self.assertEqual(queue.backlog(c)["failed"], 1)
        self.assertEqual(pipeline._todo(c), "2 phrases")
        self.assertEqual(queue.retry_failed(c, include_deferred=True), 2)
        self.assertEqual(queue.backlog(c)["runnable"], 2)
        self.assertEqual(
            [tuple(r) for r in c.execute("SELECT status,attempts FROM ai_jobs ORDER BY subject")],
            [("pending", 2), ("pending", 0), ("done", 0)],
        )

    def test_third_failure_is_terminal_with_no_false_retry_promise(self):
        c = database(self)
        self.jobs(c, 1)
        c.execute("UPDATE ai_jobs SET attempts=2")
        c.commit()
        with (
            patch.object(queue, "plan"),
            patch.object(ai, "_call", side_effect=ai.AIError("invalid")),
            patch.object(report, "line") as output,
        ):
            queue.drain(c, kinds=["phrase_enrich"])
        self.assertEqual(queue.backlog(c)["failed"], 1)
        self.assertEqual(pipeline._todo(c), "1 phrase")
        lines = " ".join(str(call) for call in output.call_args_list)
        self.assertIn("exhausted", lines)
        self.assertNotIn("retry after 5m", lines)

    def test_cue_groups_wait_for_words_and_batch_across_priority_bands(self):
        c = database(self)
        self.jobs(c, 1)
        for n in range(2):
            ids = [f"g{n}a", f"g{n}b"]
            for lid in ids:
                lexeme(c, lid, lemma=lid)
            queue._enqueue(
                c,
                "disambiguate",
                f"group{n}",
                n * 100000,
                {"ids": ids, "prompt": "house", "words": [{"id": i, "italian": i} for i in ids]},
            )
        c.commit()
        calls = []

        def provider(task):
            calls.append(task.name)
            payload = task_input(task)
            if task.name == "phrase_enrich":
                return phrase_result([p["id"] for p in payload["phrases"]])
            self.assertEqual(len(payload["groups"]), 2)
            return {
                "words": [
                    {"id": i, "hint": "dwelling", "also": ""} for g in payload["groups"] for i in g["ids"]
                ]
            }

        config = replace(queue.settings, ai=replace(queue.settings.ai, concurrency=1))
        with (
            patch.object(queue, "settings", config),
            patch.object(queue, "plan"),
            patch.object(ai, "_call", side_effect=provider),
        ):
            result = queue.drain(c, kinds=["phrase_enrich", "disambiguate"])
        self.assertEqual(calls, ["phrase_enrich", "disambiguate"])
        self.assertEqual(result["disambiguate"]["done"], 2)


class SuspensionAndTimingTests(unittest.TestCase):
    def test_only_new_suspensions_are_sent_and_counted(self):
        plan = reconcile.Plan(suspend=[10, 10, 20, 30])
        cards = [{"cardId": 10, "queue": -1}, {"cardId": 20, "queue": 2}, {}]
        with patch.object(
            reconcile, "invoke", side_effect=lambda action, **kw: cards if action == "cardsInfo" else True
        ) as invoke:
            reconcile._suspend_active(plan)
        self.assertEqual(plan.suspended, 1)
        self.assertIn("1 newly suspended", plan.result())
        self.assertEqual(invoke.call_args_list[-1].kwargs, {"cards": [20]})
        plan = reconcile.Plan(suspend=[10])
        with patch.object(reconcile, "invoke", return_value=[cards[0]]) as invoke:
            reconcile._suspend_active(plan)
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(plan.result(), "up to date")

    def test_wait_time_is_counted_once_and_gate_reopens(self):
        gate = ai._Gate()
        gate.open.clear()
        with (
            patch.object(ai.time, "monotonic", side_effect=[10, 10, 310]),
            patch.object(ai, "seconds_until_reset", return_value=0),
            patch.object(ai.STOP, "wait", return_value=False),
        ):
            self.assertEqual(gate._probe(lambda: "ok", ai.AIUnavailable("usage limit")), "ok")
        self.assertEqual(gate.waited_seconds(), 300)
        self.assertTrue(gate.open.is_set())
