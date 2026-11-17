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


class ReorderTests(unittest.TestCase):
    """AnkiConnect since October 2026 refuses to set a new card's position."""

    def plan(self, c):
        c.executemany(
            "INSERT INTO v4_notes(key,card_type,deck,fields_json,fields_hash,tags,sort_order,ready) "
            "VALUES(?,'vocab','Italian::T','{}','h','',?,1)",
            [("vocab:a:0", 1), ("vocab:b:0", 2)],
        )
        notes = {
            key: reconcile.AnkiNote(
                nid, key, {}, [], [nid * 10], cards_info={nid * 10: {"type": 0, "ord": 0, "due": 99}}
            )
            for nid, key in ((1, "vocab:a:0"), (2, "vocab:b:0"))
        }
        return reconcile.Plan(v4=notes)

    def test_refusal_skips_reordering_and_the_sync_carries_on(self):
        c = database(self)
        calls = []

        def invoke(action, **kw):
            calls.append(len(kw["actions"]))
            return [
                {"result": None, "error": "Use native scheduling commands for protected state."}
                for _ in kw["actions"]
            ]

        with (
            patch.object(reconcile, "invoke", side_effect=invoke),
            patch.object(reconcile, "_reorder_refused", []),
        ):
            self.assertEqual(reconcile._reorder(c, self.plan(c), {}, {}), 0)
            self.assertEqual(reconcile._reorder(c, self.plan2(c), {}, {}), 0)
        self.assertEqual(calls, [1])  # asked once, with one card, and never again

    def plan2(self, c):
        c.execute("DELETE FROM v4_notes")
        return self.plan(c)

    def test_supported_reordering_still_moves_every_card(self):
        c = database(self)
        moved = []

        def invoke(action, **kw):
            moved.extend(a["params"]["card"] for a in kw["actions"])
            return [{"result": True, "error": None} for _ in kw["actions"]]

        with (
            patch.object(reconcile, "invoke", side_effect=invoke),
            patch.object(reconcile, "_reorder_refused", []),
        ):
            self.assertEqual(reconcile._reorder(c, self.plan(c), {}, {}), 2)
        self.assertEqual(sorted(moved), [10, 20])

    def test_other_failures_still_stop_the_sync(self):
        c = database(self)
        with (
            patch.object(reconcile, "invoke", return_value=[{"result": None, "error": "card was not found"}]),
            patch.object(reconcile, "_reorder_refused", []),
            self.assertRaises(RuntimeError),
        ):
            reconcile._reorder(c, self.plan(c), {}, {})
