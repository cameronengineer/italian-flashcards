"""Which phases a run performs, and how its exit code is decided."""

import unittest
from collections import defaultdict
from types import SimpleNamespace

from flashcards import cli, pipeline
from flashcards.pipeline import PHASES, Run, RunOptions


def parse(*argv):
    return RunOptions.from_args(cli.build_parser().parse_args(["run", *argv]))


class OptionTests(unittest.TestCase):
    def test_everything_is_on_by_default(self):
        self.assertEqual(parse().skip, frozenset())

    def test_only_and_skip(self):
        self.assertEqual(
            parse("--only", "build", "--only", "publish").skip, frozenset(PHASES) - {"build", "publish"}
        )
        self.assertEqual(parse("--skip", "export").skip, {"export"})

    def test_older_flags_are_shorthands(self):
        self.assertEqual(parse("--no-ai").skip, {"enrich", "audio"})
        self.assertEqual(parse("--no-sync").skip, {"publish"})
        self.assertEqual(parse("--no-audio").audio, 0)

    def test_unknown_phase_is_refused(self):
        args = SimpleNamespace(
            skip=["typo"], images=0, audio=0, audio_workers=1, batches=1, once=False, keep_old=False
        )
        with self.assertRaises(ValueError):
            RunOptions.from_args(args)


class SwitchedOffTests(unittest.TestCase):
    def run_state(self, **options):
        return Run(RunOptions(**options), "id", None, 0.0, 0.0)

    def test_switched_off_phases_do_nothing(self):
        run = self.run_state(skip=frozenset(PHASES))
        # None of these may touch the connection (None would raise).
        self.assertIn("switched off", pipeline.publish(run, None))
        pipeline.plan(run, None)
        pipeline.build(run, None)


class ExitCodeTests(unittest.TestCase):
    def state(self, *, skip=(), code=0, pending=None, failed_jobs=0, image_failures=0, rejections=0):
        run = Run(RunOptions(skip=frozenset(skip)), "id", None, 0.0, 0.0, code=code)
        run.summary = {
            "pending": pending or {},
            "backlog": {"failed": failed_jobs},
            "unresolved_image_failures": image_failures,
        }
        run.pending_rejections = rejections
        run.failed = defaultdict(int, {"image": 1} if image_failures else {})
        return run

    def test_complete(self):
        self.assertEqual(pipeline.exit_code(self.state()), 0)

    def test_work_left_is_2(self):
        self.assertEqual(pipeline.exit_code(self.state(pending={"lexeme_enrich": 3})), 2)
        self.assertEqual(pipeline.exit_code(self.state(rejections=1)), 2)

    def test_pending_images_alone_are_not_work_left(self):
        self.assertEqual(pipeline.exit_code(self.state(pending={"image": 400})), 0)

    def test_failures_are_1_and_win_over_work_left(self):
        self.assertEqual(pipeline.exit_code(self.state(failed_jobs=1, pending={"lexeme_enrich": 3})), 1)
        self.assertEqual(pipeline.exit_code(self.state(image_failures=1)), 1)
        self.assertEqual(pipeline.exit_code(self.state(code=1)), 1)

    def test_text_work_does_not_count_when_enrich_is_off(self):
        state = self.state(skip={"enrich"}, pending={"lexeme_enrich": 3}, failed_jobs=2)
        self.assertEqual(pipeline.exit_code(state), 0)


if __name__ == "__main__":
    unittest.main()
