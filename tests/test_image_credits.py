"""A run makes as many images as Codex allows, then stops asking."""

import subprocess
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from flashcards import codex, queue
from flashcards.ai import AIError, AIUnavailable

from .support import database, lexeme

PROMPT = codex.image_prompt("il credito", "credit; a usage quota", "noun")


def codex_said(text, code=0):
    return patch.object(
        codex,
        "run_process",
        return_value=subprocess.CompletedProcess([], code, f"user\n{PROMPT}\n{text}", ""),
    )


class StopMessageTests(unittest.TestCase):
    def run_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex._run(PROMPT, Path(tmp) / "x.png", 60)

    def test_usage_limit_stops_with_or_without_an_exit_code(self):
        for code in (1, 0):
            with (
                self.subTest(code=code),
                codex_said(
                    "You've hit your usage limit. Upgrade to Plus, or try again in 4 days 2 hours.", code
                ),
            ):
                with self.assertRaisesRegex(codex.ImagesStopped, "usage limit"):
                    self.run_codex()

    def test_image_rate_limit_and_logout_stop(self):
        for text in ("Image generation failed: rate limit reached for gpt-image", "Error: not logged in"):
            with self.subTest(text=text), codex_said(text, 1), self.assertRaises(codex.ImagesStopped):
                self.run_codex()

    def test_the_word_itself_never_reads_as_a_limit(self):
        # The echoed prompt says "credit" and "quota": only Codex's own words count.
        with codex_said("Done, but I could not save the file."), self.assertRaises(AIError) as caught:
            self.run_codex()
        self.assertNotIsInstance(caught.exception, codex.ImagesStopped)

    def test_network_trouble_is_still_waited_out(self):
        with codex_said("stream error: connection reset by peer", 1), self.assertRaises(AIUnavailable):
            self.run_codex()


class RunStateTests(unittest.TestCase):
    def setUp(self):
        codex._stopped.clear()
        self.addCleanup(codex._stopped.clear)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = patch.object(codex, "IMAGE_DIR", Path(tmp.name))
        folder.start()
        self.addCleanup(folder.stop)

    def test_after_a_stop_codex_is_not_asked_again_and_nothing_waits(self):
        with patch.object(
            codex, "_run", side_effect=codex.ImagesStopped("You've hit your usage limit")
        ) as run:
            with self.assertRaises(codex.ImagesStopped):
                codex.generate("casa", "la casa", "house", "noun")
            with self.assertRaises(codex.ImagesStopped):
                codex.generate("cane", "il cane", "dog", "noun")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(codex.stopped(), "You've hit your usage limit")
        self.assertTrue(codex._gate.open.is_set())  # no outage wait was started


class DrainTests(unittest.TestCase):
    def setUp(self):
        codex._stopped.clear()
        self.addCleanup(codex._stopped.clear)
        self.c = database(self)
        for i in range(5):
            lexeme(self.c, lid=f"x{i}", lemma=f"casa{i}")
            queue._enqueue(self.c, "image", f"x{i}", i, {"image_key": f"casa{i}"})
        self.c.commit()

    def drain(self, run_image):
        with (
            patch.object(codex, "logged_in", return_value=True),
            patch.object(queue, "_run_image", side_effect=run_image) as run,
            patch.object(queue, "plan"),
        ):
            result = queue.drain(self.c, kinds=["image"], image_limit=None)
        return run, result

    def jobs(self):
        return [tuple(r) for r in self.c.execute("SELECT status, attempts FROM ai_jobs ORDER BY subject")]

    def test_out_of_credit_puts_jobs_back_untouched_and_stops_for_the_run(self):
        made = []
        lock = threading.Lock()

        def run_image(job, conn, _lock):
            with lock:
                if len(made) < 2:
                    made.append(job["subject"])
                    return True
            codex.stop("You've hit your usage limit")
            raise codex.ImagesStopped("You've hit your usage limit")

        run, result = self.drain(run_image)
        self.assertEqual(result["image"], {"done": 2, "failed": 0})
        self.assertEqual(sorted(self.jobs()), [("done", 1)] * 2 + [("pending", 0)] * 3)
        calls = run.call_count
        run, _ = self.drain(run_image)  # a later round in the same run
        run.assert_not_called()
        self.assertLessEqual(calls, 5)

    def test_unrecognised_repeated_failures_also_stop(self):
        run, result = self.drain(lambda *a: (_ for _ in ()).throw(AIError("codex exec produced no image")))
        self.assertEqual(run.call_count, 3)  # one wave of three, then stop
        self.assertTrue(codex.stopped().startswith("3 images failed in a row"))
        self.assertEqual(sorted(self.jobs()), [("pending", 0)] * 2 + [("pending", 1)] * 3)

    def test_a_success_resets_the_count(self):
        outcomes = iter([AIError("no image"), AIError("no image"), True, AIError("no image"), True])

        def run_image(*_a):
            value = next(outcomes)
            if isinstance(value, Exception):
                raise value
            return value

        one_at_a_time = replace(queue.settings, images=replace(queue.settings.images, concurrency=1))
        with patch.object(queue, "settings", one_at_a_time):
            run, _ = self.drain(run_image)
        self.assertEqual(run.call_count, 5)
        self.assertIsNone(codex.stopped())


if __name__ == "__main__":
    unittest.main()
