"""Health checks: AnkiConnect capabilities, census severities, deadlines."""

import re
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from flashcards import anki, lists
from flashcards.commands import data_review

from .support import database, desired, lexeme

PACKAGE = Path(__file__).resolve().parents[1] / "flashcards"


class AnkiActionTests(unittest.TestCase):
    def test_every_action_the_code_calls_is_listed(self):
        used = set()
        for path in PACKAGE.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            used |= set(re.findall(r'invoke\(\s*"(\w+)"', text))
            used |= set(re.findall(r'"action": "(\w+)",\s*"params"', text))  # multi() batches
            used |= set(re.findall(r'\("(suspend|unsuspend)", ', text))  # actions picked in a loop
        self.assertTrue(used, "the scan found no calls")
        self.assertEqual(used - {"apiReflect"} - anki.ACTIONS, set())

    def test_capabilities_names_what_anki_no_longer_offers(self):
        def invoke(action, **kw):
            return {
                "version": 6,
                "getActiveProfile": "User 1",
                "apiReflect": {"actions": sorted(anki.ACTIONS - {"cardReviews"})},
            }[action]

        with patch.object(anki, "invoke", side_effect=invoke):
            result = anki.capabilities()
        self.assertEqual(result["unsupported_actions"], ["cardReviews"])

    def test_unreachable_anki_is_reported_not_raised(self):
        with patch.object(anki, "invoke", side_effect=RuntimeError("Could not reach AnkiConnect")):
            self.assertEqual(anki.capabilities()["reachable"], False)


class CensusTests(unittest.TestCase):
    def test_decided_or_optional_findings_do_not_ask_for_review(self):
        c = database(self)
        lexeme(c)
        desired(c)  # a ready note with no audit
        result = data_review.report(c, check_media=False)
        severity = {f["code"]: f["severity"] for f in result["findings"]}
        self.assertEqual(severity.get("unaudited_ready_notes"), "info")
        warnings = [code for code, level in severity.items() if level == "warning"]
        self.assertEqual(result["status"], "review" if warnings else "ok")


class DeadlineTests(unittest.TestCase):
    plan = lists.Plan.from_dict(
        {
            "priority": [
                {"list": "cils-a1"},
                {"list": "old-film", "by": "2026-10-31"},
                {"list": "new-film", "by": "2026-12-20"},
            ]
        }
    )

    def test_a_passed_date_no_longer_jumps_the_queue(self):
        self.assertEqual(self.plan.list_order(today=date(2026, 10, 1)), ["old-film", "new-film", "cils-a1"])
        self.assertEqual(self.plan.list_order(today=date(2026, 11, 17)), ["new-film", "cils-a1", "old-film"])
        self.assertEqual(self.plan.passed_deadlines(today=date(2026, 11, 17)), [("old-film", "2026-10-31")])


if __name__ == "__main__":
    unittest.main()
