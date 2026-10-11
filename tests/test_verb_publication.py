import json
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flashcards import content_review, lists, notes, overrides, planning, queue, reconcile
from .support import database, desired, lexeme


class VerbPublicationTests(unittest.TestCase):
    def fixture(self, *, lemma="parlare"):
        c = database(self)
        people = ["io", "tu", "lui_lei", "noi", "voi", "loro"]
        forms = {
            "presente": dict(
                zip(people, ["parlo", "parli", "parla", "parliamo", "parlate", "parlano"], strict=True)
            ),
            "imperfetto": dict(
                zip(
                    people,
                    ["parlavo", "parlavi", "parlava", "parlavamo", "parlavate", "parlavano"],
                    strict=True,
                )
            ),
            "futuro_semplice": dict(
                zip(
                    people,
                    ["parlerò", "parlerai", "parlerà", "parleremo", "parlerete", "parleranno"],
                    strict=True,
                )
            ),
            "condizionale_presente": dict(
                zip(
                    people,
                    ["parlerei", "parleresti", "parlerebbe", "parleremmo", "parlereste", "parlerebbero"],
                    strict=True,
                )
            ),
            "imperativo": {"tu": "parla", "Lei": "parli", "noi": "parliamo", "voi": "parlate"},
        }
        lexeme(
            c,
            pos="verb",
            lemma=lemma,
            forms=json.dumps(
                {
                    "forms": forms,
                    "auxiliary": "avere",
                    "past_participle": "parlato",
                    "gerund": "parlando",
                }
            ),
        )
        overrides.save_senses(c, "x", [{"prompt": "to speak"}], "human")
        c.commit()
        stack = ExitStack()
        self.addCleanup(stack.close)
        for module in (notes, queue):
            stack.enter_context(patch.object(module, "full_form_verbs", return_value={"x"}))
            stack.enter_context(patch.object(module, "image_exists", return_value=True))
        return c

    def complete_verb(self, c, exclusions=()):
        queue.plan(c)
        job = c.execute("SELECT * FROM ai_jobs WHERE kind='verb_prompts'").fetchone()
        forms = json.loads(job["payload"])["forms"]
        response = {
            "verbs": [
                {
                    "id": "x",
                    "prompts": [
                        {
                            "tense": tense,
                            "person": person,
                            "english": "Speak!" if tense == "imperativo" else "I speak",
                        }
                        for tense, people in forms.items()
                        for person in people
                        if (tense, person) not in exclusions
                    ],
                    "exclusions": [
                        {
                            "tense": tense,
                            "person": person,
                            "reason": "Not appropriate for this meaning/construction",
                        }
                        for tense, person in exclusions
                    ],
                }
            ]
        }
        queue._run_text_batch(
            SimpleNamespace(run=lambda *a, **kw: response), "verb_prompts", [job], c, threading.Lock()
        )
        return job

    def test_requested_forms_are_learning_and_other_forms_are_dormant(self):
        c = self.fixture()
        self.complete_verb(c)
        notes.build(c)
        forms = [dict(r) for r in c.execute("SELECT * FROM v4_notes WHERE card_type='form'")]
        self.assertEqual(len(forms), 46)
        dormant = [r for r in forms if "::Dormant::" in r["deck"]]
        forms = [r for r in forms if "::Learning::" in r["deck"]]
        self.assertEqual(len(forms), 32)
        self.assertEqual(len(dormant), 14)
        self.assertEqual(len({r["deck"] for r in forms}), 7)
        self.assertEqual(len({r["deck"] for r in dormant}), 4)
        self.assertEqual(
            {r["key"].split(":")[2] for r in forms},
            {
                "presente",
                "passato_prossimo",
                "futuro_semplice",
                "imperativo",
                "imperfetto",
                "presente_progressivo",
            },
        )
        self.assertEqual({r["key"].split(":")[3] for r in forms if ":imperativo:" in r["key"]}, {"Lei", "tu"})
        formal = next(json.loads(r["fields_json"]) for r in forms if r["key"] == "form:x:imperativo:Lei")
        self.assertEqual(formal["Hint"], "formal")
        self.assertIn("Imperativo", formal["Labels"])
        self.assertEqual(c.execute("SELECT count(*) FROM form_prompts").fetchone()[0], 46)

    def test_publishing_hidden_tenses_later_reuses_finished_generation(self):
        c = self.fixture()
        self.complete_verb(c)
        before = [tuple(r) for r in c.execute("SELECT * FROM form_prompts ORDER BY tense,person")]
        config = lists.load_plan()
        config["conjugation"]["learning_tenses"] = config["conjugation"]["tenses"]
        config["conjugation"]["learning_imperative_persons"] = ["tu", "Lei", "noi", "voi"]
        with patch.object(planning, "load_plan", return_value=config):
            queue.plan(c)
            notes.build(c)
        job = c.execute("SELECT status,attempts FROM ai_jobs WHERE kind='verb_prompts'").fetchone()
        self.assertEqual(tuple(job), ("done", 1))
        self.assertEqual(c.execute("SELECT count(*) FROM v4_notes WHERE card_type='form'").fetchone()[0], 46)
        self.assertEqual(
            before, [tuple(r) for r in c.execute("SELECT * FROM form_prompts ORDER BY tense,person")]
        )

    def test_all_forms_can_be_dormant_without_stopping_generation(self):
        c = self.fixture()
        config = lists.load_plan()
        config["conjugation"]["learning_tenses"] = []
        with patch.object(planning, "load_plan", return_value=config):
            self.complete_verb(c)
            notes.build(c)
        self.assertEqual(c.execute("SELECT count(*) FROM form_prompts").fetchone()[0], 46)
        self.assertEqual(
            c.execute(
                "SELECT count(*) FROM v4_notes WHERE card_type='form' AND tags NOT LIKE '%fc::dormant%'"
            ).fetchone()[0],
            0,
        )

    def test_horizon_does_not_truncate_stored_verb_family(self):
        c = self.fixture()
        with patch.object(planning, "horizon_budget", return_value=3):
            self.complete_verb(c)
            plan = planning.card_plan(c)
            notes.build(c)
        self.assertEqual(plan["new_directions"], 3)
        self.assertEqual(c.execute("SELECT count(*) FROM form_prompts").fetchone()[0], 46)

    def test_ai_ignore_is_persisted_reported_and_not_retried(self):
        c = self.fixture()
        ignored = {("presente_progressivo", "io")}
        self.complete_verb(c, ignored)
        queue.plan(c)
        notes.build(c)
        self.assertEqual(
            c.execute("SELECT status FROM ai_jobs WHERE kind='verb_prompts'").fetchone()[0], "done"
        )
        self.assertIsNone(
            c.execute("SELECT key FROM v4_notes WHERE key='form:x:presente_progressivo:io'").fetchone()
        )
        row = next(r for r in content_review.grammar_holds(c) if r["key"] == "form:x:presente_progressivo:io")
        self.assertEqual(row["action"], "ignore")
        self.assertIn("Not appropriate", row["reason"])

    def test_potere_imperatives_cannot_be_published_even_if_previously_generated(self):
        c = self.fixture(lemma="potere")
        ignored = {("imperativo", person) for person in ("tu", "Lei", "noi", "voi")}
        self.complete_verb(c, ignored)
        queue.plan(c)
        notes.build(c)
        self.assertEqual(c.execute("SELECT count(*) FROM form_exclusions").fetchone()[0], 4)
        self.assertEqual(
            c.execute("SELECT count(*) FROM v4_notes WHERE key LIKE 'form:%:imperativo:%'").fetchone()[0], 0
        )
        self.assertEqual(
            c.execute("SELECT status FROM ai_jobs WHERE kind='verb_prompts'").fetchone()[0], "done"
        )

    def test_invalid_publication_settings_are_rejected(self):
        for body in (
            'learning_tenses = "presente"',
            'learning_tenses = ["invented"]',
            'tenses = ["presente"]\nlearning_tenses = ["imperfetto"]',
            'learning_imperative_persons = ["io"]',
            'learning_imperative_persons = ["tu", "tu"]',
            'deck = "Other"',
        ):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "plan.toml"
                path.write_text("[conjugation]\n" + body)
                with self.assertRaises(ValueError):
                    lists.load_plan(path)


