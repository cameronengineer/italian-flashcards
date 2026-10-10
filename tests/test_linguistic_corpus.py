import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from flashcards import lists, italian, notes, semantics, overrides, reconcile
from flashcards.domain import ListDef, Item
from .support import database, lexeme, desired

CASES = json.loads(Path(__file__).with_name("linguistic_cases.json").read_text())


class LinguisticCorpusTests(unittest.TestCase):
    def test_source_readers_preserve_expected_language_pos_and_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            for case in CASES["readers"]:
                with self.subTest(kind=case["kind"]):
                    path = Path(folder) / "fixture.csv"
                    path.write_text(case["body"])
                    rows = lists.read(ListDef("test", case["kind"], "Test", "Italian::Test", path))
                    self.assertEqual([(r.lemma, r.pos) for r in rows], [(case["lemma"], case["pos"])])
            # Subtitle token resolution is tested separately; the movie reader
            # must preserve its contextual evidence and count, not reclassify it.
            expected = Item(
                "succede", "succedere", "verb", context={"count": 2, "example": "Che cosa succede?"}
            )
            with patch("flashcards.srt.word_items", return_value=[expected]):
                item = lists.read(
                    ListDef("film", "movie", "Film", "Italian::Film", Path(folder) / "film.srt")
                )[0]
            self.assertEqual(item.context["count"], 2)
            self.assertEqual(item.lemma, "succedere")

    def test_number_and_plural_forms(self):
        for value, word in CASES["numbers"]:
            self.assertEqual(italian.number_words(value), word)
        for word, gender, article in CASES["plural_articles"]:
            self.assertEqual(italian.definite_article(word, gender, True), article)

    def test_invalid_published_event_drill_is_held_without_deleting_history(self):
        c = database(self)
        lexeme(
            c,
            pos="verb",
            lemma="succedere",
            forms=json.dumps({"forms": {"presente": {"io": "succedo", "lui_lei": "succede"}}}),
        )
        overrides.save_senses(c, "x", [{"prompt": "to happen"}], "human")
        c.commit()
        sense = semantics.primary(c, "x")
        c.execute(
            "INSERT INTO form_prompts(lexeme_id,tense,person,english,source_id) VALUES('x','presente','io','I happen',?)",
            (sense["source_id"],),
        )
        c.commit()
        fields = desired(c, key="form:x:presente:io")
        old = reconcile.AnkiNote(
            10,
            fields["Key"],
            fields,
            ["fc::owned"],
            [100],
            {"Italian::Test"},
            cards_info={100: {"type": 2, "reps": 20}},
        )
        with patch.object(notes, "full_form_verbs", return_value={"x"}):
            generated = notes.candidates(c).notes
        self.assertNotIn("form:x:presente:io", {n["key"] for n in generated})
        with (
            patch.object(reconcile, "owned_notes", return_value=[old]),
            patch.object(reconcile, "owned_legacy", return_value=[]),
            patch.object(reconcile, "legacy_index", return_value={}),
        ):
            planned = reconcile.build_plan(c, anki_media=set())
        self.assertIn(100, planned.suspend)
        self.assertEqual(planned.retire_v4, [])

    def test_interrupted_adoption_does_not_disable_existing_v4_direction(self):
        c = database(self)
        lexeme(c)
        fields = desired(c)
        existing = reconcile.AnkiNote(11, fields["Key"], fields, ["fc::owned"], [110], {"Italian::Test"})
        legacy = reconcile.AnkiNote(20, "gloss:x|en_to_it", {}, [], [200], {"Italian::Test"})
        plan = reconcile.Plan(
            adopt=[(legacy, {**fields, "_deck": "Italian::Test", "_tags": ["fc::owned"]}, "Production")],
            update=[(existing, {"fields": {**fields, "Production": ""}, "tags": ["fc::owned"]})],
        )
        calls = []

        def invoke(action, **kw):
            if action == "getActiveProfile":
                return "Test"
            if action == "multi":
                for operation in kw["actions"]:
                    if operation["action"] == "updateNoteFields":
                        calls.append(operation["params"]["note"]["id"])
                return [{"result": None, "error": None} for _ in kw["actions"]]
            if action == "notesInfo":
                return []  # read-back failure
            return []

        with (
            patch.object(reconcile, "build_plan", return_value=plan),
            patch.object(reconcile, "ensure_model", return_value="ok"),
            patch.object(reconcile, "invoke", side_effect=invoke),
        ):
            with self.assertRaisesRegex(RuntimeError, "read-back"):
                reconcile.run(c)
        self.assertIn(20, calls)
        self.assertNotIn(11, calls)

    def test_materialized_examples_cover_every_card_type(self):
        c = database(self)
        lexeme(c, "home")
        overrides.save_senses(c, "home", [{"prompt": "house"}], "human")
        lexeme(c, "greeting", pos="phrase", lemma="Buongiorno!")
        overrides.save_senses(c, "greeting", [{"prompt": "Good morning!"}], "human")
        lexeme(
            c,
            "speak",
            pos="verb",
            lemma="parlare",
            forms=json.dumps({"forms": {"presente": {"io": "parlo"}}}),
        )
        overrides.save_senses(c, "speak", [{"prompt": "to speak"}], "human")
        source = semantics.primary(c, "speak")["source_id"]
        c.execute(
            "INSERT INTO form_prompts(lexeme_id,tense,person,english,source_id) VALUES('speak','presente','io','I speak / I am speaking',?)",
            (source,),
        )
        for word, english in [("azione", "action"), ("nazione", "nation"), ("stazione", "station")]:
            lexeme(c, word, lemma=word)
            overrides.save_senses(c, word, [{"prompt": english}], "human")
            c.execute("UPDATE lexemes SET cognate_rule='-zione = -tion' WHERE id=?", (word,))
        c.execute("INSERT INTO mistakes(id,italian,english) VALUES('m','Ho fame','I am hungry')")
        c.commit()
        with patch.object(notes, "full_form_verbs", return_value={"speak"}):
            rows = notes.candidates(c).notes
        self.assertEqual(
            {r["card_type"] for r in rows}, {"vocab", "phrase", "form", "nphrase", "cognate", "mistake"}
        )
        fields = {r["key"]: r["fields"] for r in rows}
        self.assertEqual(fields["vocab:home:0"]["Italian"], "la casa")
        self.assertEqual(fields["phrase:greeting:0"]["English"], "Good morning!")
        self.assertEqual(fields["form:speak:presente:io"]["English"], "I speak / I am speaking")
        self.assertEqual(fields["nphrase:home:poss-loro:plural"]["Italian"], "le loro case")
        self.assertEqual(fields["mistake:m"]["English"], "I am hungry")
        pattern = next(r["fields"] for r in rows if r["card_type"] == "cognate")
        self.assertIn("station", pattern["English"])
        self.assertIn("-zione", pattern["Italian"])
