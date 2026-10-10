import json
import threading
import unittest
from unittest.mock import patch

from flashcards import reconcile, queue, ai, overrides
from flashcards.ownership import tags_for
from .support import database, lexeme, desired


class ReconcileTests(unittest.TestCase):
    def test_blocked_note_cannot_be_adopted(self):
        c = database(self)
        lexeme(c)
        f = desired(c, ready=False)
        old = reconcile.AnkiNote(10, "gloss:e|en_to_it", {}, [], [100], {"Italian::Test"})
        with (
            patch.object(reconcile, "owned_notes", return_value=[]),
            patch.object(reconcile, "owned_legacy", return_value=[old]),
            patch.object(reconcile, "_studied", return_value={100}),
            patch.object(reconcile, "legacy_target", return_value=(f["Key"], "Production")),
        ):
            plan = reconcile.build_plan(c, anki_media=set())
        self.assertEqual(plan.adopt, [])

    def test_failed_add_never_deletes_predecessor(self):
        c = database(self)
        lexeme(c)
        f = desired(c)
        old = reconcile.AnkiNote(10, "gloss:e|en_to_it", {}, [], [100], {"Italian::Test"})
        plan = reconcile.Plan(
            add=[
                {
                    "deckName": "Italian::Test",
                    "modelName": reconcile.MODEL_NAME,
                    "fields": f,
                    "tags": ["fc::owned"],
                }
            ],
            retire_legacy=[old],
        )
        plan.retire_dependencies[10] = (f["Key"], "Production")
        calls = []

        def invoke(action, **kw):
            calls.append(action)
            return {"getActiveProfile": "Test", "addNotes": [None]}.get(action, [])

        with (
            patch.object(reconcile, "invoke", side_effect=invoke),
            patch.object(reconcile, "ensure_model", return_value="ok"),
            patch.object(reconcile, "build_plan", return_value=plan),
            patch.object(reconcile, "pipeline_models", return_value=[]),
        ):
            reconcile.run(c, allow_retire=True)  # a rejected add is reported, not fatal
        self.assertNotIn("deleteNotes", calls)
        self.assertEqual(c.execute("SELECT status FROM sync_runs").fetchone()[0], "partial")
        self.assertEqual(plan.rejected, [f["Key"]])

    def test_multi_checks_nested_errors_and_version(self):
        with patch.object(reconcile, "invoke", return_value=[{"result": None, "error": "failure"}]) as invoke:
            with self.assertRaises(RuntimeError):
                reconcile._multi([{"action": "updateNoteFields"}])
            self.assertEqual(invoke.call_args.kwargs["actions"][0]["version"], 6)

    def test_profile_guard_precedes_model_mutation(self):
        c = database(self)
        c.execute("INSERT INTO metadata VALUES('anki_profile','Personal')")
        c.commit()
        with (
            patch.object(
                reconcile, "invoke", side_effect=lambda a, **k: "Other" if a == "getActiveProfile" else []
            ),
            patch.object(reconcile, "build_plan", return_value=reconcile.Plan()),
            patch.object(reconcile, "ensure_model") as model,
        ):
            with self.assertRaises(RuntimeError):
                reconcile.run(c)
            model.assert_not_called()

    def test_personal_and_leech_tags_survive(self):
        self.assertEqual(
            tags_for(["personal", "leech", "list::old"], ["list::new"]),
            ["fc::owned", "leech", "list::new", "personal"],
        )

    def test_unknown_v4_blocks_model_changes(self):
        c = database(self)
        lexeme(c)
        f = desired(c)
        note = reconcile.AnkiNote(10, f["Key"], f, [], [100], {"Other"})
        conflicts = []
        with (
            patch.object(reconcile, "invoke", return_value=[reconcile.MODEL_NAME]),
            patch.object(reconcile, "_notes", return_value=[note]),
        ):
            self.assertEqual(reconcile.owned_notes(c, conflicts=conflicts), [])
        self.assertTrue(conflicts)


