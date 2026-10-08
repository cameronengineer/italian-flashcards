"""v4 notes: what Anki should contain, computed from the lexicon.

One notetype ("Italian Flashcard v4") with two card templates — Recognition
(Italian → English) and Production (English → Italian) — each switched on
by a field, so a note can be either or both. Every note carries its root's
image; a note is only *ready* (sent to Anki) when its content and image
exist.

Card types:
  vocab    one note per root sense — the word, gender/plural or forms, IPA,
           film example, usage note, fun fact
  phrase   sentences, stems and fixed expressions
  form     verb forms (model verbs, irregular verbs, the most frequent N)
  nphrase  rule-built noun phrases (article / preposition / possessive …)
  cognate  one note per cognate rule with its example words
  mistake  practice errors turned into cards

Notes are filed in the deck of the root's highest-priority list (plan.toml);
verb forms, noun phrases, cognates and mistakes have their own decks.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
from collections import defaultdict

from .cards import FACT_TITLES, labels, labels_html
from .facts import normalise as fact_key
from .italian import (
    articulated_phrase, conjugation_class, definite_article, demonstrative_phrase,
    indefinite_article, possessive_phrase, with_article,
)
from .lexicon import image_exists
from .lists import load_plan
from .queue import MODEL_VERBS, full_form_verbs, study_order, verb_forms
from .settings import settings
from .util import audio_filename, image_filename, md5_hex, print_banner
from .grammar import TENSE_DISPLAY
from .paths import AUDIO_DIR, AUDIO_DIR_COMPRESSED

MODEL_NAME = "Italian Flashcard v4"
FIELDS = [
    "Italian", "English", "Hint", "Labels", "Details", "Forms", "IPA", "Example",
    "Note", "Fact", "FactTitle", "Image", "Audio", "Recognition", "Production",
    "Source", "Key",
]
DECK_FORMS = "Italian::Verbs::{tense}"
DECK_NPHRASES = "Italian::Noun Phrases"
DECK_COGNATES = "Italian::Cognates"
DECK_MISTAKES = "Italian::Mistakes"

_TYPE_LABEL = {
    "noun": "noun", "verb": "verb", "adj": "adjective", "adv": "adverb", "pron": "pronoun",
    "conj": "conjunction", "prep": "preposition", "intj": "interjection", "article": "article",
    "num": "number", "letter": "letter", "phrase": "expression", "name": "name", "word": "word",
}
_GENDER = {"masculine": "masculine", "feminine": "feminine", "both": "masculine / feminine"}
_PHRASE_FAMILIES = [("indefinite", None), ("prep", "di"), ("prep", "a"), ("prep", "da"),
                    ("prep", "in"), ("prep", "su"), ("dem", "questo"), ("dem", "quello"),
                    ("poss", "mio"), ("poss", "tuo"), ("poss", "suo"), ("poss", "nostro"),
                    ("poss", "vostro"), ("poss", "loro")]
_EN_PREP = {"di": "of the", "a": "to the", "da": "from the", "in": "in the", "su": "on the"}
_EN_POSS = {"mio": "my", "tuo": "your", "suo": "his/her", "nostro": "our", "vostro": "your (pl.)", "loro": "their"}


def _img(key: str | None) -> str:
    if not key:
        return ""
    jpg = image_filename(key, "jpg")
    return f'<img src="{jpg}">'


def _snd(text: str | None) -> str:
    """A sound tag only when the audio file exists (generated in study order)."""
    if not text:
        return ""
    name = audio_filename(text)
    if (AUDIO_DIR_COMPRESSED / name).exists() or (AUDIO_DIR / name).exists():
        return f"[sound:{name}]"
    return ""


def _spoken(text: str | None) -> str:
    """What text-to-speech should say: first variant only, no stems' '…'."""
    if not text:
        return ""
    t = text.split(" / ")[0]
    t = re.sub(r"(\w)/\w+", r"\1", t)        # andato/a → andato, il/la → il
    return t.replace("…", "").replace("...", "").strip()


def _esc(s: str | None) -> str:
    return html.escape(s or "")


def _first_alt(prompt: str) -> str:
    return prompt.split(",")[0].split(";")[0].split("(")[0].strip()


