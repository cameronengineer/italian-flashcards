"""Leeches: finding them, releasing them, and the doctor's unsuspend."""

import io
import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flashcards.commands import leech
from flashcards.db import init_schema

AGAIN, GOOD = 1, 3


class FakeAnki:
    """Notes 1 (cards 11, 12) and 2 (card 13) are leeches; 99 is suspended for another reason."""

    def __init__(self):
        self.cards = {1: [11, 12], 2: [13], 3: [99]}
        self.tagged = {1, 2}
        self.suspended = {11, 13, 99}
        self.calls = []

    def __call__(self, action, **params):
        self.calls.append(action)
        leech_cards = {c for n in self.tagged for c in self.cards[n]}
        if action == "findNotes":
            return sorted(self.tagged)
        if action == "findCards":
            found = leech_cards & self.suspended if "is:suspended" in params["query"] else leech_cards
            return sorted(found)
        if action == "unsuspend":
            self.suspended -= set(params["cards"])
            return True
        if action == "removeTags":
            self.tagged -= set(params["notes"])
            return None
        raise AssertionError(f"unexpected AnkiConnect action {action}")


class LeechTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "database.sqlite"
        with sqlite3.connect(self.path) as conn:
            init_schema(conn)
        patcher = patch.object(leech, "connect", lambda: sqlite3.connect(self.path))
        patcher.start()
        self.addCleanup(patcher.stop)

    def released(self):
        with sqlite3.connect(self.path) as conn:
            row = conn.execute("SELECT value FROM metadata WHERE key=?", (leech.RELEASED,)).fetchone()
        return {int(k) for k in json.loads(row[0])} if row else set()

    def quietly(self, fn, *args, **kwargs):
        with redirect_stdout(io.StringIO()):
            return fn(*args, **kwargs)

    def test_release_unsuspends_and_untags_every_leech(self):
        anki = FakeAnki()
        with patch.object(leech, "invoke", anki):
            self.assertEqual(self.quietly(leech.release), 0)
        self.assertEqual(anki.tagged, set())
        self.assertEqual(anki.suspended, {99})  # not a leech: left alone
        self.assertEqual(self.released(), {11, 12, 13})

    def test_dry_run_changes_nothing(self):
        anki = FakeAnki()
        with patch.object(leech, "invoke", anki):
            self.quietly(leech.release, dry_run=True)
        self.assertEqual((anki.tagged, anki.suspended), ({1, 2}, {11, 13, 99}))
        self.assertNotIn("unsuspend", anki.calls)
        self.assertEqual(self.released(), set())

    def test_released_card_is_judged_only_on_new_failures(self):
        start = int(time.time() * 1000) - 10_000
        reviews = [[start + i, 11, 0, AGAIN] for i in range(5)]

        def invoke(action, **params):
            return {
                "findCards": [11],
                "cardsInfo": [{"cardId": 11, "queue": 0, "type": 2, "note": 1}],
                "cardReviews": reviews,
            }[action]

        def leeches(released):
            with patch.object(leech, "invoke", invoke):
                stats = leech._process_deck("Italian::X", 5, True, {11}, released)
            return stats["review_leeches"]

        self.assertEqual(leeches({}), 1)
        self.assertEqual(leeches({11: start + 100}), 0)  # old failures no longer count
        reviews += [[start + 200 + i, 11, 0, AGAIN] for i in range(5)]
        self.assertEqual(leeches({11: start + 100}), 1)  # failed five times again since

    def test_doctor_unsuspends_leeches_that_already_have_a_memory_aid(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO mnemonics(key,mnemonic) VALUES('vocab:x:0','Casa sounds like…')")
        unsuspended = []

        def invoke(action, **params):
            if action == "findNotes":
                return [1]
            if action == "notesInfo":
                fields = {"Key": "vocab:x:0", "Italian": "la casa", "English": "house"}
                return [
                    {"noteId": 1, "fields": {k: {"value": v} for k, v in fields.items()}, "cards": [11, 12]}
                ]
            if action == "unsuspend":
                unsuspended.extend(params["cards"])
                return True
            raise AssertionError(action)

        owned = [SimpleNamespace(note_id=1, decks=["Italian::X"], cards=[11, 12])]
        with patch.object(leech, "invoke", invoke), patch.object(leech, "_owned", lambda: owned):
            self.assertEqual(self.quietly(leech.doctor, unsuspend=True), 0)
        self.assertEqual(unsuspended, [11, 12])
        self.assertEqual(self.released(), {11, 12})


if __name__ == "__main__":
    unittest.main()
