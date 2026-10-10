import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flashcards import reconcile
from flashcards.commands import review
from .support import database, lexeme


class ReviewFileTests(unittest.TestCase):
    def test_marked_words_are_released_and_the_rest_listed_again(self):
        c = database(self)
        lexeme(c, "a", status="needs_review", lemma="casa")
        lexeme(c, "b", status="needs_review", lemma="cane")
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(review, "REVIEW_FILE", Path(folder) / "review.csv"),
        ):
            self.assertEqual(review.write_review_file(c), 2)
            text = review.REVIEW_FILE.read_text()
            review.REVIEW_FILE.write_text(text.replace("\n,casa,", "\nyes,casa,"))
            self.assertEqual(review.apply_review_file(c), 1)
            self.assertEqual(review.write_review_file(c), 1)
            self.assertIn("cane", review.REVIEW_FILE.read_text())
        statuses = dict(c.execute("SELECT lemma, status FROM lexemes"))
        self.assertEqual(statuses, {"casa": "ready", "cane": "needs_review"})


class LegacyIndexTests(unittest.TestCase):
    def test_index_is_read_from_anki_once_then_reused(self):
        c = database(self)
        old = reconcile.AnkiNote(10, "gloss:e|en_to_it", {}, [], [100], {"Italian::Test"})
        with patch.object(reconcile, "owned_legacy", return_value=[old]) as scan:
            first = reconcile.legacy_index(c, "Personal")
            second = reconcile.legacy_index(c, "Personal")
            reconcile.legacy_index(c, "Other profile")
        self.assertEqual(first, {10: ("gloss:e|en_to_it", [100])})
        self.assertEqual(first, second)
        self.assertEqual(scan.call_count, 2)  # first use + profile change


class DuplicateRetirementTests(unittest.TestCase):
    def test_served_flag_without_verified_replacement_never_retires(self):
        c = database(self)
        dup = reconcile.AnkiNote(10, "gloss:a|it_to_en", {}, [], [100], {"Italian::Test"})
        plan = reconcile.Plan(retire_legacy=[dup])
        plan.retire_dependencies[10] = ("vocab:x:0", "Recognition")
        plan.served.add(("vocab:x:0", "Recognition"))  # a studied old card covers it
        calls = []

        def invoke(action, **kw):
            calls.append((action, kw))
            if action == "notesInfo":
                return [{"noteId": 10, "fields": {"SortKey": {"value": dup.key}}, "cards": [100]}]
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
    def test_removed_list_marks_orphaned_and_v3_notes_only(self):
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
        c.execute(
            "INSERT INTO entries(id,source_path,natural_id,mode,deck,italian,english) "
            "VALUES('e1','italki/italki.csv','zitto','gloss','Italian - Italki','zitto','quiet')"
        )
        c.execute(
            "INSERT INTO cards(entry_id,natural_key,direction,deck,front_text,back_highlight,sort_order,guid) "
            "VALUES('e1','gloss:e1','it_to_en','Italian - Italki','zitto','quiet',1,'g1')"
        )
        n = lexicon._retire_removed_lists(c, ["italki"], {"italki": "italki/italki.csv"})
        keys = {r[0] for r in c.execute("SELECT key FROM retirements")}
        self.assertEqual(keys, {"vocab:gone:0", "form:gone:presente:io", "gloss:e1|it_to_en"})
        self.assertEqual(n, 3)


class FilmPrivacyTests(unittest.TestCase):
    def test_ai_text_quoting_a_film_line_stays_out_of_the_export(self):
        import json

        from flashcards import lexicon

        c = database(self)
        lexeme(c, "x", lemma="montagna")
        film_line = "lassù in montagna non arriva mai nessuno"
        c.execute(
            "UPDATE list_items SET context=? WHERE lexeme_id='x'", (json.dumps({"example": film_line}),)
        )
        c.execute(
            "INSERT INTO senses(lexeme_id,idx,prompt,note,provenance) VALUES('x',0,'mountain',?,'claude:test')",
            (f"As in the film: '{film_line}'",),
        )
        c.commit()
        text = lexicon.export_jsonl(c).read_text(encoding="utf-8")
        self.assertIn("mountain", text)
        self.assertNotIn("arriva mai", text)