class RetirementPolicyTests(unittest.TestCase):
    def plan(self, c, v4=(), legacy=(), targets=None):
        with (
            patch.object(reconcile, "owned_notes", return_value=list(v4)),
            patch.object(reconcile, "owned_legacy", return_value=list(legacy)),
            patch.object(reconcile, "legacy_index", return_value={}),
            patch.object(reconcile, "legacy_targets", return_value=lambda key: (targets or {}).get(key)),
        ):
            return reconcile.build_plan(c, anki_media=set())

    def studied(self, key, nid=10, fields=None):
        return reconcile.AnkiNote(
            nid,
            key,
            fields or {},
            ["fc::owned"],
            [nid * 10],
            {"Italian::Verbs::Presente"},
            templates={nid * 10: "Production"},
            cards_info={nid * 10: {"type": 2, "reps": 30, "queue": 2}},
        )

    def test_removed_tense_studied_v4_and_both_legacy_directions_are_removed(self):
        c = database(self)
        lexeme(c, pos="verb")
        key = "form:x:passato_remoto:io"
        old = self.studied(key)
        legacy = [
            self.studied("verb_form:e:conditional:io|" + d, i)
            for i, d in [(20, "en_to_it"), (30, "it_to_en")]
        ]
        # Even a stale materialized desired row cannot re-enable a hidden tense.
        desired(c, key=key)
        plan = self.plan(
            c,
            [old],
            legacy,
            {n.key: (key, "Production" if i == 0 else "Recognition") for i, n in enumerate(legacy)},
        )
        self.assertEqual(plan.retire_v4, [old])
        self.assertEqual(plan.retire_legacy, legacy)
        self.assertEqual(plan.adopt, [])
        self.assertEqual(plan.add, [])
        self.assertEqual(plan.update, [])

    def test_active_studied_form_moves_to_unified_deck_without_recreation(self):
        c = database(self)
        lexeme(c, pos="verb")
        fields = desired(c, key="form:x:presente:io")
        c.execute("UPDATE v4_notes SET deck='Italian::Verbs::Learning::Presente'")
        old = self.studied(fields["Key"], fields=fields)
        plan = self.plan(c, [old])
        self.assertEqual(plan.move, [([100], "Italian::Verbs::Learning::Presente")])
        self.assertEqual(plan.retire_v4, [])
        self.assertEqual(plan.add, [])

    def test_studied_direction_stays_disabled_in_desired_fields(self):
        c = database(self)
        lexeme(c)
        fields = desired(c)
        old = self.studied(fields["Key"], fields=fields)
        c.execute("UPDATE v4_notes SET fields_json=?", (json.dumps({**fields, "Production": ""}),))
        plan = self.plan(c, [old])
        self.assertEqual(plan.update[0][1]["fields"]["Production"], "")
        self.assertEqual(plan.suspend, [100])

    def test_studied_duplicate_is_retired_after_another_home_is_selected(self):
        c = database(self)
        lexeme(c)
        fields = desired(c)
        legacy = [self.studied("gloss:e|en_to_it", nid) for nid in (10, 20)]
        plan = self.plan(c, legacy=legacy, targets={legacy[0].key: (fields["Key"], "Production")})
        self.assertEqual(plan.adopted, {10})
        self.assertEqual(plan.retire_legacy, [legacy[1]])
        self.assertEqual(plan.retire_dependencies[20], (fields["Key"], "Production"))

    def test_deletion_cleans_published_state_but_keeps_generated_data_and_identity(self):
        c = database(self)
        lexeme(c, pos="verb")
        key = "form:x:passato_remoto:io"
        old = self.studied(key)
        c.execute("INSERT INTO identity(key,guid,kind,note_id) VALUES(?,'saved-guid','v4',10)", (key,))
        c.execute("INSERT INTO adoptions VALUES(?,'Production',10,'Test')", (key,))
        c.execute(
            "INSERT INTO card_knowledge(key,direction,card_id,state,interval,lapses,reviews,suspended) VALUES(?,'Production',100,'known',30,0,30,0)",
            (key,),
        )
        c.execute(
            "INSERT INTO form_prompts(lexeme_id,tense,person,english) VALUES('x','condizionale_presente','io','I would speak')"
        )
        c.commit()
        plan = self.plan(c, [old])
        calls = []

        def invoke(action, **kw):
            calls.append(action)
            if action == "notesInfo":
                return [{"noteId": 10, "fields": {"Key": {"value": key}}, "cards": [100]}]
            if action == "getDecks":
                return {"Italian::Verbs::Condizionale Presente": [100]}
            if action == "deleteNotes":
                return None
            raise AssertionError(action)

        with patch.object(reconcile, "invoke", side_effect=invoke):
            self.assertEqual(reconcile._retire(c, plan, {}, {}, True), 1)
        self.assertEqual(plan.removed_ids, {10})
        self.assertEqual(c.execute("SELECT count(*) FROM form_prompts").fetchone()[0], 1)
        self.assertEqual(
            tuple(c.execute("SELECT guid,note_id FROM identity").fetchone()), ("saved-guid", None)
        )
        self.assertEqual(c.execute("SELECT count(*) FROM adoptions").fetchone()[0], 0)
        self.assertEqual(c.execute("SELECT count(*) FROM card_knowledge").fetchone()[0], 0)
        self.assertEqual(planning.published_directions(c), set())
        self.assertNotIn("deleteMediaFile", calls)
        self.assertIsNone(reconcile._drift_guard(c, set()))

    def test_explicitly_removed_studied_list_note_is_not_protected(self):
        c = database(self)
        lexeme(c)
        old = self.studied("vocab:x:0")
        c.execute("INSERT INTO retirements(key,reason) VALUES(?,'list removed')", (old.key,))
        plan = self.plan(c, [old])
        self.assertEqual(plan.retire_v4, [old])

    def test_keep_old_still_disables_deletion(self):
        c = database(self)
        old = self.studied("form:x:condizionale_presente:io")
        plan = reconcile.Plan(retire_v4=[old], retire_dependencies={10: None})
        with patch.object(reconcile, "invoke") as invoke:
            self.assertEqual(reconcile._retire(c, plan, {}, {}, False), 0)
        invoke.assert_not_called()
