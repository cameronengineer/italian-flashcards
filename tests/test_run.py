import unittest
from unittest.mock import patch

from flashcards import reconcile
from .support import database, lexeme


class DuplicateRetirementTests(unittest.TestCase):
    def test_unverified_replacement_never_retires(self):
        c = database(self)
        dup = reconcile.AnkiNote(10, "vocab:x:2", {}, [], [100], {"Italian::Test"})
        plan = reconcile.Plan(retire=[dup])
        plan.retire_dependencies[10] = ("vocab:x:0", "Recognition")
        calls = []

        def invoke(action, **kw):
            calls.append((action, kw))
            if action == "notesInfo":
                return [{"noteId": 10, "fields": {"Key": {"value": dup.key}}, "cards": [100]}]
            if action == "getDecks":
                return {"Italian::Test": [100]}
            if action == "getReviewsOfCards":
                return {}
            return []

        with patch.object(reconcile, "invoke", side_effect=invoke):
            retired = reconcile._retire(c, plan, final={}, note_of={}, allow=True)
        self.assertEqual(retired, 0)
        self.assertNotIn(("deleteNotes", {"notes": [10]}), calls)


class RemovedListTests(unittest.TestCase):
    def test_removed_list_marks_only_orphaned_notes(self):
        from flashcards import lexicon

        c = database(self)
        lexeme(c, "kept", lemma="casa")  # still in a list
        c.execute(
            "INSERT INTO lexemes(id,lemma,pos,display,status) VALUES('gone','zitto','intj','zitto','ready')"
        )
        c.executemany(
            "INSERT INTO identity(key,guid,kind,note_id) VALUES(?,'','v4',?)",
            [("vocab:kept:0", 1), ("vocab:gone:0", 2), ("form:gone:presente:io", 3)],
        )
        n = lexicon._retire_removed_lists(c, ["italki"])
        keys = {r[0] for r in c.execute("SELECT key FROM retirements")}
        self.assertEqual(keys, {"vocab:gone:0", "form:gone:presente:io"})
        self.assertEqual(n, 2)