class GenerationTests(unittest.TestCase):
    def test_schema_affects_cache(self):
        self.assertNotEqual(
            ai.Task("t", "s", "p", schema={"type": "string"}).cache_key(),
            ai.Task("t", "s", "p", schema={"type": "number"}).cache_key(),
        )

    def test_reset_error_releases_gate(self):
        gate = ai._Gate()
        gate.open.clear()
        with (
            patch.object(ai, "seconds_until_reset", return_value=1),
            patch.object(ai.STOP, "wait", return_value=False),
        ):
            with self.assertRaises(ai.AIError):
                gate._probe(
                    lambda: (_ for _ in ()).throw(ai.AIError("bad request")), ai.AIUnavailable("limit")
                )
        self.assertTrue(gate.open.is_set())

    def test_login_and_limit_errors_wait(self):
        # Asked for explicitly: limits, credits and logins pause and retry rather than fail jobs.
        for message in ("not logged in", "Credit balance is too low", "5-hour limit reached"):
            self.assertIsInstance(
                ai._classify(message, 401 if "logged" in message else None), ai.AIUnavailable
            )

    def test_bounded_wait_gives_up_and_releases_gate(self):
        from dataclasses import replace

        bounded = replace(
            ai.settings, ai=replace(ai.settings.ai, max_unavailable_minutes=1, unavailable_retry_minutes=5)
        )
        gate = ai._Gate()
        gate.open.clear()
        with patch.object(ai, "settings", bounded), patch.object(ai.STOP, "wait", return_value=False) as wait:
            with self.assertRaises(ai.AIUnavailable):
                gate._probe(lambda: None, ai.AIUnavailable("limit"))
            wait.assert_not_called()
        self.assertTrue(gate.open.is_set())

    def test_finished_jobs_are_not_reopened_by_their_own_output(self):
        c = database(self)
        lexeme(c)
        payload = {"id": "x", "italian": "la casa", "draft_fact": "old fact"}
        queue._enqueue(c, "lexeme_enrich", "x", 1, payload)
        c.execute("UPDATE ai_jobs SET status='done'")
        self.assertFalse(queue._enqueue(c, "lexeme_enrich", "x", 1, {**payload, "draft_fact": "new fact"}))
        self.assertEqual(c.execute("SELECT status FROM ai_jobs").fetchone()[0], "done")
        self.assertTrue(queue._enqueue(c, "lexeme_enrich", "x", 1, {**payload, "italian": "la casetta"}))

    def test_failed_jobs_stay_failed_when_reopened(self):
        c = database(self)
        lexeme(c)
        queue._enqueue(c, "image", "x", 1, {"image_key": "casa"})
        c.execute("UPDATE ai_jobs SET status='failed'")
        self.assertFalse(queue._enqueue(c, "image", "x", 1, {"image_key": "casa"}, reopen=True))
        self.assertEqual(c.execute("SELECT status FROM ai_jobs").fetchone()[0], "failed")

    def test_human_phrase_survives(self):
        c = database(self)
        lexeme(c, pos="phrase")
        overrides.set_override(c, "x", "senses", [{"prompt": "HUMAN"}])
        overrides.apply(c)
        queue._apply_phrase(c, {"id": "x", "prompt": "AI", "verified": True}, "fake")
        self.assertEqual(c.execute("SELECT prompt FROM senses WHERE active=1").fetchone()[0], "HUMAN")

    def test_foreign_and_missing_ids_are_rejected_atomically(self):
        c = database(self)
        lexeme(c, pos="phrase")
        c.execute(
            "INSERT INTO ai_jobs(kind,subject,payload) VALUES('phrase_enrich','x',?)",
            (json.dumps({"id": "x", "italian": "casa"}),),
        )
        c.commit()
        job = c.execute("SELECT * FROM ai_jobs").fetchone()

        class Fake:
            def run(self, *a, **kw):
                return {
                    "phrases": [
                        {
                            "id": "foreign",
                            "prompt": "oops",
                            "hint": "",
                            "note": "",
                            "verified": True,
                            "issues": [],
                        }
                    ]
                }

        with self.assertRaises(ValueError):
            queue._run_text_batch(Fake(), "phrase_enrich", [job], c, threading.Lock())
        self.assertEqual(c.execute("SELECT count(*) FROM senses").fetchone()[0], 0)
        self.assertEqual(c.execute("SELECT status FROM ai_jobs").fetchone()[0], "pending")

    def test_partial_apply_rolls_back(self):
        c = database(self)
        lexeme(c, pos="phrase")
        lexeme(c, "y", pos="phrase", lemma="altro")
        for lid in ("x", "y"):
            c.execute(
                "INSERT INTO ai_jobs(kind,subject,payload) VALUES('phrase_enrich',?,?)",
                (lid, json.dumps({"id": lid, "italian": lid})),
            )
        c.commit()
        jobs = c.execute("SELECT * FROM ai_jobs").fetchall()

        class Fake:
            def run(self, *a, **kw):
                return {
                    "phrases": [
                        {"id": lid, "prompt": lid, "hint": "", "note": "", "verified": True, "issues": []}
                        for lid in ("x", "y")
                    ]
                }

        original = queue._apply_phrase

        def fail(conn, p, model):
            if p["id"] == "y":
                raise ValueError("injected database failure")
            return original(conn, p, model)

        with patch.object(queue, "_apply_phrase", side_effect=fail):
            with self.assertRaises(ValueError):
                queue._run_text_batch(Fake(), "phrase_enrich", jobs, c, threading.Lock())
        self.assertEqual(c.execute("SELECT count(*) FROM senses").fetchone()[0], 0)


