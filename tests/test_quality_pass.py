"""Readiness for a from-scratch regeneration with a stronger model."""

import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flashcards import notes, overrides, queue, reconcile, semantics, srt
from flashcards import lists
from .support import database, desired, lexeme

SIX = ["io", "tu", "lui_lei", "noi", "voi", "loro"]


def verb(c, persons):
    forms = {
        "presente": dict(zip(SIX, ["vedo", "vedi", "vede", "vediamo", "vedete", "vedono"], strict=True)),
        "imperativo": {"tu": "vedi", "Lei": "veda", "noi": "vediamo", "voi": "vedete"},
    }
    lexeme(c, pos="verb", lemma="vedere", forms=json.dumps({"forms": forms, "auxiliary": "avere"}))
    overrides.save_senses(c, "x", [{"prompt": "to see", "features": {"persons": persons}}], "claude:test")
    c.commit()


class PersonTests(unittest.TestCase):
    def test_six_persons_mean_any_subject_and_lei_follows_lui_lei(self):
        self.assertTrue(semantics.person_allowed(SIX, "Lei"))
        self.assertTrue(semantics.person_allowed([], "Lei"))
        self.assertTrue(semantics.person_allowed(["lui_lei", "loro"], "Lei"))
        self.assertFalse(semantics.person_allowed(["lui_lei", "loro"], "io"))
        self.assertFalse(semantics.person_allowed(["tu"], "Lei"))

    def test_vedere_with_six_persons_gets_every_imperative_and_extras_are_dropped(self):
        c = database(self)
        verb(c, SIX)
        lx = c.execute("SELECT * FROM lexemes").fetchone()
        with patch.object(queue, "verb_forms", side_effect=lambda lx, sense: {
            t: {p: (f, f) for p, f in ps.items()} for t, ps in json.loads(lx["forms_json"])["forms"].items()
        }):
            payload = queue._verb_payload(c, lx)
        self.assertEqual(set(payload["forms"]["imperativo"]), {"tu", "Lei", "noi", "voi"})
        item = {
            "id": "x",
            "prompts": [
                {"tense": t, "person": p, "english": "x"}
                for t, ps in payload["forms"].items()
                for p in ps
            ]
            + [{"tense": "imperativo", "person": "io", "english": "invented"}],
            "exclusions": [],
        }
        job = {"subject": "x"}
        queue._validate_text_result("verb_prompts", [payload], [job], {"verbs": [item]})
        self.assertNotIn(("imperativo", "io"), {(p["tense"], p["person"]) for p in item["prompts"]})

    def test_restricted_persons_are_not_asked_for(self):
        c = database(self)
        verb(c, ["lui_lei", "loro"])
        lx = c.execute("SELECT * FROM lexemes").fetchone()
        with patch.object(queue, "verb_forms", side_effect=lambda lx, sense: {
            "presente": {p: ("f", "f") for p in SIX}
        }):
            payload = queue._verb_payload(c, lx)
        self.assertEqual(set(payload["forms"]["presente"]), {"lui_lei", "loro"})


class StableKeyTests(unittest.TestCase):
    def plan(self, c, v4):
        with (
            patch.object(reconcile, "owned_notes", return_value=v4),
        ):
            return reconcile.build_plan(c, anki_media=set())

    def note(self, key, nid, fields=None):
        return reconcile.AnkiNote(nid, key, fields or {}, ["fc::owned"], [nid * 10], {"Italian::Test"})

    def test_older_keys_are_retired_once_their_stable_card_exists(self):
        c = database(self)
        lexeme(c)
        fields = desired(c)  # vocab:x:0, ready
        current = self.note("vocab:x:0", 1, fields)
        old_vocab, old_form = self.note("vocab:x:1", 2), self.note("form:x:presente:io:sense-1", 3)
        plan = self.plan(c, [current, old_vocab, old_form])
        self.assertEqual(plan.retire, [old_vocab])  # the form's stable key isn't wanted
        self.assertEqual(plan.retire_dependencies[2], ("vocab:x:0", "Recognition"))
        self.assertEqual(plan.keep_unmapped, 1)

    def test_nothing_is_retired_while_the_stable_card_is_waiting(self):
        c = database(self)
        lexeme(c)
        desired(c, ready=False)
        old = self.note("vocab:x:1", 2)
        plan = self.plan(c, [old])
        self.assertEqual((plan.retire, plan.keep_unmapped), ([], 1))