class _Builder:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.notes: list[dict] = []
        self.order = study_order(conn)
        self.lists = {r["id"]: r for r in conn.execute("SELECT * FROM lists")}
        self.list_settings = {lid: json.loads(r["settings"] or "{}") for lid, r in self.lists.items()}
        plan = load_plan()
        self.priority = [p["list"] for p in plan.get("priority", [])]
        self.membership: dict[str, list[str]] = defaultdict(list)
        self.context: dict[str, dict] = {}
        for r in conn.execute("SELECT list_id, lexeme_id, context FROM list_items"):
            self.membership[r["lexeme_id"]].append(r["list_id"])
            if r["context"] and r["lexeme_id"] not in self.context:
                ctx = json.loads(r["context"])
                if ctx.get("example"):
                    self.context[r["lexeme_id"]] = {**ctx, "list": r["list_id"]}
        self.facts = {r["word"]: r for r in conn.execute(
            "SELECT * FROM word_facts WHERE has_fact = 1 AND confidence >= ?", (settings.facts.min_confidence,))}
        self.mnemonics = {r["key"]: r["mnemonic"] for r in conn.execute("SELECT * FROM mnemonics")}

    # ── helpers ───────────────────────────────────────────────────────────
    def home_list(self, lexeme_id: str) -> str:
        lists = self.membership.get(lexeme_id, [])
        ranked = sorted(lists, key=lambda l: self.priority.index(l) if l in self.priority else 999)
        return ranked[0] if ranked else ""

    def production_on(self, lexeme_id: str) -> bool:
        return any(not self.list_settings.get(l, {}).get("recognition_only") for l in self.membership.get(lexeme_id, []))

    def facts_on(self, lexeme_id: str) -> bool:
        return any(self.list_settings.get(l, {}).get("facts", True) for l in self.membership.get(lexeme_id, []))

    def type_label(self, lx) -> str:
        home = self.home_list(lx["id"])
        t = self.list_settings.get(home, {}).get("type")
        return t or _TYPE_LABEL.get(lx["pos"], lx["pos"])

    def add(self, *, key, lexeme_id, card_type, deck, fields: dict, tags: list[str], sort: int,
            image_key: str | None, recognition: bool, production: bool, blocked: str | None = None,
            audio_text: str | None = None):
        if self.mnemonics.get(key):
            fields["Note"] = ((fields.get("Note") or "") + ("<br>" if fields.get("Note") else "")
                              + "💡 " + _esc(self.mnemonics[key]))
        fields = {f: fields.get(f, "") or "" for f in FIELDS}
        fields["Key"] = key
        fields["Image"] = _img(image_key)
        fields["Recognition"] = "1" if recognition else ""
        fields["Production"] = "1" if production else ""
        reasons = []
        if blocked:
            reasons.append(blocked)
        if not image_exists(image_key):
            reasons.append("image")
        if not (recognition or production):
            reasons.append("no card direction")
        self.notes.append({
            "key": key, "lexeme_id": lexeme_id, "card_type": card_type, "deck": deck,
            "fields": fields, "tags": sorted(set(tags)), "sort": sort,
            "ready": not reasons, "blocked_by": ",".join(reasons) or None,
            "audio_text": audio_text,
        })

    def fact_fields(self, lx) -> dict:
        if not self.facts_on(lx["id"]):
            return {}
        f = self.facts.get(fact_key(lx["lemma"]))
        if not f:
            return {}
        return {"Fact": _esc(f["fact"]), "FactTitle": FACT_TITLES.get(f["kind"] or "", "Did you know?")}

    def example_field(self, lexeme_id: str) -> str:
        ctx = self.context.get(lexeme_id)
        if not ctx:
            return ""
        title = self.lists.get(ctx["list"])
        where = f"{title['title']}, {ctx.get('example_at', '')}" if title else ""
        return f"<i>“{_esc(ctx['example'])}”</i><br><span class=\"src\">— {_esc(where)}</span>"

    # ── card types ────────────────────────────────────────────────────────
    def vocab_and_phrases(self):
        rows = self.conn.execute(
            "SELECT * FROM lexemes WHERE id IN (SELECT lexeme_id FROM list_items)").fetchall()
        senses = defaultdict(list)
        for s in self.conn.execute("SELECT * FROM senses ORDER BY lexeme_id, idx"):
            senses[s["lexeme_id"]].append(s)
        for lx in rows:
            sort = self.order.get(lx["id"], 1 << 30) * 10
            home = self.home_list(lx["id"])
            deck = self.lists[home]["deck"] if home in self.lists else "Italian::Other"
            tags = [f"list::{l}" for l in self.membership[lx["id"]]] + [f"pos::{lx['pos']}"]
            if lx["cognate_rule"]:
                tags.append("cognate")
            # deterministic roots (numbers, letters) need no AI sense
            if lx["pos"] in ("num", "letter"):
                hint_row = self.conn.execute("SELECT hint FROM list_items WHERE lexeme_id = ? AND hint IS NOT NULL", (lx["id"],)).fetchone()
                english = (hint_row[0] if hint_row else lx["lemma"]).split(" / ")[0]
                sense_rows = [{"prompt": english, "hint": None, "register": None, "note": None, "also": None}]
            else:
                sense_rows = senses.get(lx["id"]) or []
            blocked = None
            if not sense_rows:
                blocked = "awaiting AI"
                sense_rows = [{"prompt": "", "hint": None, "register": None, "note": None, "also": None}]
            if lx["status"] == "needs_review":
                blocked = "needs review"
            card_type = "phrase" if lx["pos"] == "phrase" else "vocab"
            for i, s in enumerate(sense_rows):
                details, forms = self._details(lx)
                note = " · ".join(x for x in (s["note"], f"also: {s['also']}" if s["also"] else None) if x)
                english = s["prompt"]
                production = self.production_on(lx["id"]) and english.strip().lower() != lx["display"].strip().lower()
                self.add(
                    key=f"{card_type}:{lx['id']}:{i}", lexeme_id=lx["id"], card_type=card_type, deck=deck,
                    fields={
                        "Italian": _esc(lx["display"]), "English": _esc(english),
                        "Hint": _esc(s["hint"]),
                        "Labels": labels_html(labels(type=self.type_label(lx), extra=f"register: {s['register']}" if s["register"] else None)),
                        "Details": _esc(details), "Forms": _esc(forms), "IPA": _esc(lx["ipa"]),
                        "Example": self.example_field(lx["id"]), "Note": _esc(note),
                        "Audio": _snd(_spoken(lx["display"])),
                        "Source": _esc(", ".join(self.lists[l]["title"] for l in self.membership[lx["id"]] if l in self.lists)),
                        **(self.fact_fields(lx) if i == 0 else {}),
                    },
                    tags=tags, sort=sort + i, image_key=lx["image_key"],
                    recognition=True, production=production, blocked=blocked,
                    audio_text=_spoken(lx["display"]),
                )

    def _details(self, lx) -> tuple[str, str]:
        if lx["pos"] == "noun":
            parts = [_GENDER.get(lx["gender"] or "")]
            if lx["plural"] and lx["gender"] in ("masculine", "feminine"):
                parts.append("plural: " + with_article(definite_article(lx["plural"], lx["gender"], True), lx["plural"]))
            return " · ".join(p for p in parts if p), ""
        if lx["pos"] == "adj" and lx["forms_json"]:
            f = json.loads(lx["forms_json"])
            forms = [f.get(k) for k in ("ms", "fs", "mp", "fp")]
            return "", " · ".join(dict.fromkeys(x for x in forms if x))
        if lx["pos"] == "verb" and lx["forms_json"]:
            info = json.loads(lx["forms_json"])
            aux = info.get("auxiliary")
            aux = "avere / essere" if aux == "both" else aux
            parts = [f"auxiliary: {aux}" if aux else None,
                     f"past participle: {info['past_participle'].removesuffix('si')}" if info.get("past_participle") else None,
                     "irregular" if info.get("irregular") else None,
                     "reflexive" if info.get("reflexive") else None]
            pres = info.get("forms", {}).get("presente", {})
            forms = " · ".join(pres[p] for p in ("io", "tu", "lui_lei", "noi", "voi", "loro") if p in pres)
            return " · ".join(p for p in parts if p), (f"presente: {forms}" if info.get("irregular") and forms else "")
        return "", ""

    def verb_form_notes(self):
        prompts = defaultdict(dict)
        for r in self.conn.execute("SELECT * FROM form_prompts"):
            prompts[r["lexeme_id"]][(r["tense"], r["person"])] = r["english"]
        meanings = {r["lexeme_id"]: r["prompt"] for r in self.conn.execute("SELECT lexeme_id, prompt FROM senses WHERE idx = 0")}
        tense_rank = {t: i for i, t in enumerate(load_plan().get("conjugation", {}).get("tenses", []))}
        for vid in full_form_verbs(self.conn):
            lx = self.conn.execute("SELECT * FROM lexemes WHERE id = ?", (vid,)).fetchone()
            info = json.loads(lx["forms_json"] or "{}")
            cls = conjugation_class(lx["lemma"], info.get("forms", {}).get("presente", {}).get("io"))
            model = lx["lemma"] in MODEL_VERBS.values()
            for tense, persons in verb_forms(lx).items():
                for person, (display, audio) in persons.items():
                    english = prompts[vid].get((tense, person), "")
                    details = f"{lx['lemma']} — {meanings.get(vid, '')}".rstrip(" —")
                    if model:
                        details += f" · model for regular {cls} verbs"
                    self.add(
                        key=f"form:{vid}:{tense}:{person}", lexeme_id=vid, card_type="form",
                        deck=DECK_FORMS.format(tense=TENSE_DISPLAY.get(tense, tense)),
                        fields={
                            "Italian": _esc(display), "English": _esc(english),
                            "Hint": "formal" if person == "Lei" else "",
                            "Labels": labels_html(labels(type="verb", tense=tense, subject=person)),
                            "Details": _esc(details), "Audio": _snd(audio),
                            "Source": "conjugation",
                        },
                        tags=["verb-form", f"tense::{tense}", f"verb::{lx['lemma']}"] + (["pattern"] if model else []),
                        sort=(self.order.get(vid, 1 << 29) * 10) + 1_000_000_000 + tense_rank.get(tense, 9) * 10,
                        image_key=lx["image_key"], recognition=False, production=True,
                        blocked=None if english else "awaiting AI", audio_text=audio,
                    )

    def noun_phrase_notes(self):
        if not load_plan().get("cards", {}).get("noun_phrases", True):
            return
        for lx in self.conn.execute(
            """SELECT l.*, s.prompt FROM lexemes l JOIN senses s ON s.lexeme_id = l.id AND s.idx = 0
               WHERE l.pos = 'noun' AND l.gender IN ('masculine', 'feminine') AND l.status = 'ready'
                 AND l.id IN (SELECT lexeme_id FROM list_items)"""
        ):
            word, g = lx["lemma"], lx["gender"]
            en = _first_alt(lx["prompt"])
            en_pl = lx["english_plural"] or ""
            idx = int(hashlib.md5(word.encode()).hexdigest()[:8], 16) % len(_PHRASE_FAMILIES)
            family, arg = _PHRASE_FAMILIES[idx]
            variants = [("singular", word, en)]
            if lx["plural"] and en_pl:
                variants.append(("plural", lx["plural"], en_pl))
            for number, form, english in variants:
                plural = number == "plural"
                if family == "indefinite":
                    if plural:
                        it = articulated_phrase("di", form, g, True)  # dei/degli/delle = some
                        eng = f"some {english}"
                    else:
                        it = with_article(indefinite_article(form, g), form)
                        eng = f"a {english}"
                elif family == "prep":
                    it = articulated_phrase(arg, form, g, plural)
                    eng = f"{_EN_PREP[arg]} {english}"
                elif family == "dem":
                    it = demonstrative_phrase(arg, form, g, plural)
                    eng = f"{('these' if plural else 'this') if arg == 'questo' else ('those' if plural else 'that')} {english}"
                else:
                    it = possessive_phrase(arg, form, g, plural)
                    eng = f"{_EN_POSS[arg]} {english}"
                self.add(
                    key=f"nphrase:{lx['id']}:{family}-{arg}:{number}", lexeme_id=lx["id"], card_type="nphrase",
                    deck=DECK_NPHRASES,
                    fields={
                        "Italian": _esc(it), "English": _esc(eng),
                        "Labels": labels_html(labels(type="noun phrase", phrase={"prep": "articulated_preposition", "dem": "demonstrative", "poss": "possessive"}.get(family, family), preposition=arg if family == "prep" else None, number=number)),
                        "Details": _esc(f"{lx['display']} · {_GENDER[g]}"), "Audio": _snd(it),
                        "Source": "rules",
                    },
                    tags=["noun-phrase", f"noun::{word}"],
                    sort=(self.order.get(lx["id"], 1 << 29) * 10) + 2_000_000_000 + (1 if plural else 0),
                    image_key=lx["image_key"], recognition=False, production=True,
                    audio_text=it,
                )

    def cognate_notes(self):
        if not load_plan().get("cards", {}).get("cognates", True):
            return
        groups = defaultdict(list)
        for r in self.conn.execute(
            """SELECT l.*, s.prompt FROM lexemes l JOIN senses s ON s.lexeme_id = l.id AND s.idx = 0
               WHERE l.cognate_rule IS NOT NULL AND l.cognate_rule != 'similar spelling' AND l.status = 'ready'
                 AND l.id IN (SELECT lexeme_id FROM list_items) ORDER BY l.freq_rank"""
        ):
            groups[r["cognate_rule"]].append(r)
        for rule, words in groups.items():
            if len(words) < 3:
                continue
            ex = words[:6]
            it_suffix, en_suffix = [x.strip() for x in rule.split("(")[0].split("=")]
            self.add(
                key=f"cognate:{md5_hex(rule)}", lexeme_id=None, card_type="cognate", deck=DECK_COGNATES,
                fields={
                    "Italian": _esc(f"{it_suffix}  ·  " + ", ".join(w["display"] for w in ex)),
                    "English": _esc(f"{en_suffix}  ·  " + ", ".join(_first_alt(w["prompt"]) for w in ex)),
                    "Labels": labels_html(labels(type="cognate pattern")),
                    "Details": _esc(f"{len(words)} words in your lists follow this pattern"),
                    "Note": _esc(rule), "Source": "rules",
                },
                tags=["cognate-rule"], sort=3_000_000_000 + len(groups),
                image_key=ex[0]["image_key"], recognition=True, production=True,
            )

    def mistake_notes(self):
        for r in self.conn.execute("SELECT * FROM mistakes"):
            key = r["italian"]
            self.add(
                key=f"mistake:{r['id']}", lexeme_id=None, card_type="mistake", deck=DECK_MISTAKES,
                fields={"Italian": _esc(r["italian"]), "English": _esc(r["english"]),
                        "Note": _esc(r["note"]), "Labels": labels_html(labels(type="practice mistake")),
                        "Audio": _snd(r["italian"]), "Source": "practice"},
                tags=["mistake"], sort=500, image_key=key, recognition=True, production=True,
                audio_text=r["italian"],
            )


