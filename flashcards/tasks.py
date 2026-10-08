"""Every AI task in the project, defined in one place.

Each task is a :class:`TaskSpec`: a name (also the cache namespace and the
key for per-task models in ``settings.toml``), an instruction, a list of
rules, and a JSON schema. All prompts share one shape —

    Task: <instruction>
    Context: <context>            (optional, e.g. a source's prompt_hint)
    Rules:
      - …
    Input:
    {"italian": …}

— and one system preamble, so every model sees the same framing whether it
is glossing a word, conjugating a verb or auditing a card. Enrichment tasks
share the same core fields (``english``, ``disambiguation``, ``usage_note``,
``confidence``, ``valid``) so every mode formats English the same way
(``cards.merge_english``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .ai import Task
from .grammar import TENSES, VERB_PERSONS

SYSTEM = (
    "You are an expert Italian teacher and lexicographer building flashcards and "
    "study material for an English-speaking learner. Be accurate. When you are "
    "unsure, say so through the confidence field (or decline) rather than guessing. "
    "Follow the rules exactly and answer in the requested format."
)

_STYLE_RULES = (
    "English must be natural and concise; never include the Italian word in the English.",
    "usage_note: a very short label only if the item is archaic, formal, vulgar, "
    "literary, regional or colloquial; empty string for ordinary modern usage.",
    "disambiguation: a short clarifier only when the English could mean several "
    "things (e.g. 'right (direction)'); empty string otherwise.",
    "confidence: 0–1, how sure you are that the answer is correct.",
)


@dataclass(frozen=True)
class TaskSpec:
    name: str
    instruction: str
    rules: tuple[str, ...] = ()
    schema: dict | None = None
    timeout: int | None = None
    cache: bool = True
    system: str = SYSTEM

    def task(self, item, *, context: str | None = None, rules: tuple[str, ...] = ()) -> Task:
        parts = [f"Task: {self.instruction}"]
        if context:
            parts.append(f"Context: {context}")
        all_rules = self.rules + tuple(rules)
        if all_rules:
            parts.append("Rules:\n" + "\n".join(f"  - {r}" for r in all_rules))
        if item is not None:
            body = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
            parts.append(f"Input:\n{body}")
        return Task(
            name=self.name,
            system=self.system,
            prompt="\n\n".join(parts),
            schema=self.schema,
            timeout=self.timeout,
            cache=self.cache,
        )


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": required if required is not None else list(props),
        "additionalProperties": False,
    }


_STR = {"type": "string"}
_BOOL = {"type": "boolean"}
_CONF = {"type": "number", "minimum": 0.0, "maximum": 1.0}

#: Fields every enrichment answer carries (see ``cards.merge_english``).
_ENRICH = {
    "valid": _BOOL,
    "english": _STR,
    "disambiguation": _STR,
    "usage_note": _STR,
    "confidence": _CONF,
}


def _enrichment(**extra) -> dict:
    return _obj({**_ENRICH, **extra})


# ── Enrichment (one per mode) ──────────────────────────────────────────────

GLOSS = TaskSpec(
    name="gloss",
    instruction="Write the English side of a flashcard for this Italian vocabulary item.",
    rules=(
        "english: concise, natural gloss for the back of the card; keep the "
        "punctuation of exclamations. english_hint is the learner's own gloss — "
        "respect its meaning.",
        *_STYLE_RULES,
        "valid=false only if the entry is clearly erroneous.",
    ),
    schema=_enrichment(),
)

AVERE_GLOSS = TaskSpec(
    name="avere_gloss",
    instruction="Write the English base form of this Italian 'avere' expression.",
    rules=(
        "english: concise base form starting with 'to', e.g. 'to be hungry' for "
        "'avere fame'. Separate genuine alternatives with ' / '.",
        *_STYLE_RULES,
        "valid=false only if this is clearly not an avere expression.",
    ),
    schema=_enrichment(),
)

VERB_META = TaskSpec(
    name="verb_meta",
    instruction="Identify this Italian verb and describe it for a flashcard.",
    rules=(
        "lemma and infinitive: the canonical Italian infinitive, lowercase "
        "(reflexive verbs keep -si, e.g. 'trasferirsi').",
        "english: concise gloss starting with 'to', e.g. 'to go'.",
        *_STYLE_RULES,
        "auxiliary: avere | essere | both | unknown.",
        "past_participle: masculine singular, lowercase.",
        "is_reflexive: true for reflexive verbs.",
        "valid=false only if this is not a real Italian verb.",
    ),
    schema=_enrichment(
        lemma=_STR,
        infinitive=_STR,
        auxiliary={"type": "string", "enum": ["avere", "essere", "both", "unknown"]},
        past_participle=_STR,
        is_reflexive=_BOOL,
    ),
)

NOUN_META = TaskSpec(
    name="noun_meta",
    instruction="Identify this Italian noun and describe it for a flashcard.",
    rules=(
        "lemma and singular: canonical singular, lowercase.",
        "english: concise gloss, usually without an article.",
        "singular_english / plural_english: bare translations ('house' / 'houses').",
        *_STYLE_RULES,
        "definite_singular ∈ {il, lo, l', la, ''}; definite_plural ∈ {i, gli, le, ''}; "
        "indefinite_singular ∈ {un, uno, una, un', ''}.",
        "plural: empty string if the noun has no plural.",
        "valid=false only if this is clearly not a real Italian noun.",
    ),
    schema=_enrichment(
        lemma=_STR,
        singular=_STR,
        singular_english=_STR,
        plural=_STR,
        plural_english=_STR,
        gender={"type": "string", "enum": ["masculine", "feminine", "both", "unknown"]},
        definite_singular=_STR,
        definite_plural=_STR,
        indefinite_singular=_STR,
    ),
)

# ── Generated forms ────────────────────────────────────────────────────────

VERB_FORMS = TaskSpec(
    name="verb_forms",
    instruction="Conjugate this Italian verb for flashcards. Generate exactly the "
                "(tense, person) pairs listed in the rules and nothing else.",
    rules=(
        "presente_progressivo: stare in the presente + gerundio (e.g. 'sto parlando'); "
        "for reflexives the pronoun is usually proclitic on stare ('mi sto lavando').",
        "passato_prossimo: use the supplied auxiliary and past participle.",
        "futuro_semplice: simple future; respect irregular stems (avrò, sarò, andrò, "
        "vedrò, farò, dirò…).",
        "condizionale_passato: the auxiliary in the present conditional + past "
        "participle ('avrei parlato', 'sarei andato'); with essere agree the participle, "
        "using the masculine when gender is ambiguous.",
        "imperativo has no io form; Lei is the formal-you imperative.",
        "Reflexive verbs include the correct reflexive pronouns.",
        "english: a natural prompt, e.g. 'we speak / we are speaking', 'Speak!', "
        "'I will speak', 'I would have spoken'.",
        "usage_note: very short label only if archaic/formal/vulgar/literary/regional.",
    ),
    schema=_obj({"forms": {"type": "array", "items": _obj({
        "tense": {"type": "string", "enum": list(TENSES)},
        "person": {"type": "string", "enum": list(VERB_PERSONS)},
        "italian": _STR,
        "english": _STR,
        "usage_note": _STR,
    })}}),
    timeout=90,
)

NOUN_PHRASES = TaskSpec(
    name="noun_phrases",
    instruction="Build Italian noun phrases for flashcards. Build exactly the "
                "phrases listed in the rules and nothing else.",
    rules=(
        "Use the correct article for the noun's gender and starting sound.",
        "For nouns with no plural, only build singular phrases.",
        "english: definite 'the …'; indefinite 'a/an …' or 'some …'; demonstrative "
        "'this/these …' for questo, 'that/those …' for quello; possessive my / your / "
        "his/her / our / your (pl) / their.",
        "usage_note: empty unless archaic/formal/vulgar/literary/regional.",
    ),
    schema=_obj({"phrases": {"type": "array", "items": _obj({
        "phrase_type": {"type": "string", "enum": [
            "definite", "indefinite", "articulated_preposition", "demonstrative", "possessive",
        ]},
        "number": {"type": "string", "enum": ["singular", "plural"]},
        "preposition": _STR,
        "italian": _STR,
        "english": _STR,
        "usage_note": _STR,
    })}}),
    timeout=90,
)

# ── Fun facts ──────────────────────────────────────────────────────────────

FACT_KINDS = ("etymology", "english_link", "false_friend", "culture", "usage")

WORD_FACTS = TaskSpec(
    name="word_facts",
    instruction="For each Italian word in the input list, decide whether it has a "
                "genuinely interesting, memorable fact worth putting on its flashcard, "
                "and if so write it.",
    rules=(
        "Return exactly one entry per input word, copying its 'italian' value exactly.",
        "Good facts: where the word comes from (Latin, Greek, French, Arabic, "
        "Germanic…), a surprising link to an English word (shared root, cognate), a "
        "false-friend warning, or a memorable cultural or usage note.",
        "Style example — rubinetto: 'From French robinet, from Robin, a traditional "
        "name for a sheep: early taps were often shaped like a ram's head.'",
        "Only state facts that are well established in standard etymological "
        "dictionaries. Never invent or speculate. If unsure, has_fact=false.",
        "Be selective: most words (about two in three) should get has_fact=false. "
        "Skip anything obvious (e.g. 'telefono' looks like 'telephone').",
        "fact: plain English, one or two sentences, at most 220 characters, no "
        "markdown, and do not start with the word itself.",
        "kind: etymology | english_link | false_friend | culture | usage "
        "(when has_fact=false use 'etymology' and an empty fact).",
        "confidence: 0–1, how sure you are the fact is correct.",
    ),
    schema=_obj({"facts": {"type": "array", "items": _obj({
        "italian": _STR,
        "has_fact": _BOOL,
        "kind": {"type": "string", "enum": list(FACT_KINDS)},
        "fact": _STR,
        "confidence": _CONF,
    })}}),
)

# ── Media ──────────────────────────────────────────────────────────────────

IMAGE_PROMPT = TaskSpec(
    name="image_prompt",
    instruction="Write an image-generation prompt for the illustration on this "
                "Italian flashcard.",
    rules=(
        "2–3 sentences describing one flat-design, minimalist, icon-style illustration.",
        "Depict the meaning of the Italian; it takes precedence when the English is "
        "ambiguous. Simple and clear for a language learner.",
        "STRICTLY no text, letters, numbers or labels in the image.",
    ),
    schema=_obj({"prompt": _STR}),
)

# ── Quality ────────────────────────────────────────────────────────────────

CARD_AUDIT = TaskSpec(
    name="card_audit",
    instruction="Audit this automatically generated Italian flashcard.",
    rules=(
        "correctness: the Italian and English mean the same thing.",
        "grammar: the Italian is correct — gender, number agreement, conjugation, "
        "articles, accents.",
        "naturalness: both sides read naturally to a native speaker.",
        "consistency: front, back, labels, details and fact all describe the same item.",
        "fact (if present): it must be accurate; an invented or doubtful etymology is a fail.",
        "A fine card is verdict 'pass', severity 0, empty issues and suggestion. Use "
        "'warn' (severity 1–2) for minor stylistic nits and 'fail' (3–5) for errors "
        "that would mislead a learner.",
        "issues: one or two sentences. suggestion: the corrected text when you propose "
        "a fix, else an empty string.",
    ),
    schema=_obj({
        "verdict": {"type": "string", "enum": ["pass", "warn", "fail"]},
        "severity": {"type": "integer", "minimum": 0, "maximum": 5},
        "categories": {"type": "array", "items": {"type": "string", "enum": [
            "correctness", "grammar", "naturalness", "consistency", "fact",
        ]}},
        "issues": _STR,
        "suggestion": _STR,
    }),
    timeout=90,
)

# ── Practice (interactive; never cached) ───────────────────────────────────

PRACTICE_SENTENCE = TaskSpec(
    name="practice_sentence",
    instruction="Write one natural English sentence for the learner to translate "
                "into Italian, using at least one item from the word bank.",
    rules=(
        "Refer to word-bank items by their English meaning; do NOT write Italian in "
        "the English sentence.",
        "Natural and conversational, at an intermediate level.",
        "italian: the correct, natural Italian translation.",
        "words_used: the English meanings from the word bank that appear in the sentence.",
    ),
    schema=_obj({"english": _STR, "italian": _STR, "words_used": {"type": "array", "items": _STR}}),
    timeout=90,
    cache=False,
)

PRACTICE_FEEDBACK = TaskSpec(
    name="practice_feedback",
    instruction="Give focused error feedback on the learner's Italian translation.",
    rules=(
        "List every error as a short bullet point: what is wrong and the correct form, "
        "plus a one-sentence explanation of the rule (name tense/person/number for verb errors).",
        "Do not skip any error, even minor spelling mistakes.",
        "Ignore missing or incorrect accents entirely — do not mention them.",
        "No summary section, and do not restate the correct Italian sentence — the "
        "learner can already see it.",
        "If the attempt is fully correct (ignoring accents), say so in one line.",
    ),
    schema=None,
    cache=False,
)