class IgnoreTests(unittest.TestCase):
    def answer(self, **extra):
        return {
            "id": "x",
            "senses": [
                {
                    "prompt": "T",
                    "hint": "",
                    "register": "",
                    "note": "",
                    "source_id": "s",
                    "features": {
                        "persons": [],
                        "auxiliary": "",
                        "construction": "",
                        "singular_gender": "",
                        "plural": "",
                        "plural_gender": "",
                        "english_plural": "",
                    },
                }
            ],
            "english_plural": "",
            "verified": True,
            "issues": [],
            "fact": {"has_fact": False, "kind": "usage", "text": "", "confidence": 0},
            **extra,
        }

    def test_ignored_word_has_no_cards_and_its_note_leaves_anki(self):
        c = database(self)
        lexeme(c, lemma="T")
        queue._apply_lexeme(c, self.answer(ignore=True, ignore_reason="A chess abbreviation."), "test")
        self.assertEqual(c.execute("SELECT ignored FROM lexemes").fetchone()[0], "A chess abbreviation.")
        c.commit()
        with patch.object(notes, "image_exists", return_value=True):
            notes.build(c)
        self.assertEqual(c.execute("SELECT count(*) FROM v4_notes").fetchone()[0], 0)
        old = reconcile.AnkiNote(1, "vocab:x:0", {}, ["fc::owned"], [10], {"Italian::Test"})
        plan = StableKeyTests.plan(self, c, [old])
        self.assertEqual(plan.retire, [old])
        self.assertIn("A chess abbreviation", plan.retirement_reasons[1])

    def test_an_ignored_word_may_come_back_without_card_text(self):
        c = database(self)
        lexeme(c, lemma="pete")
        answer = self.answer(ignore=True, ignore_reason="Not Italian.")
        answer["senses"] = []
        queue._validate_text_result("lexeme_enrich", [{"id": "x"}], [{"subject": "x"}], {"words": [answer]})
        queue._apply_lexeme(c, answer, "test")
        self.assertEqual(c.execute("SELECT ignored FROM lexemes").fetchone()[0], "Not Italian.")
        with self.assertRaisesRegex(ValueError, "Empty sense"):
            kept = {**self.answer(), "senses": []}
            queue._validate_text_result("lexeme_enrich", [{"id": "x"}], [{"subject": "x"}], {"words": [kept]})

    def test_kept_answer_clears_an_earlier_ignore_and_your_edit_always_keeps_it(self):
        c = database(self)
        lexeme(c)
        queue._apply_lexeme(c, self.answer(ignore=True, ignore_reason="x"), "test")
        queue._apply_lexeme(c, self.answer(ignore=False, ignore_reason=""), "test")
        self.assertIsNone(c.execute("SELECT ignored FROM lexemes").fetchone()[0])
        overrides.set_override(c, "x", "senses", [{"prompt": "house"}])
        queue._apply_lexeme(c, self.answer(ignore=True, ignore_reason="x"), "test")
        self.assertIsNone(c.execute("SELECT ignored FROM lexemes").fetchone()[0])


