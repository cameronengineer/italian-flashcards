import json
import sqlite3
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from flashcards import (
    assets,
    cli,
    lexicon,
    lists,
    notes,
    overrides,
    paths,
    planning,
    quality,
    queue,
    recovery,
    reconcile,
)
from flashcards.domain import Item, ListDef
from flashcards.commands import audit, data_review, media, movie, share
from flashcards.util import md5_hex
from .support import database, desired, lexeme


class DataContractTests(unittest.TestCase):
    def test_movie_sync_preserves_token_counts_after_resolution(self):
        c = database(self)
        lexeme(c, pos="pron", lemma="tu")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "sample.srt"
            source.write_text("fixture")
            definition = ListDef("movie", "movie", "Movie", "Italian::Movie", source)
            items = [
                Item("ti", "ti", "pron", context={"count": 5, "forms": ["ti"], "first_seen": "0:05:00"}),
                Item("tu", "tu", "pron", context={"count": 3, "forms": ["tu"], "first_seen": "0:01:00"}),
            ]
            record = {
                "id": "x",
                "lemma": "tu",
                "pos": "pron",
                "display": "tu",
                "provenance": "{}",
                "in_kaikki": 1,
                "irregular": 0,
            }
            with (
                patch.object(paths, "PROJECT_ROOT", root),
                patch.object(lexicon, "INPUTS_DIR", root),
                patch.object(lists, "load", return_value=[definition]),
                patch.object(lists, "validate", return_value=[]),
                patch.object(lists, "read", return_value=items),
                patch.object(lexicon, "_resolve", return_value=record),
                patch.object(lexicon, "_assign_images"),
                patch.object(lexicon.kaikki, "available", return_value=True),
            ):
                lexicon.sync(c)
            context = json.loads(c.execute("SELECT context FROM list_items").fetchone()[0])
            self.assertEqual(context["count"], 8)
            self.assertEqual(context["first_seen"], "0:01:00")
            self.assertEqual(set(context["forms"]), {"ti", "tu"})

    def test_movie_coverage_maps_all_collapsed_source_roots(self):
        c = database(self)
        lexeme(c, pos="pron", lemma="tu")
        c.execute("UPDATE list_items SET raw='ti'")
        for i, word in enumerate(("tu", "ti")):
            c.execute(
                "INSERT INTO source_observations VALUES('test',?,'x',?,?,'hash','generation')",
                (str(i), word, json.dumps({"lemma": word, "pos": "pron"})),
            )
        self.assertEqual(movie.source_mappings(c, "test"), {("tu", "pron"): "x", ("ti", "pron"): "x"})

    def test_source_glosses_and_examples_survive_membership_collapse(self):
        c = database(self)
        lexeme(c)
        for i, gloss in enumerate(("house", "home")):
            payload = {"english": gloss, "context": {"example": f"example {i}"}}
            c.execute(
                "INSERT INTO source_observations VALUES('test',?,'x','casa',?,'hash','generation')",
                (str(i), json.dumps(payload)),
            )
        self.assertEqual(queue._list_glosses(c, "x"), ["house", "home"])
        self.assertEqual(queue._context_lines(c, "x"), ["example 0", "example 1"])

    def test_multi_sense_knowledge_uses_the_actual_recognition_card(self):
        c = database(self)
        lexeme(c)
        overrides.save_senses(c, "x", [{"prompt": "house"}, {"prompt": "home"}], "human")
        c.execute(
            "INSERT INTO card_knowledge(key,direction,card_id,state,interval,lapses,reviews,suspended) "
            "VALUES('vocab:x:0','Recognition',1,'known',30,0,10,0)"
        )
        self.assertEqual(planning.known_roots(c), {"x"})
        c.execute("UPDATE card_knowledge SET suspended=1")
        self.assertEqual(planning.known_roots(c), set())

    def test_archived_first_sense_does_not_supply_knowledge(self):
        c = database(self)
        lexeme(c)
        overrides.save_senses(c, "x", [{"prompt": "house"}, {"prompt": "home"}], "human")
        c.execute("UPDATE senses SET active=0 WHERE idx=0")
        c.execute(
            "INSERT INTO card_knowledge(key,direction,card_id,state,interval,lapses,reviews,suspended) "
            "VALUES('vocab:x:0','Recognition',1,'known',30,0,10,0)"
        )
        self.assertEqual(planning.known_roots(c), set())

    def test_deterministic_number_knowledge_needs_no_generated_sense(self):
        c = database(self)
        lexeme(c, pos="num", lemma="due", status="new")
        desired(c)
        c.execute(
            "INSERT INTO card_knowledge(key,direction,card_id,state,interval,lapses,reviews,suspended) "
            "VALUES('vocab:x:0','Recognition',1,'known',30,0,10,0)"
        )
        self.assertEqual(planning.known_roots(c), {"x"})

    def test_refresh_holds_old_content_locally_until_reverified(self):
        c = database(self)
        lexeme(c)
        fields = desired(c)
        queue.refresh(c, "test")
        self.assertIn("awaiting verification", quality.reasons(c, fields["Key"], "x", fields))
        self.assertEqual(c.execute("SELECT count(*) FROM v4_notes").fetchone()[0], 1)

    def test_pending_verb_job_blocks_old_prompt(self):
        c = database(self)
        lexeme(c, pos="verb", lemma="parlare")
        fields = desired(c, key="form:x:presente:io")
        c.execute("INSERT INTO ai_jobs(kind,subject,status) VALUES('verb_prompts','x','pending')")
        self.assertIn("awaiting verb prompts", quality.reasons(c, fields["Key"], "x", fields))

    def test_stale_generation_blocks_publication_and_audio_selection(self):
        c = database(self)
        lexeme(c)
        desired(c)
        c.execute("INSERT INTO metadata VALUES('source_generation','new')")
        for check in (quality.require_current_notes, media._audio_texts):
            with self.assertRaisesRegex(ValueError, "Notes are stale"):
                check(c)
        c.execute("INSERT INTO metadata VALUES('notes_generation','new')")
        quality.require_current_notes(c)

    def test_note_rebuild_failure_rolls_back_previous_view(self):
        c = database(self)
        lexeme(c)
        desired(c)
        c.execute(
            "CREATE TRIGGER fail_build BEFORE INSERT ON v4_notes BEGIN SELECT RAISE(ABORT,'test failure'); END"
        )
        c.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            notes.build(c)
        self.assertFalse(c.in_transaction)
        self.assertEqual(c.execute("SELECT count(*) FROM v4_notes").fetchone()[0], 1)

    def test_audit_rejects_duplicate_missing_and_foreign_ids(self):
        batch = [("a", "Italian::Test", {}), ("b", "Italian::Test", {})]
        verdict = {
            "id": "a",
            "verdict": "pass",
            "severity": 0,
            "categories": [],
            "issues": "",
            "suggestion": "",
        }
        for ids in (("a", "a"), ("a",), ("a", "foreign")):
            with self.assertRaises(ValueError):
                audit._validated_verdicts(batch, {"cards": [{**verdict, "id": i} for i in ids]})
        self.assertEqual(
            set(audit._validated_verdicts(batch, {"cards": [verdict, {**verdict, "id": "b"}]})), {"a", "b"}
        )

    def test_offline_census_is_read_only_and_contains_no_subtitle_text(self):
        c = database(self)
        lexeme(c)
        desired(c)
        c.execute("UPDATE list_items SET context=?", (json.dumps({"example": "PRIVATE SUBTITLE"}),))
        c.commit()
        before = c.total_changes
        result = data_review.report(c, check_media=False)
        self.assertEqual(before, c.total_changes)
        self.assertEqual(result["ready_count"], 1)
        self.assertNotIn("PRIVATE SUBTITLE", json.dumps(result))
        self.assertIn("unaudited_ready_notes", {r["code"] for r in result["findings"]})

    def test_offline_census_reports_stale_ready_flags(self):
        c = database(self)
        lexeme(c, status="needs_review")
        desired(c)
        result = data_review.report(c, check_media=False)
        self.assertEqual(result["status"], "error")
        self.assertIn("readiness_conflicts", {r["code"] for r in result["findings"]})