def build(conn: sqlite3.Connection) -> dict:
    """Compute every v4 note into ``v4_notes``; returns counts."""
    print_banner("notes — desired Anki state")
    b = _Builder(conn)
    b.vocab_and_phrases()
    b.verb_form_notes()
    b.noun_phrase_notes()
    b.cognate_notes()
    b.mistake_notes()
    b.notes.sort(key=lambda n: (n["sort"], n["key"]))
    conn.execute("DELETE FROM v4_notes")
    conn.executemany(
        "INSERT OR REPLACE INTO v4_notes (key, lexeme_id, card_type, deck, fields_json, fields_hash, tags, sort_order, ready, blocked_by, audio_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(n["key"], n["lexeme_id"], n["card_type"], n["deck"], json.dumps(n["fields"], ensure_ascii=False),
          md5_hex(json.dumps([n["deck"], n["fields"], n["tags"]], ensure_ascii=False, sort_keys=True)),
          " ".join(n["tags"]), i + 1, 1 if n["ready"] else 0, n["blocked_by"], n["audio_text"])
         for i, n in enumerate(b.notes)],
    )
    conn.commit()
    counts = defaultdict(lambda: [0, 0])
    for n in b.notes:
        counts[n["card_type"]][0 if n["ready"] else 1] += 1
    for ct, (ready, waiting) in sorted(counts.items()):
        print(f"  {ct:<8} {ready:>6} ready  {waiting:>6} waiting")
    blocked = defaultdict(int)
    for n in b.notes:
        for reason in (n["blocked_by"] or "").split(","):
            if reason:
                blocked[reason] += 1
    if blocked:
        print("  waiting on: " + ", ".join(f"{k} {v}" for k, v in sorted(blocked.items(), key=lambda kv: -kv[1])))
    return {k: {"ready": v[0], "waiting": v[1]} for k, v in counts.items()}