class BatchedRetirementTests(unittest.TestCase):
    def test_only_unstudied_owned_predecessors_are_deleted_in_one_call(self):
        c = database(self)
        free = reconcile.AnkiNote(10, "gloss:a|en_to_it", {}, [], [100], {"Italian::Test"})
        studied = reconcile.AnkiNote(11, "gloss:b|en_to_it", {}, [], [110], {"Italian::Test"})
        foreign = reconcile.AnkiNote(12, "gloss:c|en_to_it", {}, [], [120], {"Italian::Test"})
        plan = reconcile.Plan(retire_legacy=[free, studied, foreign])
        plan.retire_dependencies.update({10: None, 11: None, 12: None})
        calls = []

        def invoke(action, **kw):
            calls.append((action, kw))
            if action == "getActiveProfile":
                return "Test"
            if action == "notesInfo":
                return [
                    {"noteId": n.note_id, "fields": {"SortKey": {"value": n.key}}, "cards": n.cards}
                    for n in (free, studied, foreign)
                    if n.note_id in kw["notes"]
                ]
            if action == "findCards" and kw["query"].startswith("cid:"):
                return [110]
            if action == "getReviewsOfCards":
                return {}
            if action == "getDecks":
                return {"Italian::Test": [100, 110], "ZCam's Decks - Old": [120]}
            return []

        with (
            patch.object(reconcile, "invoke", side_effect=invoke),
            patch.object(reconcile, "ensure_model", return_value="ok"),
            patch.object(reconcile, "build_plan", return_value=plan),
            patch.object(reconcile, "owned_notes", return_value=[]),
            patch.object(reconcile, "owned_legacy", return_value=[]),
        ):
            reconcile.run(c, allow_retire=True)
        deletes = [kw["notes"] for action, kw in calls if action == "deleteNotes"]
        self.assertEqual(deletes, [[10]])

    def test_studied_lookup_is_limited_to_the_given_cards(self):
        with patch.object(
            reconcile, "invoke", side_effect=lambda a, **kw: [] if a == "findCards" else {}
        ) as inv:
            reconcile._studied([1, 2, 3])
        query = next(
            kw["query"] for (a,), kw in ((c.args, c.kwargs) for c in inv.call_args_list) if a == "findCards"
        )
        self.assertEqual(query, "cid:1,2,3 -is:new")
