"""Regressions for the seven findings in the 10 October follow-up review."""

import csv
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing, ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flashcards import (
    content_review,
    db,
    lists,
    notes,
    overrides,
    paths,
    pipeline,
    planning,
    quality,
    queue,
    reconcile,
    semantics,
    kaikki,
)
from flashcards.commands import review, movie
from .support import database, lexeme, desired


class ApprovalTests(unittest.TestCase):
    def test_stale_approval_is_rejected_and_mark_retained_for_review(self):
        c = database(self)
        lexeme(c, status="needs_review")
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(review, "REVIEW_FILE", Path(folder) / "review.csv"),
        ):
            review.write_review_file(c)
            review.REVIEW_FILE.write_text(review.REVIEW_FILE.read_text().replace("\n,casa,", "\nyes,casa,"))
            c.execute("UPDATE lexemes SET gender='masculine',issues='[\"changed issue\"]'")
            c.commit()
            self.assertEqual(review.apply_review_file(c), 0)
            review.write_review_file(c)
            with review.REVIEW_FILE.open() as fh:
                row = next(csv.DictReader(fh))
            self.assertEqual(row["approve"], "")
            self.assertIn("Previous mark 'yes'", row["review_status"])
            self.assertIn("changed issue", row["why_held"])
            self.assertEqual(c.execute("SELECT status FROM lexemes").fetchone()[0], "needs_review")

    def test_changed_sense_grammar_invalidates_exported_approval(self):
        c = database(self)
        lexeme(c, status="needs_review")
        overrides.save_senses(c, "x", [{"prompt": "house"}], "human")
        row = c.execute("SELECT * FROM lexemes").fetchone()
        before = review.content_hash(c, row)
        c.execute('UPDATE senses SET features=\'{"plural_gender":"masculine"}\'')
        self.assertNotEqual(before, review.content_hash(c, row))


class MeaningTests(unittest.TestCase):
    def test_new_meaning_cannot_recycle_studied_identity(self):
        c = database(self)
        lexeme(c)
        overrides.save_senses(c, "x", [{"prompt": "house"}], "claude:test")
        c.execute("INSERT INTO identity(key,guid,kind,note_id) VALUES('vocab:x:0','','v4',7)")
        overrides.save_senses(c, "x", [{"prompt": "legislative chamber"}], "claude:test")
        rows = list(c.execute("SELECT idx,prompt,active FROM senses ORDER BY idx"))
        self.assertEqual([tuple(r) for r in rows], [(0, "house", 0), (1, "legislative chamber", 1)])
        self.assertEqual(c.execute("SELECT note_id FROM identity").fetchone()[0], 7)
        self.assertEqual(semantics.form_suffix(semantics.primary(c, "x")), ":sense-1")

    def test_explicit_paraphrase_requires_review_and_keeps_key(self):
        c = database(self)
        lexeme(c)
        overrides.save_senses(c, "x", [{"prompt": "house"}], "human")
        with self.assertRaisesRegex(ValueError, "same_meaning"):
            overrides.save_senses(c, "x", [{"idx": 0, "prompt": "home"}], "human")
        overrides.save_senses(c, "x", [{"idx": 0, "prompt": "home", "same_meaning": True}], "human")
        self.assertEqual(semantics.primary(c, "x")["idx"], 0)

    def test_dictionary_homonyms_keep_distinct_stable_evidence(self):
        entries = [
            dict(word="zecca", pos="noun", etymology_text=etym, senses=[{"glosses": [gloss]}])
            for etym, gloss in [("A", "mint"), ("B", "tick")]
        ]
        a = semantics.dictionary_candidates(entries)
        self.assertEqual(a, list(reversed(semantics.dictionary_candidates(list(reversed(entries))))))
        self.assertNotEqual(a[0]["source_id"], a[1]["source_id"])


