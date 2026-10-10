import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from flashcards import paths, recovery, media_inventory, kaikki
from .support import database, lexeme


class InventoryTests(unittest.TestCase):
    def test_resume_unreferenced_assets_and_detect_changed_bytes_without_deleting(self):
        c = database(self)
        lexeme(c)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            media = root / "media"
            media.mkdir()
            for name in ("one.png", "two.png", "unused.png"):
                Image.new("RGB", (3, 3), "white").save(media / name)
            inventory = root / "inventory.sqlite"
            with patch.object(paths, "MEDIA_DIR", media):
                first = media_inventory.scan(c, out=inventory, limit=1)
                self.assertFalse(first["complete"])
                resumed = media_inventory.scan(c, out=inventory)
                self.assertEqual(
                    (resumed["checked"], resumed["reused"], resumed["unreferenced_preserved"]), (2, 1, 3)
                )
                self.assertTrue(resumed["complete"])
                (media / "one.png").write_bytes(b"bad")
                changed = media_inventory.scan(c, out=inventory, rehash=True)
                self.assertGreater(
                    changed["status"].get("changed", 0) + changed["status"].get("invalid", 0), 0
                )
                self.assertEqual(len(list(media.iterdir())), 3)
                self.assertEqual((media / "one.png").read_bytes(), b"bad")


class CompleteRecoveryTests(unittest.TestCase):
    def test_restore_includes_dictionary_code_runtime_and_anki_history_and_media(self):
        c = database(self)
        lexeme(c)
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            parent = Path(folder)
            root = parent / "source"
            root.mkdir()
            for name in ("media", "inputs", "lexicon", "data"):
                (root / name).mkdir()
            with closing(sqlite3.connect(root / "database.sqlite")) as target:
                c.backup(target)
            with closing(sqlite3.connect(root / "data" / "kaikki.sqlite")) as dictionary:
                dictionary.executescript(kaikki.SCHEMA)
                dictionary.execute("INSERT INTO meta VALUES('imported_at','fixture')")
                dictionary.commit()
            anki = root / "anki-profile"
            anki.mkdir()
            (anki / "collection.media").mkdir()
            with closing(sqlite3.connect(anki / "collection.anki2")) as collection:
                collection.executescript(
                    "CREATE TABLE revlog(id INTEGER PRIMARY KEY,cid INTEGER); INSERT INTO revlog VALUES(1,42); CREATE TABLE cards(id INTEGER PRIMARY KEY); INSERT INTO cards VALUES(42);"
                )
            (anki / "collection.media" / "studied.mp3").write_bytes(b"expensive preserved audio")
            Image.new("RGB", (3, 3), "white").save(root / "media" / "original.png")
            (root / "lists.toml").write_text(
                '[[lists]]\nid="numbers"\nkind="numbers"\ntitle="Numbers"\ndeck="Italian::Numbers"\npath="numbers.csv"\n'
            )
            # The manifest uses [[list]], matching the real parser.
            (root / "lists.toml").write_text(
                (root / "lists.toml").read_text().replace("[[lists]]", "[[list]]")
            )
            (root / "inputs" / "numbers.csv").write_text("english,italian\n11 / eleven,undici\n")
            (root / "plan.toml").write_text("new_cards_per_day=25\n")
            shutil.copytree(
                repo / "flashcards", root / "flashcards", ignore=shutil.ignore_patterns("__pycache__")
            )
            shutil.copy2(repo / "pyproject.toml", root / "pyproject.toml")
            with patch.multiple(
                paths,
                PROJECT_ROOT=root,
                DB_PATH=root / "database.sqlite",
                INPUTS_DIR=root / "inputs",
                LEXICON_DIR=root / "lexicon",
                MEDIA_DIR=root / "media",
            ):
                bundle = recovery.create(
                    parent / "bundle", include_media=True, anki_collection=anki / "collection.anki2"
                )
                manifest = recovery.verify(bundle)
                self.assertEqual(manifest["recovery_level"], "study-system")
                restored = recovery.restore(bundle, parent / "restored")
            self.assertEqual(
                (restored / "media" / "original.png").read_bytes(),
                (root / "media" / "original.png").read_bytes(),
            )
            self.assertEqual(
                (restored / "anki" / "collection.media" / "studied.mp3").read_bytes(),
                b"expensive preserved audio",
            )
            with closing(sqlite3.connect(restored / "anki" / "collection.anki2")) as history:
                self.assertEqual(history.execute("SELECT cid FROM revlog").fetchone()[0], 42)
            self.assertTrue(json.loads((restored / "runtime.json").read_text())["packages"])
            env = {**os.environ, "FLASHCARDS_ROOT": str(restored)}
            env.pop("FLASHCARDS_DB", None)
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from flashcards import db,lexicon,notes,kaikki; "
                    "c=db.connect(); db.init_schema(c); assert kaikki.available(); "
                    "lexicon.sync(c); notes.build(c); assert c.execute('select count(*) from v4_notes').fetchone()[0] > 0; c.close()",
                ],
                cwd=restored,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