__all__ = ["build", "MODEL_NAME", "FIELDS"]


# ── v4 notetype (created and kept in sync by the reconciler) ───────────────

CSS = """
.card { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  font-size: 18px; text-align: center; color: #333; background-color: #f4f4f9; padding: 10px; }
.card-container { background-color: white; border-radius: 15px; padding: 20px;
  box-shadow: 0 2px 5px rgba(0,0,0,0.1); max-width: 90%; margin: 0 auto; }
.meta-row { display: flex; justify-content: center; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
.pill { display: inline-block; padding: 4px 12px; border-radius: 999px; font-size: 0.75em;
  font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; }
.pill.type { background-color: #f3e8ff; color: #6b21a8; }
.pill.tense { background-color: #ede9fe; color: #5b21b6; }
.pill.subject { background-color: #fce7f3; color: #9d174d; }
.pill.phrase { background-color: #fef3c7; color: #92400e; }
.pill.preposition { background-color: #fef9c3; color: #713f12; }
.pill.number { background-color: #e0f2fe; color: #075985; }
.pill.register { background-color: #fee2e2; color: #991b1b; }
.front-text { font-size: 2em; font-weight: 700; color: #2c3e50; line-height: 1.3; }
.hint { margin-top: 6px; color: #6b7280; font-size: 0.9em; font-style: italic; }
.back-highlight { font-size: 2em; font-weight: 700; color: #e74c3c; margin-bottom: 8px; }
.ipa { color: #6b7280; font-size: 0.95em; margin-top: 4px; }
.details, .forms { font-size: 1.05em; color: #6b7280; font-style: italic; line-height: 1.5; margin-top: 6px; }
.forms { font-style: normal; color: #374151; }
.example { margin: 14px auto 0; max-width: 34em; color: #374151; font-size: 0.95em; line-height: 1.5; }
.example .src { color: #9ca3af; font-size: 0.85em; }
.note { margin-top: 10px; color: #4b5563; font-size: 0.9em; }
.fact { margin: 16px auto 0; max-width: 34em; padding: 10px 14px; border-left: 4px solid #f59e0b;
  border-radius: 8px; background-color: #fffbeb; color: #78350f; font-size: 0.85em; line-height: 1.5; text-align: left; }
.fact-title { display: block; font-size: 0.75em; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.06em; color: #b45309; margin-bottom: 2px; }
.card-image { margin: 12px 0; }
.card-image img { max-height: 420px; max-width: 100%; border-radius: 10px; }
hr#answer { border: 0; border-top: 1px solid #ddd; margin: 20px 0; }
.nightMode .card, .night_mode .card { background-color: #1f2937; color: #e5e7eb; }
.nightMode .card-container, .night_mode .card-container { background-color: #111827; }
.nightMode .front-text, .night_mode .front-text { color: #f3f4f6; }
.nightMode .forms, .night_mode .forms, .nightMode .example, .night_mode .example { color: #d1d5db; }
.nightMode .fact, .night_mode .fact { background-color: #3b2a12; color: #fde68a; }
.nightMode .fact-title, .night_mode .fact-title { color: #fbbf24; }
"""