class GrammarTests(unittest.TestCase):
    def test_event_meaning_excludes_personal_forms_but_succession_does_not(self):
        lx = {"lemma": "succedere"}
        for word in ("to happen", "to occur"):
            sense = {"prompt": word, "features": "{}"}
            self.assertTrue(semantics.exclusion(lx, sense, "presente", "io"))
            self.assertTrue(semantics.exclusion(lx, sense, "presente", "noi"))
            self.assertIsNone(semantics.exclusion(lx, sense, "presente", "loro"))
            self.assertEqual(semantics.usage(lx, sense)["auxiliary"], "essere")
        self.assertIsNone(
            semantics.exclusion(lx, {"prompt": "to succeed someone", "features": "{}"}, "presente", "io")
        )

    def test_incomplete_response_cannot_finish_but_explicit_exclusion_can(self):
        c = database(self)
        lexeme(c, pos="verb", lemma="dovere")
        payload = {
            "id": "x",
            "infinitive": "dovere",
            "source_id": "meaning:must",
            "forms": {"presente": {"io": "devo"}, "imperativo": {"tu": "devi"}},
        }
        c.execute(
            "INSERT INTO ai_jobs(kind,subject,payload) VALUES('verb_prompts','x',?)", (json.dumps(payload),)
        )
        c.commit()
        job = c.execute("SELECT * FROM ai_jobs").fetchone()
        result = {
            "verbs": [
                {
                    "id": "x",
                    "prompts": [{"tense": "presente", "person": "io", "english": "I must"}],
                    "exclusions": [],
                }
            ]
        }
        fake = SimpleNamespace(run=lambda *a, **k: result)
        with self.assertRaisesRegex(ValueError, "Every requested"):
            queue._run_text_batch(fake, "verb_prompts", [job], c, threading.Lock())
        self.assertEqual(c.execute("SELECT status FROM ai_jobs").fetchone()[0], "pending")
        result["verbs"][0]["exclusions"] = [
            {"tense": "imperativo", "person": "tu", "reason": "No ordinary imperative of dovere"}
        ]
        queue._run_text_batch(fake, "verb_prompts", [job], c, threading.Lock())
        self.assertEqual(c.execute("SELECT source_id FROM form_prompts").fetchone()[0], "meaning:must")
        self.assertIn("imperative", c.execute("SELECT reason FROM form_exclusions").fetchone()[0])
        self.assertEqual(c.execute("SELECT status FROM ai_jobs").fetchone()[0], "done")

    def test_head_gender_ignores_derivatives_and_plural_tracks_own_gender(self):
        self.assertEqual(
            kaikki.gender({"head": ["camera f (plural camere, diminutive camerino m)"]}), "feminine"
        )
        entry = {
            "word": "braccio",
            "head": ["braccio m (plural braccia f or bracci m)"],
            "forms": [
                {"form": "braccia", "tags": ["plural", "feminine"]},
                {"form": "bracci", "tags": ["plural", "masculine"]},
            ],
        }
        self.assertEqual(kaikki.gender(entry), "masculine")
        lx = {
            "lemma": "braccio",
            "gender": "masculine",
            "plural": "braccia",
            "english_plural": "arms",
            "forms_json": json.dumps({"noun_forms": kaikki.noun_forms(entry)}),
        }
        sense = {"prompt": "arm", "features": "{}"}
        self.assertIsNone(semantics.noun_variant(lx, sense, True))  # choose meaning first
        sense["features"] = json.dumps(
            {"plural": "braccia", "plural_gender": "feminine", "english_plural": "arms"}
        )
        self.assertEqual(semantics.noun_variant(lx, sense, True), ("braccia", "feminine", "arms"))

    def test_reflexive_and_dual_auxiliary_compounds(self):
        reflexive = {
            "lemma": "lavarsi",
            "forms_json": json.dumps({"auxiliary": "essere", "reflexive": True, "past_participle": "lavato"}),
        }
        with patch.object(planning, "plan_tenses", return_value=["passato_prossimo"]):
            self.assertIn("mi sono", planning.verb_forms(reflexive)["passato_prossimo"]["io"][0])
            dual = {
                "lemma": "passare",
                "forms_json": json.dumps({"auxiliary": "both", "past_participle": "passato"}),
            }
            self.assertEqual(planning.verb_forms(dual), {})
            sense = {"prompt": "to pass through", "features": json.dumps({"auxiliary": "essere"})}
            self.assertIn("sono", planning.verb_forms(dual, sense)["passato_prossimo"]["io"][0])


