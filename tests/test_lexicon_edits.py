"""Edits to lexicon/lexemes.jsonl survive the run writing the file again."""

import json
import unittest
from unittest.mock import patch

from flashcards import lexicon, quality
from flashcards.domain import ListDef

from .support import database, lexeme


class LiveEditTests(unittest.TestCase):
    def setUp(self):
        self.c = database(self)
        lexeme(self.c)
        self.c.execute("INSERT INTO metadata VALUES('source_generation','g1')")
        self.c.execute("INSERT INTO metadata VALUES('notes_generation','g1')")
        self.c.commit()
        configured = patch.object(
            lexicon.lists_mod, "load", return_value=[ListDef("test", "csv", "Test", "Italian::Test", None)]
        )
        configured.start()
        self.addCleanup(configured.stop)
        self.path = lexicon.export_jsonl(self.c)

    def edit(self, **fields):
        record = json.loads(self.path.read_text(encoding="utf-8").splitlines()[0])
        record.update(fields)
        record["provenance"] = {**record.get("provenance", {}), **{k: "human" for k in fields}}
        self.path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    def test_an_edit_made_during_a_run_is_taken_in_not_overwritten(self):
        self.edit(display="la casetta")
        lexicon.export_jsonl(self.c)  # the run writes the file again
        self.assertEqual(self.c.execute("SELECT display FROM lexemes").fetchone()[0], "la casetta")
        self.assertIn('"la casetta"', self.path.read_text(encoding="utf-8"))
        self.assertFalse(quality.notes_current(self.c))  # the notes are rebuilt before publishing

    def test_an_unchanged_file_takes_nothing_in(self):
        with patch.object(lexicon, "import_human_edits") as take_in:
            lexicon.export_jsonl(self.c)
        take_in.assert_not_called()
        self.assertTrue(quality.notes_current(self.c))

    def test_reading_the_same_edits_again_changes_nothing(self):
        self.edit(display="la casetta")
        self.assertEqual(lexicon.import_human_edits(self.c), 1)
        self.assertEqual(lexicon.import_human_edits(self.c), 0)


if __name__ == "__main__":
    unittest.main()