_EXTRAS = """
  {{#Details}}<div class="details">{{Details}}</div>{{/Details}}
  {{#Forms}}<div class="forms">{{Forms}}</div>{{/Forms}}
  {{#Example}}<div class="example">{{Example}}</div>{{/Example}}
  {{#Note}}<div class="note">{{Note}}</div>{{/Note}}
  {{#Fact}}<div class="fact"><span class="fact-title">{{FactTitle}}</span>{{Fact}}</div>{{/Fact}}
"""

TEMPLATES = [
    {
        "Name": "Recognition",
        "Front": """{{#Recognition}}
<div class="card-container">
  {{Labels}}
  <div class="front-text">{{Italian}}</div>
  {{#IPA}}<div class="ipa">{{IPA}}</div>{{/IPA}}
  {{Audio}}
</div>
{{/Recognition}}""",
        "Back": """{{FrontSide}}
<hr id="answer">
<div class="card-container">
  <div class="back-highlight">{{English}}</div>
  {{#Hint}}<div class="hint">{{Hint}}</div>{{/Hint}}
  {{#Image}}<div class="card-image">{{Image}}</div>{{/Image}}""" + _EXTRAS + "</div>",
    },
    {
        "Name": "Production",
        "Front": """{{#Production}}
<div class="card-container">
  {{#Image}}<div class="card-image">{{Image}}</div>{{/Image}}
  {{Labels}}
  <div class="front-text">{{English}}</div>
  {{#Hint}}<div class="hint">{{Hint}}</div>{{/Hint}}
</div>
{{/Production}}""",
        "Back": """{{FrontSide}}
<hr id="answer">
<div class="card-container">
  <div class="back-highlight">{{Italian}}</div>
  {{#IPA}}<div class="ipa">{{IPA}}</div>{{/IPA}}
  {{Audio}}""" + _EXTRAS + "</div>",
    },
]