class RetirementTests(unittest.TestCase):
    def test_both_adopted_directions_allow_duplicate_candidate_without_v4(self):
        c = database(self)
        lexeme(c)
        fields = desired(c)
        olds = []
        for nid, direction, studied in [
            (10, "it_to_en", False),
            (20, "it_to_en", True),
            (30, "en_to_it", True),
        ]:
            n = reconcile.AnkiNote(nid, f"gloss:e|{direction}", {}, [], [nid * 10], {"Italian::Test"})
            n.cards_info = {nid * 10: {"type": 2 if studied else 0, "reps": 10 if studied else 0}}
            olds.append(n)

        def target(key):
            return fields["Key"], "Recognition" if key.endswith("it_to_en") else "Production"

        with (
            patch.object(reconcile, "owned_notes", return_value=[]),
            patch.object(reconcile, "owned_legacy", return_value=olds[1:]),
            patch.object(reconcile, "legacy_index", return_value={10: (olds[0].key, [100])}),
            patch.object(reconcile, "legacy_targets", return_value=target),
        ):
            plan = reconcile.build_plan(c, anki_media=set())
        self.assertEqual(plan.add, [])
        self.assertEqual([n.note_id for n in plan.retire_legacy], [10])
        self.assertEqual(len(plan.replacements), 2)

    def test_disappeared_adopted_replacement_prevents_deletion(self):
        c = database(self)
        old = reconcile.AnkiNote(10, "gloss:e|it_to_en", {}, [], [100], {"Italian::Test"})
        dep = ("vocab:x:0", "Recognition")
        plan = reconcile.Plan(
            retire_legacy=[old],
            retire_dependencies={10: dep},
            served={dep},
            replacements={dep: (20, {"SortKey": "gloss:b|it_to_en", "FrontText": "casa"}, "Italian::Test")},
        )
        with patch.object(reconcile, "invoke", return_value=[]) as invoke:
            with self.assertRaisesRegex(RuntimeError, "read-back"):
                reconcile._retire(c, plan, {}, {}, True)
        self.assertFalse(any(c.args[0] == "deleteNotes" for c in invoke.call_args_list))

    def test_verified_adopted_home_allows_unstudied_duplicate_cleanup(self):
        c = database(self)
        old = reconcile.AnkiNote(10, "gloss:e|it_to_en", {}, [], [100], {"Italian::Test"})
        dep = ("vocab:x:0", "Recognition")
        expected = {"SortKey": "gloss:b|it_to_en", "FrontText": "casa"}
        plan = reconcile.Plan(
            retire_legacy=[old],
            retire_dependencies={10: dep},
            replacements={dep: (20, expected, "Italian::Test")},
        )
        calls = []

        def invoke(action, **kw):
            calls.append(action)
            if action == "notesInfo":
                return [
                    {
                        "noteId": n,
                        "fields": {
                            k: {"value": v}
                            for k, v in (expected if n == 20 else {"SortKey": old.key}).items()
                        },
                        "cards": [n * 10],
                    }
                    for n in kw["notes"]
                ]
            if action == "cardsInfo":
                return [{"note": 20, "cardId": 200, "ord": 0}]
            if action == "getDecks":
                return {"Italian::Test": kw["cards"]}
            if action == "getReviewsOfCards":
                return {}
            return []

        with patch.object(reconcile, "invoke", side_effect=invoke):
            self.assertEqual(reconcile._retire(c, plan, {}, {}, True), 1)
        self.assertIn("deleteNotes", calls)


