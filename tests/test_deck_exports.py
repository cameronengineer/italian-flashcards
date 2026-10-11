import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from flashcards import reconcile, content_review, quality
from flashcards.commands import share
from .support import database, desired, lexeme


class DeckExportTests(unittest.TestCase):
    def fixture(self):
        c = database(self)
        lexeme(c)
        f = desired(c)
        f.update(Audio="[sound:fixture.mp3]", Example="Film line with its translation")
        c.execute("UPDATE v4_notes SET fields_json=?", (json.dumps(f),))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        audio = root / "fixture.mp3"
        audio.write_bytes(b"test audio")
        for target, value in (
            ("DECKS_DIR", root / "decks"),
            ("locate", lambda name: audio if audio.exists() else None),
        ):
            patcher = patch.object(share, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return c, root, audio

    def test_packages_include_content_media_and_dormancy_and_are_stable(self):
        c, root, audio = self.fixture()
        c.execute(
            "UPDATE v4_notes SET deck='Italian::Verbs::Dormant::Condizionale Presente', tags='fc::dormant'"
        )
        result = share.export_decks(c)
        self.assertEqual(len(result["written"]), 1)
        package = result["written"][0]
        with zipfile.ZipFile(package) as z:
            mapping = json.loads(z.read("media"))
            self.assertEqual(list(mapping.values()), ["fixture.mp3"])
            self.assertEqual(z.read(next(iter(mapping))), audio.read_bytes())
            path = root / "export.sqlite"
            path.write_bytes(z.read("collection.anki2"))
        with sqlite3.connect(path) as saved:
            self.assertEqual(saved.execute("SELECT DISTINCT queue FROM cards").fetchall(), [(-1,)])
            self.assertIn(
                "Film line with its translation", saved.execute("SELECT flds FROM notes").fetchone()[0]
            )
        stamp = package.stat().st_mtime_ns
        self.assertEqual(share.export_decks(c)["unchanged"], 1)
        self.assertEqual(package.stat().st_mtime_ns, stamp)

    def test_media_template_and_sort_changes_rebuild_and_missing_media_retains_package(self):
        c, _root, audio = self.fixture()
        package = share.export_decks(c)["written"][0]
        audio.write_bytes(b"changed audio")
        self.assertEqual(len(share.export_decks(c)["written"]), 1)
        with patch.object(share, "CSS", share.CSS + "\n.card { color: red; }"):
            self.assertEqual(len(share.export_decks(c)["written"]), 1)
        c.execute("UPDATE v4_notes SET sort_order=20")
        self.assertEqual(len(share.export_decks(c)["written"]), 1)
        before = package.read_bytes()
        with patch.object(share, "locate", return_value=None):
            result = share.export_decks(c)
        self.assertEqual(result["missing_media"], 1)
        self.assertEqual(result["written"], [])
        self.assertEqual(package.read_bytes(), before)

    def test_empty_deck_is_archived_and_manual_exports_are_preserved(self):
        c, root, _audio = self.fixture()
        package = share.export_decks(c)["written"][0]
        before = package.read_bytes()
        manual = root / "decks" / "my-export.apkg"
        manual.write_bytes(b"personal")
        c.execute("DELETE FROM v4_notes")
        self.assertEqual(share.export_decks(c)["archived"], 1)
        self.assertEqual(next((root / "decks" / "archive").glob("*.apkg")).read_bytes(), before)
        self.assertEqual(manual.read_bytes(), b"personal")
        self.assertFalse(package.exists())


class DormancyTests(unittest.TestCase):
    def test_only_pipeline_suspensions_are_reactivated_and_reruns_are_idempotent(self):
        c = database(self)
        lexeme(c, pos="verb")
        key = "form:x:condizionale_presente:io"
        desired(c, key=key)
        c.execute("UPDATE v4_notes SET card_type='form',tags='fc::dormant'")
        states = {100: 2, 101: -1, 102: -1}  # studied, manually suspended, leech
        note = reconcile.AnkiNote(
            10, key, {}, [], list(states), cards_info={cid: {"queue": q} for cid, q in states.items()}
        )
        calls = []

        def invoke(action, **kw):
            calls.append(action)
            if action == "notesInfo":
                return [{"noteId": 10, "cards": list(states), "tags": []}]
            if action == "cardsInfo":
                return [{"cardId": cid, "queue": states[cid]} for cid in kw["cards"]]
            if action in {"suspend", "unsuspend"}:
                for cid in kw["cards"]:
                    states[cid] = -1 if action == "suspend" else 2
                return True
            raise AssertionError(action)

        with patch.object(reconcile, "invoke", side_effect=invoke):
            plan = reconcile.Plan(v4={key: note}, profile="Test")
            reconcile._sync_dormancy(c, plan, {key: 10})
            self.assertEqual(plan.suspended, 1)
            self.assertEqual(states, {100: -1, 101: -1, 102: -1})
            self.assertEqual(note.cards_info[100]["queue"], -1)
            reconcile._sync_dormancy(c, plan, {key: 10})
            self.assertEqual(calls.count("suspend"), 1)
            c.execute("UPDATE v4_notes SET tags='verb-form'")
            reconcile._sync_dormancy(c, plan, {key: 10})
            self.assertEqual(states, {100: 2, 101: -1, 102: -1})
            self.assertEqual(plan.resumed, 1)
            reconcile._sync_dormancy(c, plan, {key: 10})
            self.assertEqual(calls.count("unsuspend"), 1)

    def test_new_and_adopted_dormant_notes_are_suspended(self):
        c = database(self)
        lexeme(c, pos="verb")
        key = "form:x:condizionale_presente:io"
        fields = desired(c, key=key)
        c.execute("UPDATE v4_notes SET card_type='form',tags='fc::dormant'")
        states = {100: 0, 200: 2}
        plan = reconcile.Plan(replacements={(key, "Production"): (20, fields, "Italian::Verbs::Dormant")})

        def invoke(action, **kw):
            if action == "notesInfo":
                return [{"noteId": nid, "cards": [nid * 10], "tags": []} for nid in kw["notes"]]
            if action == "cardsInfo":
                return [{"cardId": cid, "queue": states[cid]} for cid in kw["cards"]]
            if action == "suspend":
                states.update(dict.fromkeys(kw["cards"], -1))
                return True
            raise AssertionError(action)

        with patch.object(reconcile, "invoke", side_effect=invoke):
            reconcile._sync_dormancy(c, plan, {key: 10})
        self.assertEqual(states, {100: -1, 200: -1})
        self.assertEqual(plan.suspended, 2)

    def test_interruption_after_suspension_can_resume_and_leech_is_not_reactivated(self):
        c = database(self)
        lexeme(c, pos="verb")
        key = "form:x:condizionale_presente:io"
        desired(c, key=key)
        c.execute("UPDATE v4_notes SET card_type='form',tags='fc::dormant'")
        state = {"queue": 2, "leech": False, "interrupt": True}

        def invoke(action, **kw):
            if action == "notesInfo":
                return [{"noteId": 10, "cards": [100], "tags": ["leech"] if state["leech"] else []}]
            if action == "cardsInfo":
                return [{"cardId": 100, "queue": state["queue"]}]
            if action == "suspend":
                state["queue"] = -1
                if state.pop("interrupt", False):
                    raise KeyboardInterrupt
                return True
            if action == "unsuspend":
                state["queue"] = 2
                return True
            raise AssertionError(action)

        with patch.object(reconcile, "invoke", side_effect=invoke):
            with self.assertRaises(KeyboardInterrupt):
                reconcile._sync_dormancy(c, reconcile.Plan(profile="Test"), {key: 10})
            checkpoint = c.execute(
                "SELECT value FROM metadata WHERE key='dormant_suspensions:Test'"
            ).fetchone()[0]
            self.assertEqual(json.loads(checkpoint), {"100": 10})
            reconcile._sync_dormancy(c, reconcile.Plan(profile="Test"), {key: 10})
            c.execute("UPDATE v4_notes SET tags='verb-form'")
            state["leech"] = True
            reconcile._sync_dormancy(c, reconcile.Plan(profile="Test"), {key: 10})
            self.assertEqual(state["queue"], -1)
            state["leech"] = False
            reconcile._sync_dormancy(c, reconcile.Plan(profile="Test"), {key: 10})
            self.assertEqual(state["queue"], 2)

    def test_saved_cue_corrections_survive_retiring_the_manual_review_workflow(self):
        c = database(self)
        lexeme(c)
        fields = desired(c)
        c.execute(
            "INSERT INTO cue_overrides(key,content_hash,hint,alternatives,reason) VALUES(?,?,?,?,?)",
            (fields["Key"], quality.note_hash(fields), "dwelling", "home", "prior correction"),
        )
        content_review.apply_overrides(c, fields["Key"], fields)
        self.assertEqual(fields["Hint"], "dwelling")
        self.assertIn("Also accepted: home", fields["Note"])
