import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flashcards import lists
from flashcards.domain import ListDef


class NumberSourceTests(unittest.TestCase):
    def test_curated_source_is_active_and_prioritizes_requested_contrasts(self):
        source = next(l for l in lists.load() if l.id == "numbers")
        self.assertEqual(source.path.name, "italian_numbers.csv")
        items = lists.read(source)
        values = [item.context["number"] for item in items]
        self.assertEqual(values[:12], [11, 12, 13, 14, 15, 16, 17, 18, 19, 10, 20, 30])
        self.assertEqual(values[12:30], [n for u in range(1, 10) for n in (20 + u, 30 + u)])
        self.assertEqual(len(values), len(set(values)))
        self.assertTrue(set(lists.number_set()) <= set(values))
        self.assertTrue(all(item.pos == "num" and item.raw == str(item.context["number"]) for item in items))

    def test_source_keeps_numeric_identity_and_million_article(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "numbers.csv"
            path.write_text(
                "english,italian\n23 / twenty-three,ventitré\n"
                "1000 / one thousand,mille\n1000000 / one million,un milione\n",
                encoding="utf-8",
            )
            items = lists.read(ListDef("numbers", "numbers", "Numbers", "Italian::Numbers", path))
        self.assertEqual([i.raw for i in items], ["23", "1000", "1000000"])
        self.assertEqual(items[-1].lemma, "un milione")
        self.assertEqual(items[1].english, "1,000 / one thousand")
        self.assertEqual(items[0].lemma, "ventitré")

    def test_bad_source_rows_fail_before_import(self):
        examples = [
            "eleven / 11,undici\n",
            "11 / ,undici\n",
            "11 / eleven,undici\n11 / eleven,undici\n",
            "218 / two hundred and eighteen,duecentodicotto\n",
            "23 / twenty-three,ventitre\n",
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "numbers.csv"
            source = ListDef("numbers", "numbers", "Numbers", "Italian::Numbers", path)
            for body in examples:
                with self.subTest(body=body):
                    path.write_text("english,italian\n" + body, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        lists.read(source)

    def test_configured_missing_file_is_not_silently_generated(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(lists, "load_plan", return_value={}):
            source = ListDef(
                "numbers", "numbers", "Numbers", "Italian::Numbers", Path(folder) / "missing.csv"
            )
            self.assertTrue(lists.validate([source]))
            with self.assertRaises(FileNotFoundError):
                lists.read(source)


if __name__ == "__main__":
    unittest.main()