class PlanningTests(unittest.TestCase):
    def test_shared_plan_counts_actual_drills_and_adopted_directions(self):
        c = database(self)
        lexeme(c)
        candidates = [
            {"key": key, "lexeme_id": "x", "card_type": kind, "sort": i, "fields": fields}
            for i, (key, kind, fields) in enumerate(
                [
                    ("vocab:x:0", "vocab", {"Recognition": "1", "Production": "1"}),
                    ("form:x:presente:io", "form", {"Recognition": "", "Production": "1"}),
                ]
            )
        ]
        c.execute("INSERT INTO adoptions VALUES('vocab:x:0','Recognition',1,'Test')")
        with patch.object(planning, "horizon_budget", return_value=1):
            plan = planning.card_plan(c, candidates)
        self.assertEqual(plan["new_directions"], 1)
        self.assertEqual(plan["keys"], {"vocab:x:0"})
        self.assertEqual(plan["verbs"], set())
        self.assertEqual(plan["roots"], {"x"})

    def test_published_verb_family_stays_when_root_becomes_known(self):
        c = database(self)
        for lid in ("a", "b"):
            lexeme(c, lid, pos="verb", lemma=lid, forms="{}")
        c.execute("UPDATE lexemes SET irregular=1")
        c.execute("INSERT INTO adoptions VALUES('form:a:presente:io','Production',1,'Test')")
        with (
            patch.object(
                planning,
                "load_plan",
                return_value={"cards": {"patterns": False}, "conjugation": {"max_full_verbs": 1}},
            ),
            patch.object(planning, "study_order", return_value={"a": 999999, "b": 1}),
        ):
            self.assertEqual(planning.full_form_verbs(c), {"a"})

    def test_order_changes_signature_and_movie_forecast_counts_directions(self):
        c = database(self)
        lexeme(c)
        desired(c)
        before = notes.signature(c)
        c.execute("UPDATE v4_notes SET sort_order=2")
        self.assertNotEqual(before, notes.signature(c))
        target = movie.coverage({"x": 9, "y": 1}, set(), 0, 2, {"x": 5, "y": 2})["targets"][0]
        self.assertEqual((target["roots"], target["new_cards"], target["days"]), (1, 5, 3))


class CueReviewTests(unittest.TestCase):
    def test_swapped_language_is_warning_not_a_loanword_rejection(self):
        self.assertTrue(
            content_review.language_direction_warning(
                "Could I have a coffee please?", "Posso avere un caffè?"
            )
        )
        self.assertFalse(content_review.language_direction_warning("il computer", "the computer"))

    def test_cue_override_requires_current_version_and_stops_applying_after_content_change(self):
        c = database(self)
        lexeme(c)
        fields = desired(c)
        digest = quality.note_hash(fields)
        c.execute(
            "INSERT INTO cue_overrides VALUES(?,?,?,?,?)",
            (fields["Key"], digest, "dwelling", "home", "Reviewed distinction"),
        )
        changed = dict(fields)
        content_review.apply_overrides(c, fields["Key"], changed)
        self.assertEqual(changed["Hint"], "dwelling")
        self.assertIn("Also accepted: home", changed["Note"])
        changed = {**fields, "English": "building"}
        content_review.apply_overrides(c, fields["Key"], changed)
        self.assertEqual(changed["Hint"], "")