class RegenerationTests(unittest.TestCase):
    def setUp(self):
        self.c = database(self)
        lexeme(self.c)
        overrides.save_senses(self.c, "x", [{"prompt": "house"}], "claude:test")
        self.c.commit()
        self.patches = [
            patch.object(queue, "_lexeme_payload", side_effect=lambda conn, lx: {"id": lx["id"]}),
            patch.object(queue.kaikki, "version", return_value="v"),
            patch.object(queue, "image_exists", return_value=True),
            patch.object(notes, "image_exists", return_value=True),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def pending(self, kind):
        return self.c.execute(
            "SELECT count(*) FROM ai_jobs WHERE kind=? AND status='pending'", (kind,)
        ).fetchone()[0]

    def test_new_fingerprint_formula_reopens_nothing(self):
        # A database written by the previous version: finished jobs carry
        # fingerprints made with the old formula.
        queue.plan(self.c)
        self.c.execute("UPDATE ai_jobs SET status='done', fingerprint='made-by-the-old-formula'")
        self.c.execute("INSERT OR REPLACE INTO metadata VALUES('fingerprint_version','2')")
        self.c.commit()
        self.assertEqual(sum(queue.plan(self.c).values()), 0)
        self.assertEqual(self.pending("lexeme_enrich"), 0)
        recorded = self.c.execute("SELECT fingerprint FROM ai_jobs WHERE kind='lexeme_enrich'").fetchone()[0]
        self.assertNotEqual(recorded, "made-by-the-old-formula")
        self.assertEqual(sum(queue.plan(self.c).values()), 0)  # and it stays quiet afterwards

    def test_bump_regenerates_everything_while_current_cards_stay_published(self):
        queue.plan(self.c)
        self.c.execute("UPDATE ai_jobs SET status='done'")
        self.assertEqual(sum(queue.plan(self.c).values()), 0)  # stable: nothing reopens by itself
        bumped = replace(queue.settings, ai=replace(queue.settings.ai, task_version="bumped"))
        with patch.object(queue, "settings", bumped):
            queue.plan(self.c)
        self.assertEqual(self.pending("lexeme_enrich"), 1)
        self.assertEqual(self.c.execute("SELECT status FROM lexemes").fetchone()[0], "ready")
        notes.build(self.c)
        self.assertEqual(
            self.c.execute("SELECT ready FROM v4_notes WHERE key='vocab:x:0'").fetchone()[0], 1
        )


class PayloadTests(unittest.TestCase):
    def test_claude_sees_short_meaning_ids_and_answers_map_back(self):
        c = database(self)
        lexeme(c)
        evidence = {
            "candidates": [
                {"source_id": "dictionary:" + "a" * 32, "glosses": ["house"], "tags": []},
                {"source_id": "source:" + "b" * 32, "glosses": ["home"], "context": {"row": 3}},
                {"source_id": "source:" + "c" * 32, "glosses": ["home"], "context": {"row": 9}},
            ],
            "observations": [],
        }
        with (
            patch.object(queue.semantics, "evidence", return_value=evidence),
            patch.object(queue, "_kaikki_entry", return_value=None),
        ):
            payload = queue._lexeme_payload(c, c.execute("SELECT * FROM lexemes").fetchone())
        seen = queue._model_view(payload)
        self.assertEqual(
            seen["meaning_evidence"]["candidates"],
            [{"source_id": "m1", "glosses": ["house"]}, {"source_id": "m2", "glosses": ["home"]}],
        )
        for gone in ("_meaning_ids", "draft_fact", "existing_senses", "dictionary_senses"):
            self.assertNotIn(gone, seen)
        c.execute(
            "INSERT INTO ai_jobs(kind,subject,payload) VALUES('lexeme_enrich','x',?)", (json.dumps(payload),)
        )
        c.commit()
        job = c.execute("SELECT * FROM ai_jobs").fetchone()
        answer = IgnoreTests.answer(self)
        answer["senses"][0].update(prompt="home", source_id="m2")
        queue._run_text_batch(
            SimpleNamespace(run=lambda *a, **kw: {"words": [answer]}), "lexeme_enrich", [job], c, threading.Lock()
        )
        self.assertEqual(c.execute("SELECT source_id FROM senses").fetchone()[0], "source:" + "b" * 32)


class RetryTests(unittest.TestCase):
    def test_a_fresh_run_retries_timeouts_but_not_invalid_answers(self):
        c = database(self)
        for subject, error in (("a", queue.INVALID + "Empty sense response"), ("b", "no answer within 600s")):
            c.execute(
                "INSERT INTO ai_jobs(kind,subject,status,attempts,error) VALUES('lexeme_enrich',?,'failed',3,?)",
                (subject, error),
            )
        self.assertEqual(queue.retry_failed(c, include_deferred=True, transient_only=True), 1)
        status = dict(c.execute("SELECT subject, status FROM ai_jobs"))
        self.assertEqual(status, {"a": "failed", "b": "pending"})


class FilmNameTests(unittest.TestCase):
    def test_line_and_sentence_starts_are_not_names(self):
        srt_text = (
            "1\n00:00:01,000 --> 00:00:02,000\nBruno è qui.\nAspetta Bruno. Ecco\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\nvieni qui\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "subs.srt"
            path.write_text(srt_text, encoding="utf-8")
            with patch.object(srt, "lemmatise", side_effect=lambda t: (t, "verb")):
                _cues, tokens = srt.analyze(path)
        status = {t.text: t.status for t in tokens}
        self.assertEqual(status["Aspetta"], "resolved")
        self.assertEqual(status["Ecco"], "resolved")
        self.assertEqual(
            [t.status for t in tokens if t.text == "Bruno"], ["excluded_name", "excluded_name"]
        )


if __name__ == "__main__":
    unittest.main()


class CardContentTests(unittest.TestCase):
    def setUp(self):
        self.c = database(self)
        lexeme(self.c, lemma="penna")
        self.c.execute("UPDATE lexemes SET in_kaikki=1")
        image = patch.object(notes, "image_exists", return_value=True)
        image.start()
        self.addCleanup(image.stop)

    def vocab(self):
        return {
            r["key"]: json.loads(r["fields_json"])
            for r in self.c.execute("SELECT key, fields_json FROM v4_notes WHERE card_type='vocab'")
        }

    def test_one_card_per_word_with_only_its_main_meaning(self):
        answer = IgnoreTests.answer(self)
        answer["senses"] = [
            {**answer["senses"][0], "prompt": "pen", "source_id": "a"},
            {**answer["senses"][0], "prompt": "penne pasta", "source_id": "b"},
        ]
        queue._apply_lexeme(self.c, answer, "test")
        self.c.commit()
        self.assertEqual(self.c.execute("SELECT count(*) FROM senses WHERE active=1").fetchone()[0], 1)
        # A second active meaning left in the database still gets no card of its own.
        self.c.execute("INSERT INTO senses(lexeme_id,idx,prompt,active,source_id) VALUES('x',1,'penne pasta',1,'b')")
        self.assertEqual(len(self.vocab_after_build()), 1)
        fields = self.vocab_after_build()["vocab:x:0"]
        self.assertEqual(fields["English"], "pen")

    def vocab_after_build(self):
        self.c.commit()
        notes.build(self.c)
        return self.vocab()

    def test_cards_show_no_source_lists_or_dictionary_link(self):
        overrides.save_senses(self.c, "x", [{"prompt": "pen"}], "claude:test")
        fields = self.vocab_after_build()["vocab:x:0"]
        self.assertEqual(fields["Source"], "")
        for template in notes.TEMPLATES:
            self.assertNotIn("Source", template["Back"])

    def test_facts_are_checked_and_dropped_or_fixed_on_the_card(self):
        overrides.save_senses(self.c, "x", [{"prompt": "pen"}], "claude:test")
        self.c.execute(
            "INSERT INTO word_facts(word,has_fact,kind,fact,confidence,model) VALUES('penna',1,'culture',"
            "'Quills were cut from feathers. English has borrowed the word penna.',0.9,'claude:test')"
        )
        self.c.commit()
        with (
            patch.object(queue, "_lexeme_payload", side_effect=lambda conn, lx: {"id": lx["id"]}),
            patch.object(queue.kaikki, "version", return_value="v"),
            patch.object(queue, "image_exists", return_value=True),
        ):
            queue.plan(self.c)
            job = self.c.execute("SELECT * FROM ai_jobs WHERE kind='fact_check'").fetchone()
            self.assertEqual(job["status"], "pending")
            for verdict, text, shown in (
                ("fix", "Quills were cut from feathers.", "Quills were cut from feathers."),
                ("drop", "", ""),
            ):
                answer = {"facts": [{"id": "penna", "verdict": verdict, "text": text, "reason": "test"}]}
                queue._run_text_batch(
                    SimpleNamespace(run=lambda *a, answer=answer, **kw: answer),
                    "fact_check",
                    [job],
                    self.c,
                    threading.Lock(),
                )
                self.assertEqual(self.vocab_after_build()["vocab:x:0"]["Fact"], shown)
            # A newly written fact is unchecked again and gets its own check.
            self.c.execute(
                "INSERT OR REPLACE INTO word_facts(word,has_fact,kind,fact,confidence,model) "
                "VALUES('penna',1,'etymology','From Latin penna, feather.',0.9,'claude:test')"
            )
            self.c.commit()
            queue.plan(self.c)
        self.assertEqual(
            self.c.execute("SELECT status FROM ai_jobs WHERE kind='fact_check'").fetchone()[0], "pending"
        )
        self.assertEqual(self.vocab_after_build()["vocab:x:0"]["Fact"], "From Latin penna, feather.")


class EveryListTests(unittest.TestCase):
    """A word has a card in the deck of every list it is in (no deduplication)."""

    def setUp(self):
        self.c = database(self)
        lexeme(self.c, lemma="penna")  # in list 'test' (Italian::Test)
        self.c.execute(
            "INSERT INTO lists(id,kind,title,deck,settings) VALUES('film','movie','Film','Italian::Film',"
            "'{\"recognition_only\": true}')"
        )
        self.c.execute("INSERT INTO list_items(list_id,lexeme_id,rank,raw) VALUES('film','x',7,'penna')")
        overrides.save_senses(self.c, "x", [{"prompt": "pen"}], "claude:test")
        self.c.commit()
        for target in (notes, queue):
            p = patch.object(target, "image_exists", return_value=True)
            p.start()
            self.addCleanup(p.stop)
        plan = patch.object(
            notes, "load_plan", return_value=lists.Plan.from_dict({"priority": [{"list": "film"}, {"list": "test"}]})
        )
        plan.start()
        self.addCleanup(plan.stop)

    def cards(self):
        self.c.commit()
        notes.build(self.c)
        return {
            r["key"]: (r["deck"], json.loads(r["fields_json"]))
            for r in self.c.execute("SELECT * FROM v4_notes WHERE card_type='vocab'")
        }

    def test_one_card_per_list_and_the_existing_note_keeps_its_place(self):
        cards = self.cards()
        self.assertEqual(set(cards), {"vocab:x:0", "vocab:x:0@test"})
        # The plain key stays with the deck the word started in (the film: first priority)…
        self.assertEqual(cards["vocab:x:0"][0], "Italian::Film")
        self.assertEqual(cards["vocab:x:0@test"][0], "Italian::Test")
        self.assertEqual(self.c.execute("SELECT key_list FROM lexemes").fetchone()[0], "film")
        # …and keeps both directions you may already study; the other copy follows its list.
        self.assertEqual(cards["vocab:x:0"][1]["Production"], "1")
        self.assertEqual(cards["vocab:x:0@test"][1]["Production"], "1")
        # A film-only copy is recognition-only.
        self.c.execute("UPDATE lexemes SET key_list='test'")
        self.assertEqual(self.cards()["vocab:x:0@film"][1]["Production"], "")

    def test_a_copy_leaves_anki_when_the_word_leaves_its_list(self):
        self.cards()
        self.c.execute("DELETE FROM list_items WHERE list_id='test'")
        self.cards()
        copy = reconcile.AnkiNote(2, "vocab:x:0@test", {}, ["fc::owned"], [20], {"Italian::Test"})
        plan = StableKeyTests.plan(self, self.c, [copy])
        self.assertEqual(plan.retire, [copy])
        self.assertEqual(plan.retirement_reasons[2], "No longer in list test")

    def test_copies_are_never_mistaken_for_older_keys(self):
        self.assertIsNone(reconcile._current_key("vocab:x:0@test"))


class AudioLimitTests(unittest.TestCase):
    def client(self, *outcomes):
        calls = iter(outcomes)

        def convert(**kw):
            outcome = next(calls)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        return SimpleNamespace(text_to_speech=SimpleNamespace(convert=convert))

    def test_busy_elevenlabs_is_waited_out_and_no_credit_stops_the_rest(self):
        from elevenlabs.core.api_error import ApiError
        from flashcards.commands import media

        busy = ApiError(status_code=429, body={"detail": {"status": "too_many_concurrent_requests"}})
        # Sent as 429, it still must not be waited for.
        broke = ApiError(status_code=429, body={"detail": {"status": "quota_exceeded", "message": "No credits"}})
        out = threading.Event()
        with tempfile.TemporaryDirectory() as tmp, patch.object(media.STOP, "wait", return_value=False):
            dest = Path(tmp) / "a.mp3"
            media._speak(self.client(busy, busy, b"mp3"), "ciao", dest, out)
            self.assertEqual(dest.read_bytes(), b"mp3")
            with self.assertRaises(media.OutOfCredit):
                media._speak(self.client(broke), "casa", Path(tmp) / "b.mp3", out)
            self.assertTrue(out.is_set())
            with self.assertRaises(media.OutOfCredit):  # later recordings aren't even asked for
                media._speak(self.client(), "cane", Path(tmp) / "c.mp3", out)


class ProgressTests(unittest.TestCase):
    def test_long_media_work_reports_progress_about_once_a_minute(self):
        from flashcards import report

        clock, lines = [0.0], []
        with (
            patch.object(report.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(report, "line", side_effect=lines.append),
        ):
            progress = report.Progress("audio", 300)
            for i in range(300):
                clock[0] += 1.5
                progress.tick(i != 5)
        self.assertEqual(lines[0], "audio     300 to make …")
        self.assertEqual(len(lines), 1 + 7)  # 450 s of work, a line each minute
        self.assertIn("40 / 300 · 1 failed · 40/min · ~6m 30s left", lines[1])


class ParallelImageTests(unittest.TestCase):
    def test_three_images_at_once_but_never_two_for_the_same_file(self):
        c = database(self)
        for i, key in enumerate(["a", "a", "b", "c"]):  # x0 and x1 share one image file
            lexeme(c, lid=f"x{i}", lemma=f"w{i}")
            queue._enqueue(c, "image", f"x{i}", i, {"image_key": key, "subject": f"x{i}"})
        c.commit()
        together = threading.Barrier(3, timeout=5)  # breaks unless 3 run at the same time
        made = []

        def fake_codex(job, conn, lock):
            made.append(json.loads(job["payload"])["image_key"])
            if len(made) <= 3:
                together.wait()
            return True

        with (
            patch.object(queue, "_run_image", side_effect=fake_codex),
            patch.object(queue.codex, "logged_in", return_value=True),
            patch.object(queue, "plan"),
        ):
            result = queue.drain(c, kinds=["image"])
        self.assertEqual(result["image"], {"done": 4, "failed": 0})
        self.assertEqual(sorted(made[:3]), ["a", "b", "c"])  # the second "a" waits for the next round
        self.assertEqual(made[3], "a")


class SiblingTests(unittest.TestCase):
    def test_italian_decks_get_their_own_preset_with_siblings_buried(self):
        def config(cid, name):
            return {"id": cid, "name": name, "new": {"bury": False, "perDay": 10}, "rev": {"bury": False},
                    "buryInterdayLearning": False}

        default = config(1, "Default")
        saved, decks = {}, {"Default": 1, "ZCam's Decks - Old": 1, "Italian": 1, "Italian::CILS::A1": 1}
        presets = {1: default}

        def anki(action, **kw):
            if action == "deckNames":
                return list(decks)
            if action == "getDeckConfig":
                return json.loads(json.dumps(presets[decks[kw["deck"]]]))
            if action == "cloneDeckConfigId":
                presets[2] = {**json.loads(json.dumps(presets[int(kw["cloneFrom"])])), "id": 2, "name": kw["name"]}
                return 2
            if action == "setDeckConfigId":
                decks.update(dict.fromkeys(kw["decks"], kw["configId"]))
                return True
            if action == "saveDeckConfig":
                presets[kw["config"]["id"]] = saved[kw["config"]["id"]] = kw["config"]
                return True
            raise AssertionError(action)

        with patch.object(reconcile, "invoke", side_effect=anki):
            reconcile._spaced.clear()
            self.assertEqual(reconcile.space_siblings("User 1"), 3)  # 2 decks moved + 1 preset saved
            reconcile._spaced.clear()
            self.assertEqual(reconcile.space_siblings("User 1"), 0)  # nothing left to change
        self.assertEqual((decks["Italian"], decks["Italian::CILS::A1"], decks["ZCam's Decks - Old"]), (2, 2, 1))
        self.assertEqual(list(saved), [2])  # Default (shared with ZCam's Decks - Old) is never saved
        ours = presets[2]
        self.assertTrue(ours["new"]["bury"] and ours["rev"]["bury"] and ours["buryInterdayLearning"])
        self.assertEqual((ours["name"], ours["new"]["perDay"]), ("Italian flashcards", 10))
        self.assertFalse(presets[1]["new"]["bury"])


class CognateTests(unittest.TestCase):
    def test_one_production_card_per_word_and_only_real_suffix_matches(self):
        c = database(self)
        for lid, lemma, prompt in (
            ("n", "nazione", "nation"),
            ("s", "stazione", "station"),
            ("x", "sale", "salt"),  # close, but "salt" doesn't end in -al
        ):
            lexeme(c, lid=lid, lemma=lemma)
            overrides.save_senses(c, lid, [{"prompt": prompt}], "claude:test")
        c.commit()
        with patch.object(notes, "image_exists", return_value=True):
            b = notes._Builder(c)
            b.cognate_notes()
        cards = {n["key"]: n["fields"] for n in b.notes}
        self.assertEqual(set(cards), {"cognate:n", "cognate:s"})
        nation = cards["cognate:n"]
        self.assertEqual((nation["English"], nation["Hint"]), ("nation", ""))  # nothing on the front
        self.assertIn("-zione = -tion", nation["Details"])  # the rule is on the back
        self.assertEqual((nation["Production"], nation["Recognition"]), ("1", ""))
        self.assertIn("like la stazione", nation["Details"])

    def test_old_per_rule_cards_leave_anki(self):
        c = database(self)
        lexeme(c)
        old = reconcile.AnkiNote(9, "cognate:" + "f" * 32, {}, ["fc::owned"], [90], {"Italian::Cognates"})
        plan = StableKeyTests.plan(self, c, [old])
        self.assertEqual(plan.retire, [old])
        self.assertEqual(plan.retirement_reasons[9], "No longer a cognate card")


class ShuffleTests(unittest.TestCase):
    def test_light_shuffle_within_each_deck_keeps_its_slots_and_is_stable(self):
        numbers = [{"key": f"vocab:n{i}:0", "deck": "Numbers"} for i in range(100)]
        film = [{"key": f"vocab:f{i}:0", "deck": "Film"} for i in range(100)]
        ordered = [n for pair in zip(film, numbers, strict=True) for n in pair]  # decks interleave
        shuffled = notes.shuffle_within_decks(ordered, 20)
        self.assertEqual([n["deck"] for n in shuffled], [n["deck"] for n in ordered])  # same slots
        nums = [int(n["key"][7:].split(":")[0]) for n in shuffled if n["deck"] == "Numbers"]
        self.assertNotEqual(nums, sorted(nums))  # not 0, 1, 2 …
        self.assertTrue(all(abs(pos - num) <= 20 for pos, num in enumerate(nums)))  # not too far
        self.assertEqual(shuffled, notes.shuffle_within_decks(ordered, 20))  # same every run
        self.assertEqual(notes.shuffle_within_decks(ordered, 0), ordered)


class NounNumberTests(unittest.TestCase):
    def setUp(self):
        self.c = database(self)
        self.c.execute(
            "INSERT INTO lexemes(id,lemma,pos,display,gender,plural,english_plural,status,provenance) "
            "VALUES('o','occhiale','noun','l''occhiale','masculine','occhiali','glasses','ready','{}')"
        )
        self.c.execute(
            "INSERT OR IGNORE INTO lists(id,kind,title,deck,settings) VALUES('test','csv','Test','Italian::Test','{}')"
        )
        self.c.execute("INSERT INTO list_items(list_id,lexeme_id,rank,raw) VALUES('test','o',1,'gli occhiali')")
        overrides.save_senses(self.c, "o", [{"prompt": "glasses"}], "claude:test")
        self.c.commit()
        for target in (notes, queue):
            p = patch.object(target, "image_exists", return_value=True)
            p.start()
            self.addCleanup(p.stop)

    def answer(self, number, english=""):
        with (
            patch.object(queue, "_lexeme_payload", side_effect=lambda conn, lx: {"id": lx["id"]}),
            patch.object(queue.kaikki, "version", return_value="v"),
        ):
            queue.plan(self.c)
        job = self.c.execute("SELECT * FROM ai_jobs WHERE kind='noun_number'").fetchone()
        self.assertEqual(json.loads(job["payload"])["plural"], "gli occhiali")
        result = {"nouns": [{"id": "o", "number": number, "english": english}]}
        queue._run_text_batch(
            SimpleNamespace(run=lambda *a, **kw: result), "noun_number", [job], self.c, threading.Lock()
        )
        self.c.commit()
        notes.build(self.c)
        return {r["key"]: json.loads(r["fields_json"]) for r in self.c.execute("SELECT * FROM v4_notes")}

    def test_a_noun_used_in_the_plural_is_taught_in_the_plural(self):
        cards = self.answer("plural", "glasses")
        card = cards["vocab:o:0"]
        self.assertEqual((card["Italian"], card["English"], card["Details"]), ("gli occhiali", "glasses", "masculine plural"))
        drills = [k for k in cards if k.startswith("nphrase:")]
        self.assertTrue(drills and all(k.endswith(":plural") for k in drills))
        # An older singular drill (un occhiale) leaves Anki.
        old = reconcile.AnkiNote(5, "nphrase:o:indefinite-None:singular", {}, ["fc::owned"], [50], {"Italian::Noun Phrases"})
        plan = StableKeyTests.plan(self, self.c, [old])
        self.assertEqual(plan.retirement_reasons[5], "Singular not used for this noun")

    def test_a_singular_only_noun_shows_no_plural(self):
        card = self.answer("singular")["vocab:o:0"]
        self.assertEqual((card["Italian"], card["Details"]), ("l&#x27;occhiale", "masculine"))


class CueCheckTests(unittest.TestCase):
    def test_prompts_that_give_the_italian_away_are_rewritten_on_the_card(self):
        from flashcards.italian import gives_away

        self.assertTrue(gives_away("priority (precedence; right of way)", "precedenza"))
        self.assertFalse(gives_away("nation", "nazione"))  # the only natural English stays
        c = database(self)
        lexeme(c, lemma="precedenza")
        # The give-away is in the cue-group hint, as on the real card.
        overrides.save_senses(c, "x", [{"prompt": "priority", "hint": "precedence; right of way"}], "claude:test")
        c.commit()
        for target in (notes, queue):
            p = patch.object(target, "image_exists", return_value=True)
            p.start()
            self.addCleanup(p.stop)
        with (
            patch.object(queue, "_lexeme_payload", side_effect=lambda conn, lx: {"id": lx["id"]}),
            patch.object(queue.kaikki, "version", return_value="v"),
        ):
            queue.plan(c)
        job = c.execute("SELECT * FROM ai_jobs WHERE kind='cue_check'").fetchone()
        answer = {"cues": [{"id": "x", "english": "priority", "hint": "right of way"}]}
        queue._run_text_batch(SimpleNamespace(run=lambda *a, **kw: answer), "cue_check", [job], c, threading.Lock())
        c.commit()
        notes.build(c)
        card = json.loads(c.execute("SELECT fields_json FROM v4_notes WHERE key='vocab:x:0'").fetchone()[0])
        self.assertEqual((card["English"], card["Hint"]), ("priority", "right of way"))
        # A new prompt from a later answer is shown as is until it has been checked.
        overrides.save_senses(c, "x", [{"prompt": "right of way"}], "claude:test")
        c.commit()
        notes.build(c)
        card = json.loads(c.execute("SELECT fields_json FROM v4_notes WHERE key='vocab:x:0'").fetchone()[0])
        self.assertEqual(card["English"], "right of way")
