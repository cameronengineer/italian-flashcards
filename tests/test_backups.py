"""Backup retention: the newest few, plus the first of each recent day."""

import unittest
from datetime import date
from pathlib import Path

from flashcards.backup import expired


def names(paths):
    return sorted(p.name for p in paths)


class RetentionTests(unittest.TestCase):
    today = date(2026, 11, 20)

    def db(self, stamp):
        return Path(f"database.backup.{stamp}_abcd1234.sqlite")

    def test_first_of_each_recent_day_survives_a_busy_day(self):
        files = [self.db(f"20261117_17{m:02d}00") for m in range(5)]  # the day of an upgrade
        files += [self.db(f"20261120_{h:02d}0000") for h in range(8, 20)]  # twelve runs today
        gone = expired(files, keep_last=10, keep_days=14, today=self.today)
        kept = set(files) - set(gone)
        self.assertIn(self.db("20261117_170000"), kept)  # the state before that day
        self.assertIn(self.db("20261120_080000"), kept)
        self.assertEqual(len(kept), 12)  # newest ten + the first of each of the two days
        self.assertNotIn(self.db("20261117_170400"), kept)

    def test_days_outside_the_window_go(self):
        files = [self.db("20261001_090000"), self.db("20261120_090000")]
        self.assertEqual(names(expired(files, keep_last=1, keep_days=14, today=self.today)), [files[0].name])

    def test_anki_names_and_unknown_files(self):
        files = [Path(f"collection.User_1.2026111{d}_120000.zip") for d in range(7, 10)] + [Path("notes.txt")]
        gone = expired(files, keep_last=1, keep_days=0, today=self.today)
        self.assertEqual(
            names(gone), ["collection.User_1.20261117_120000.zip", "collection.User_1.20261118_120000.zip"]
        )


if __name__ == "__main__":
    unittest.main()