class PipelineTests(unittest.TestCase):
    def run_fixture(self, *, late_approval=False, rejected=False):
        c = database(self)
        lexeme(c, status="needs_review" if late_approval else "ready")
        overrides.save_senses(c, "x", [{"prompt": "house"}], "human")
        c.commit()
        seen = []
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            root = Path(folder)
            db_path = root / "database.sqlite"
            with closing(sqlite3.connect(db_path)) as target:
                c.backup(target)
            connect = db.connect
            stack.enter_context(patch.object(db, "connect", side_effect=lambda: connect(db_path)))
            stack.enter_context(patch.multiple(paths, PROJECT_ROOT=root, DB_PATH=db_path))
            stack.enter_context(patch.object(review, "REVIEW_FILE", root / "review.csv"))
            stack.enter_context(patch("flashcards.backup.snapshot"))
            stack.enter_context(patch("flashcards.backup.anki_snapshot", return_value=None))
            stack.enter_context(patch.object(lists, "load", return_value=[]))
            stack.enter_context(patch.object(lists, "validate", return_value=[]))
            stack.enter_context(patch.object(kaikki, "available", return_value=True))
            for name in ("import_human_edits", "sync_if_changed", "export_jsonl"):
                stack.enter_context(patch("flashcards.lexicon." + name, return_value=None))
            stack.enter_context(patch.object(queue, "plan", return_value={}))
            stack.enter_context(patch.object(notes, "image_exists", return_value=True))
            stack.enter_context(patch.object(notes, "_img", return_value=""))
            stack.enter_context(
                patch(
                    "flashcards.anki.invoke",
                    side_effect=lambda a, **k: "Test" if a == "getActiveProfile" else 6,
                )
            )
            stack.enter_context(patch.object(movie, "headlines", return_value=[]))

            def sync(conn, **kw):
                seen.append(conn.execute("SELECT ready FROM v4_notes WHERE key='vocab:x:0'").fetchone()[0])
                return reconcile.Plan(rejected=["vocab:x:0"] if rejected and len(seen) == 1 else [])

            stack.enter_context(patch.object(reconcile, "run", side_effect=sync))
            if late_approval:
                with closing(connect(db_path)) as target:
                    review.write_review_file(target)

            def compress(**kw):
                if late_approval:
                    review.REVIEW_FILE.write_text(
                        review.REVIEW_FILE.read_text().replace("\n,casa,", "\nyes,casa,")
                    )
                return {}

            stack.enter_context(patch("flashcards.commands.media.compress", side_effect=compress))
            args = SimpleNamespace(
                no_sync=False,
                keep_old=False,
                no_ai=True,
                images=0,
                audio=0,
                audio_workers=1,
                batches=20,
                once=False,
            )
            code = pipeline.run(args)
            with closing(connect(db_path)) as target:
                ready = target.execute("SELECT ready FROM v4_notes WHERE key='vocab:x:0'").fetchone()[0]
            return code, seen, ready

    def test_late_approval_reaches_final_sync_in_same_run(self):
        code, seen, ready = self.run_fixture(late_approval=True)
        self.assertEqual((code, seen, ready), (0, [0, 1], 1))

    def test_partial_result_and_external_changes_are_checked_at_final_sync(self):
        self.assertEqual(self.run_fixture(rejected=True)[:2], (0, [1, 1]))
        self.assertEqual(self.run_fixture()[:2], (0, [1, 1]))


class HorizonIntegrationTests(unittest.TestCase):
    def test_generation_and_publication_use_same_budget_and_verb_pairs(self):
        c = database(self)
        lexeme(
            c,
            "v",
            pos="verb",
            lemma="parlare",
            forms=json.dumps({"forms": {"presente": {"io": "parlo", "tu": "parli"}}}),
        )
        overrides.save_senses(c, "v", [{"prompt": "to speak"}], "human")
        c.commit()
        with (
            patch.object(planning, "horizon_budget", return_value=3),
            patch.object(notes, "full_form_verbs", return_value={"v"}),
            patch.object(notes, "image_exists", return_value=True),
            patch.object(queue, "image_exists", return_value=True),
        ):
            expected = planning.card_plan(c)
            queue.plan(c)
            payload = json.loads(
                c.execute("SELECT payload FROM ai_jobs WHERE kind='verb_prompts'").fetchone()[0]
            )
            requested = {
                f"form:v:{tense}:{person}"
                for tense, persons in payload["forms"].items()
                for person in persons
            }
            self.assertEqual(requested, {key for key in expected["keys"] if key.startswith("form:")})
            self.assertEqual(expected["new_directions"], 3)
            notes.build(c)
            inside = {
                r[0]
                for r in c.execute(
                    "SELECT key FROM v4_notes WHERE coalesce(blocked_by,'') NOT LIKE '%outside study horizon%'"
                )
            }
            self.assertEqual(inside, expected["keys"])