class MediaPreservationTests(unittest.TestCase):
    def test_legacy_compressed_image_remains_usable_after_settings_change(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            jpg = root / (md5_hex("casa") + ".jpg")
            Image.new("RGB", (4, 4), "white").save(jpg)
            before = jpg.read_bytes()
            from flashcards.settings import settings

            custom = replace(settings, images=replace(settings.images, compressed_quality=60))
            with (
                patch.multiple(
                    paths,
                    IMAGE_DIR_COMPRESSED=root,
                    IMAGE_DIR=root,
                    AUDIO_DIR=root,
                    AUDIO_DIR_COMPRESSED=root,
                ),
                patch("flashcards.settings.settings", custom),
            ):
                self.assertEqual(assets.image_for("casa"), jpg)
                self.assertTrue(lexicon.image_exists("casa"))
                self.assertIn(jpg.name, notes._img("casa"))
            self.assertEqual(jpg.read_bytes(), before)

    def test_invalid_existing_compressed_image_is_preserved_and_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            src = root / "test.png"
            dest = root / "test.jpg"
            Image.new("RGB", (4, 4), "white").save(src)
            dest.write_bytes(b"not an image")
            with (
                patch.object(media, "IMAGE_DIR_COMPRESSED", root),
                patch.object(media, "image_compression_suffix", return_value=""),
            ):
                with self.assertRaisesRegex(ValueError, "preserved"):
                    media._compress_image(src)
            self.assertEqual(dest.read_bytes(), b"not an image")
            self.assertTrue(src.is_file())


class RecoveryTests(unittest.TestCase):
    def test_portable_bundle_restores_identical_media_and_rejects_damage(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "workspace"
            root.mkdir()
            for name in ("media", "inputs", "lexicon"):
                (root / name).mkdir()
            db_path = root / "database.sqlite"
            c = database(self)
            lexeme(c)
            with sqlite3.connect(db_path) as target:
                c.backup(target)
            target.close()
            media_file = root / "media" / "expensive.png"
            Image.new("RGB", (4, 4), "white").save(media_file)
            original = media_file.read_bytes()
            (root / "settings.toml").write_text("[images]\nrequired=true\n")
            with patch.multiple(
                paths,
                PROJECT_ROOT=root,
                DB_PATH=db_path,
                MEDIA_DIR=root / "media",
                INPUTS_DIR=root / "inputs",
                LEXICON_DIR=root / "lexicon",
            ):
                bundle = recovery.create(Path(folder) / "bundle", include_media=True)
                dest = recovery.restore(bundle, Path(folder) / "restored")
                self.assertEqual((dest / "media" / "expensive.png").read_bytes(), original)
                self.assertEqual(media_file.read_bytes(), original)
                with self.assertRaises(ValueError):
                    recovery.restore(bundle, dest)
                (bundle / "media" / "expensive.png").write_bytes(b"damaged")
                with self.assertRaisesRegex(ValueError, "Missing or changed media"):
                    recovery.verify(bundle)

    def test_bundle_cannot_copy_itself_recursively(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "inputs"
            with patch.object(paths, "INPUTS_DIR", source):
                with self.assertRaisesRegex(ValueError, "copying itself"):
                    recovery.create(source / "new-bundle")
            self.assertFalse(source.exists())


class OutputTests(unittest.TestCase):
    def test_share_package_has_both_directions_and_strips_private_example(self):
        c = database(self)
        lexeme(c)
        fields = desired(c)
        fields["Example"] = "PRIVATE SUBTITLE"
        fields["Image"] = '<img src="fixture.png">'
        c.execute("UPDATE v4_notes SET fields_json=?", (json.dumps(fields),))
        c.commit()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            asset = root / "fixture.png"
            Image.new("RGB", (4, 4), "white").save(asset)
            output = root / "shared.apkg"
            with (
                patch.object(share, "connect", return_value=c),
                patch.object(share, "DECKS_DIR", root),
                patch.object(share, "locate", return_value=asset),
            ):
                self.assertEqual(share.run(out=output), 0)
            with zipfile.ZipFile(output) as package:
                media_map = json.loads(package.read("media"))
                self.assertIn("fixture.png", media_map.values())
                collection = root / "collection.anki2"
                collection.write_bytes(package.read("collection.anki2"))
            from contextlib import closing

            with closing(sqlite3.connect(collection)) as exported:
                self.assertEqual(exported.execute("SELECT count(*) FROM cards").fetchone()[0], 2)
                rendered = exported.execute("SELECT flds FROM notes").fetchone()[0]
                self.assertNotIn("PRIVATE SUBTITLE", rendered)
                self.assertIn("vocab:x:0", rendered)
            manifest = json.loads(output.with_suffix(".manifest.json").read_text())
            self.assertEqual(manifest["notes"], 1)
            self.assertFalse(manifest["private_included"])

    def test_partial_anki_apply_has_partial_exit_status(self):
        c = database(self)
        with (
            patch.object(cli, "_conn", return_value=c),
            patch.object(notes, "build"),
            patch.object(lexicon, "export_jsonl"),
            patch.object(reconcile, "run", return_value=reconcile.Plan(rejected=["vocab:x:0"])),
        ):
            self.assertEqual(cli.cmd_apply(SimpleNamespace(allow_retire=False)), 2)

    def test_invalid_run_configuration_stops_before_work(self):
        from flashcards import backup

        from flashcards import lists

        args = cli.build_parser().parse_args(["run"])
        with (
            patch.object(lists, "validate", return_value=["broken"]),
            patch.object(backup, "snapshot") as snapshot,
        ):
            self.assertEqual(cli.cmd_run(args), 1)
        snapshot.assert_not_called()

    def test_missing_review_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / "missing.sqlite"
            self.assertEqual(cli.main(["data-review", "--database", str(missing)]), 1)
            self.assertFalse(missing.exists())
