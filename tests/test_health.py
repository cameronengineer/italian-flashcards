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


class NewCardQueueTests(unittest.TestCase):
    def test_reports_what_comes_next_and_the_deck_options(self):
        cards = {
            1: (5, "Italian::Movies::Le otto montagne", "Recognition"),
            2: (6, "Italian::Movies::Le otto montagne", "Recognition"),
            3: (7, "Italian::CILS::A1", "Recognition"),
            4: (7, "Italian::CILS::A1", "Production"),
            9: (900, "Italian::CILS::B2", "Production"),
        }

        def invoke(action, **params):
            if action == "findCards":
                template = params["query"].rsplit("card:", 1)[1]
                return [c for c, (_due, _deck, t) in cards.items() if t == template]
            if action == "cardsInfo":
                return [{"cardId": c, "due": cards[c][0], "deckName": cards[c][1]} for c in params["cards"]]
            if action == "getDeckConfig":
                return {"name": "Italian flashcards", "newGatherPriority": 1, "newSortOrder": 0,
                        "new": {"bury": True}, "rev": {"bury": True}}  # fmt: skip
            raise AssertionError(action)

        with patch.object(anki, "invoke", invoke):
            result = anki.new_card_queue(sample=4)
        self.assertEqual(result["waiting"], {"Italian → English": 3, "English → Italian": 2})
        self.assertEqual(result["next_4_by_position"], {"Italian → English": 3, "English → Italian": 1})
        self.assertEqual(
            result["next_by_deck"]["Italian::Movies::Le otto montagne"],
            {"Italian → English": 2, "English → Italian": 0},
        )
        options = result["italian_deck_options"]
        self.assertEqual(options["new_card_gather_order"], "Ascending position")
        self.assertEqual(options["new_card_sort_order"], "Card type, then order gathered")


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
