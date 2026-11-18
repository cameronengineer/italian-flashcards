"""Claude and ElevenLabs work until their limits, then stop for the run."""

import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flashcards import ai, codex, queue
from flashcards.ai import AIUnavailable, LimitReached

from .support import database, lexeme

LIMIT = "claude -p: You've hit your limit · resets 3pm (Europe/London)"


def reset_claude(test):
    ai._limit.clear()
    test.addCleanup(ai._limit.clear)


class ClaudeLimitTests(unittest.TestCase):
    def setUp(self):
        reset_claude(self)

    def test_the_limit_stops_claude_for_the_rest_of_the_run_without_waiting(self):
        task = SimpleNamespace(name="t")
        with patch.object(ai, "_claude_code", side_effect=AIUnavailable(LIMIT)) as claude:
            with self.assertRaises(LimitReached):
                ai._call(task)
            with self.assertRaises(LimitReached):
                ai._call(task)
        self.assertEqual(claude.call_count, 1)  # the second call never starts a process
        self.assertEqual(ai.limit_reached(), "usage limit reached (resets 3pm)")
        self.assertTrue(ai.gate.open.is_set())  # no outage wait was started

    def test_claude_code_limit_wordings(self):
        for text in (
            "Claude AI usage limit reached|1700000000",
            "5-hour limit reached ∙ resets 3am",
            "Opus weekly limit reached ∙ resets Mon 9am",
            "You're out of extra usage · resets 3pm",
            "Credit balance is too low",
        ):
            with self.subTest(text=text):
                self.assertTrue(ai._USAGE_LIMIT.search(text))
        self.assertIn("resets", ai._short_reason("Claude AI usage limit reached|1700000000"))

    def test_rate_limits_and_outages_are_still_waited_out(self):
        for text in (
            'API Error: 429 {"type":"rate_limit_error","message":"would exceed your rate limit"}',
            "API Error: 529 overloaded_error",
            "fetch failed: ECONNRESET",
        ):
            with self.subTest(text=text):
                ai._stop_at_limit(AIUnavailable(text))  # returns: the gate's normal wait applies
        self.assertIsNone(ai.limit_reached())

    def test_wait_setting_keeps_the_old_behaviour(self):
        waiting = replace(ai.settings, ai=replace(ai.settings.ai, on_limit="wait"))
        with patch.object(ai, "settings", waiting):
            ai._stop_at_limit(AIUnavailable(LIMIT))
        self.assertIsNone(ai.limit_reached())


class ClaudeDrainTests(unittest.TestCase):
    def setUp(self):
        reset_claude(self)
        codex._stopped.clear()
        self.addCleanup(codex._stopped.clear)
        self.c = database(self)
        for i in range(3):
            lexeme(self.c, lid=f"x{i}", lemma=f"casa{i}")
            queue._enqueue(self.c, "lexeme_enrich", f"x{i}", i, {"id": f"x{i}"})
        self.c.commit()

    def drain(self, run_text):
        with (
            patch.object(queue, "_run_text_batch", side_effect=run_text) as run,
            patch.object(queue, "plan"),
            redirect_stdout(io.StringIO()),
        ):
            queue.drain(self.c, kinds=["lexeme_enrich"], image_limit=0)
        return run

    def test_jobs_go_back_untouched_and_later_rounds_skip_claude(self):
        def run_text(*_a):
            ai._limit.append("usage limit reached")
            raise LimitReached("usage limit reached")

        self.drain(run_text)
        jobs = [tuple(r) for r in self.c.execute("SELECT status, attempts, error FROM ai_jobs")]
        self.assertEqual(jobs, [("pending", 0, None)] * 3)
        self.drain(run_text).assert_not_called()  # the next round in the same run


class AudioStopTests(unittest.TestCase):
    def client(self, error):
        def convert(**kw):
            raise error

        return SimpleNamespace(text_to_speech=SimpleNamespace(convert=convert))

    def test_refused_key_or_plan_stops_audio(self):
        from elevenlabs.core.api_error import ApiError
        from flashcards.commands import media

        refused = ApiError(
            status_code=401, body={"detail": {"status": "invalid_api_key", "message": "bad key"}}
        )
        stop = media._Stop()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(media.OutOfCredit):
                media._speak(self.client(refused), "casa", Path(tmp) / "a.mp3", stop)
        self.assertIn("401", stop.reason)

    def test_repeated_failures_stop_the_run_of_recordings(self):
        from flashcards.commands import media

        fake = SimpleNamespace(
            text_to_speech=SimpleNamespace(convert=lambda **kw: (_ for _ in ()).throw(OSError("down")))
        )
        texts = [f"parola {i}" for i in range(20)]
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(media, "connect", lambda: sqlite3.connect(":memory:")),
            patch.object(media, "_audio_texts", return_value=texts),
            patch.object(media, "locate", return_value=None),
            patch.object(media, "ensure_dirs"),
            patch.object(media, "AUDIO_DIR", Path(tmp)),
            patch.object(media, "load_key_file", return_value="key"),
            patch("elevenlabs.client.ElevenLabs", return_value=fake),
            redirect_stdout(io.StringIO()),
        ):
            made = media.generate_audio(workers=1)
        self.assertEqual(made["failed"], 5)
        self.assertEqual(made["generated"], 0)
        self.assertTrue(made["stopped"].startswith("5 recordings failed in a row"))
        self.assertFalse(made["out_of_credit"])


if __name__ == "__main__":
    unittest.main()
