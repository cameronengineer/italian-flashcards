"""``purge-v3`` deletes only notes that are provably the old pipeline's."""

import unittest
from contextlib import contextmanager
from unittest.mock import patch

from flashcards import purge_v3

from .support import database


class PurgeTests(unittest.TestCase):
    def setUp(self):
        self.c = database(self)
        self.c.execute("CREATE TABLE v3_note_keys (sort_key TEXT PRIMARY KEY)")
        self.c.executemany(
            "INSERT INTO v3_note_keys VALUES(?)", [("gloss:a|it_to_en",), ("gloss:b|en_to_it",)]
        )
        self.c.execute("INSERT INTO metadata VALUES('v3_decks','[\"Italian - Old\"]')")
        self.c.commit()
        # note id → (SortKey, deck of its card, reviews)
        self.anki = {
            1: ("gloss:a|it_to_en", "Italian - Old", 12),  # ours, studied
            2: ("gloss:b|en_to_it", "Italian::CILS::A1", 0),  # ours, never studied
            3: ("gloss:b|en_to_it", "My archive", 3),  # moved away by the user
            4: ("someone-elses-key", "Italian - Old", 0),  # same notetype, not issued by the pipeline
        }
        self.deleted = []

    def invoke(self, action, **kw):
        if action == "getActiveProfile":
            return "Test"
        if action == "modelNames":
            return ["Italian Card Model+++", "Italian Card Model other", "Basic"]
        if action == "modelFieldNames":
            return purge_v3.MODEL_FIELDS if kw["modelName"] == "Italian Card Model+++" else ["Front"]
        if action == "findNotes":
            self.assertIn("Italian Card Model+++", kw["query"])
            self.assertNotIn("Italian Card Model other", kw["query"])
            return sorted(self.anki)
        if action == "notesInfo":
            return [
                {"noteId": n, "fields": {"SortKey": {"value": self.anki[n][0]}}, "cards": [n * 10]}
                for n in kw["notes"]
                if n in self.anki
            ]
        if action == "getDecks":
            decks = {}
            for cid in kw["cards"]:
                decks.setdefault(self.anki[cid // 10][1], []).append(cid)
            return decks
        if action == "cardsInfo":
            return [
                {"cardId": c, "type": 2 if self.anki[c // 10][2] else 0, "reps": self.anki[c // 10][2]}
                for c in kw["cards"]
            ]
        if action == "deleteNotes":
            self.deleted += kw["notes"]
            for n in kw["notes"]:
                del self.anki[n]
            return None
        raise AssertionError(action)

    def run_purge(self, **kw):
        @contextmanager
        def same_connection():
            yield self.c

        with (
            patch.object(purge_v3, "invoke", side_effect=self.invoke),
            patch.object(purge_v3, "connect", return_value=self.c),
            patch.object(purge_v3, "closing", side_effect=lambda conn: same_connection()),
            patch.object(purge_v3.backup, "anki_snapshot", return_value=None) as snapshot,
        ):
            code = purge_v3.run(**kw)
        return code, snapshot

    def test_dry_run_changes_nothing(self):
        code, snapshot = self.run_purge()
        self.assertEqual((code, self.deleted), (0, []))
        snapshot.assert_not_called()
        self.assertEqual(self.c.execute("SELECT count(*) FROM v3_note_keys").fetchone()[0], 2)

    def test_apply_deletes_only_owned_notes_after_a_backup(self):
        code, snapshot = self.run_purge(apply=True)
        self.assertEqual(code, 0)
        snapshot.assert_called_once_with("Test")
        self.assertEqual(self.deleted, [1, 2])
        self.assertEqual(sorted(self.anki), [3, 4])
        # Nothing of the pipeline's is left: the saved keys go too.
        self.assertIsNone(self.c.execute("SELECT 1 FROM sqlite_master WHERE name='v3_note_keys'").fetchone())

    def test_wrong_profile_is_refused(self):
        self.c.execute("INSERT INTO metadata VALUES('anki_profile','Personal')")
        self.c.commit()
        with self.assertRaises(RuntimeError):
            self.run_purge(apply=True)
        self.assertEqual(self.deleted, [])


if __name__ == "__main__":
    unittest.main()
