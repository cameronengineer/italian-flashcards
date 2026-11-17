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
is writing a word's prompt, conjugating a verb or auditing a card. Every
structured task has a strict JSON schema; answers are validated against it
before anything is stored (:mod:`flashcards.validation`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .ai import Task

SYSTEM = (
    "You are an expert Italian teacher and lexicographer building flashcards and "
    "study material for an English-speaking learner. Be accurate. When you are "
    "unsure, say so through the confidence field (or decline) rather than guessing. "
    "Follow the rules exactly and answer in the requested format."
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


def _obj(props: dict, optional: tuple[str, ...] = ()) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": [k for k in props if k not in optional],
        "additionalProperties": False,
    }


#: Leaving these out means "keep the entry".
_OPTIONAL_IGNORE = ("ignore", "ignore_reason")


_STR = {"type": "string"}
_BOOL = {"type": "boolean"}
_CONF = {"type": "number", "minimum": 0.0, "maximum": 1.0}

FACT_KINDS = ("etymology", "english_link", "false_friend", "culture", "usage")


# ── Quality ────────────────────────────────────────────────────────────────

_AUDIT_RULES = (
    "correctness: the Italian and English mean the same thing.",
    "grammar: the Italian is correct — gender, number agreement, conjugation, articles, accents.",
    "naturalness: both sides read naturally to a native speaker.",
    "consistency: front, back, labels, details and fact all describe the same item.",
    "fact (if present): it must be accurate; an invented or doubtful etymology is a fail.",
    "A fine card is verdict 'pass', severity 0, empty issues and suggestion. Use "
    "'warn' (severity 1–2) for minor stylistic nits and 'fail' (3–5) for errors "
    "that would mislead a learner.",
    "issues: one or two sentences. suggestion: the corrected text when you propose "
    "a fix, else an empty string.",
)

# ── Practice (interactive; never cached) ───────────────────────────────────

PRACTICE_SENTENCE = TaskSpec(
    name="practice_sentence",
    instruction="Write one natural English sentence for the learner to translate "
    "into Italian, using at least one item from the word bank.",
    rules=(
        "Refer to word-bank items by their English meaning; do NOT write Italian in the English sentence.",
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


# ══════════════════════════ v4: verify, don't recall ══════════════════════════
# Every v4 task gets the looked-up data (Wiktionary via Kaikki, the lists'
# own glosses, film lines) and asks Claude to choose, phrase and check it.

_FACT_RULES = (
    "fact: only if genuinely interesting and memorable (most words: has_fact=false). Prefer the "
    "sourced etymology given (Wiktionary) and its source-language entry; you may add widely "
    "documented detail, but never invent. Good facts: origin story, a surprising link to an "
    "English word, a false-friend warning, a cultural note. Plain English, at most 220 "
    "characters, no markdown, do not start with the word itself.",
    "fact.kind: etymology | english_link | false_friend | culture | usage; fact.confidence 0–1.",
)

_IGNORE_RULE = (
    "ignore: true only when this entry should not be a flashcard at all: it is not Italian, it is a "
    "person or place name, a stray abbreviation or letter, a fragment of a bad parse, or a rare "
    "secondary use whose ordinary meaning is studied on another card (also_listed_as shows the "
    "other parts of speech this word has in the lists, e.g. the noun l'entrare when the verb "
    "entrare is listed). ignore_reason: one short sentence saying why (name the other card if "
    "that is the reason). Otherwise ignore=false and ignore_reason empty. When in doubt, keep it. "
    "Fill in every other field either way."
)

SENSE_FEATURES = _obj(
    {
        "persons": {
            "type": "array",
            "items": {"type": "string", "enum": ["io", "tu", "lui_lei", "noi", "voi", "loro", "Lei"]},
        },
        "auxiliary": {"type": "string", "enum": ["", "avere", "essere"]},
        "construction": _STR,
        "singular_gender": {"type": "string", "enum": ["", "masculine", "feminine", "both"]},
        "plural": _STR,
        "plural_gender": {"type": "string", "enum": ["", "masculine", "feminine"]},
        "english_plural": _STR,
    }
)

LEXEME_ENRICH = TaskSpec(
    name="lexeme_enrich",
    instruction="For each Italian word, write the English side of its flashcard from the "
    "dictionary data provided, verify that data, and add a fun fact where one is "
    "genuinely worth it. Return one entry per input id.",
    rules=(
        "senses: usually exactly one — the meaning the lists use (list_glosses and contexts "
        "show which). Add a second only when the lists clearly use two different meanings.",
        "prompt: the English a learner sees and must turn into the Italian. Natural, short "
        "(ideally under 40 characters), never containing the Italian word. Verbs start with "
        "'to'; nouns without an article; adjectives in their basic sense.",
        "hint: at most 3 words, only when the English prompt itself is ambiguous "
        "(e.g. 'right' → hint 'direction'). Empty otherwise.",
        "register: formal | colloquial | vulgar | literary | regional | archaic, or empty.",
        "note: a short usage note for the back of the card (≤ 120 characters), or empty.",
        "source_id: the source_id (m1, m2 …) of the meaning_evidence candidate this sense is; "
        "never merge distinct homonyms.",
        "features: for verbs, persons lists the subjects this meaning is used with — leave it [] "
        "(any subject) unless the meaning genuinely restricts them (event meanings use it/they, "
        "not I/we happen); give the auxiliary and construction. For nouns give singular gender and "
        "the plural, plural gender and English plural that fit THIS meaning. Leave inapplicable "
        "feature strings empty. Do not guess a plural for an ambiguous meaning.",
        "english_plural: for nouns, the English plural of the prompt (house → houses); else empty.",
        "verified: false and list issues when the dictionary data looks wrong (gender, part of "
        "speech, meaning) — say what you believe is right. Empty issues when it checks out.",
        _IGNORE_RULE,
        *_FACT_RULES,
    ),
    schema=_obj(
        {
            "words": {
                "type": "array",
                "items": _obj(
                    {
                        "id": _STR,
                        "senses": {
                            "type": "array",
                            "items": _obj(
                                {
                                    "prompt": _STR,
                                    "hint": _STR,
                                    "register": _STR,
                                    "note": _STR,
                                    "source_id": _STR,
                                    "features": SENSE_FEATURES,
                                }
                            ),
                        },
                        "english_plural": _STR,
                        "verified": _BOOL,
                        "issues": {"type": "array", "items": _STR},
                        "ignore": _BOOL,
                        "ignore_reason": _STR,
                        "fact": _obj(
                            {
                                "has_fact": _BOOL,
                                "kind": {"type": "string", "enum": list(FACT_KINDS)},
                                "text": _STR,
                                "confidence": _CONF,
                            }
                        ),
                    },
                    _OPTIONAL_IGNORE,
                ),
            }
        }
    ),
    timeout=None,  # [ai] timeout
)

PHRASE_ENRICH = TaskSpec(
    name="phrase_enrich",
    instruction="Write the English side of a flashcard for each Italian phrase or sentence "
    "and check the Italian. Return one entry per input id.",
    rules=(
        "prompt: natural English the learner must turn into the Italian; keep a trailing "
        "'…' when the Italian is a sentence starter.",
        "hint: at most 4 words when the English could be said several ways in Italian; else empty.",
        "note: short usage note (≤ 120 characters) or empty.",
        "verified: false with issues if the Italian has a mistake (say the correction).",
        "ignore: true only if this is not Italian or is too garbled to fix; ignore_reason says why "
        "in one sentence. Otherwise ignore=false and ignore_reason empty. When in doubt, keep it.",
    ),
    schema=_obj(
        {
            "phrases": {
                "type": "array",
                "items": _obj(
                    {
                        "id": _STR,
                        "prompt": _STR,
                        "hint": _STR,
                        "note": _STR,
                        "verified": _BOOL,
                        "issues": {"type": "array", "items": _STR},
                        "ignore": _BOOL,
                        "ignore_reason": _STR,
                    },
                    _OPTIONAL_IGNORE,
                ),
            }
        }
    ),
    timeout=None,  # [ai] timeout
)

VERB_PROMPTS = TaskSpec(
    name="verb_prompts",
    instruction="Write the English prompt for each Italian verb form given (forms come from "
    "Wiktionary). Return every (tense, person) pair that was given, per verb id.",
    rules=(
        "Present: 'we speak / we are speaking'; imperfetto: 'I used to speak / I was speaking'; "
        "passato prossimo: 'I spoke / I have spoken'; futuro: 'I will speak'; condizionale "
        "presente: 'I would speak'; condizionale passato: 'I would have spoken'; presente "
        "progressivo: 'I am speaking'; imperativo: 'Speak!' (noi: \"Let's speak!\").",
        "Use the subject in the prompt (I, you, he/she, we, you all, they); for 'Lei' "
        "imperatives write the command and nothing else — the card adds 'formal'.",
        "Reflexive verbs: reflect it naturally ('I wash myself' / 'I get washed' as fits).",
        "Return EACH requested pair exactly once in prompts OR exclusions. Every exclusion needs a "
        "specific reason (not appropriate for this meaning/construction, invalid form, etc.). "
        "Never silently omit a pair. Respect usage.persons, auxiliary and construction. "
        "For event subjects use it/they; do not translate succedere (happen) as I happen. "
        "Exclude imperatives that are not ordinarily used.",
        "Never include Italian. Keep each prompt under 50 characters.",
    ),
    schema=_obj(
        {
            "verbs": {
                "type": "array",
                "items": _obj(
                    {
                        "id": _STR,
                        "exclusions": {
                            "type": "array",
                            "items": _obj({"tense": _STR, "person": _STR, "reason": _STR}),
                        },
                        "prompts": {
                            "type": "array",
                            "items": _obj(
                                {
                                    "tense": _STR,
                                    "person": _STR,
                                    "english": _STR,
                                }
                            ),
                        },
                    }
                ),
            }
        }
    ),
    timeout=None,  # [ai] timeout
)

DISAMBIGUATE = TaskSpec(
    name="disambiguate",
    instruction="Each group is several Italian words whose flashcards would show the same "
    "English prompt. Give each word a short hint so the learner knows which one "
    "is wanted. Only list alternatives that remain correct for that word's specific cue and hint.",
    rules=(
        "hint: at most 4 words that teach the real difference (register, nuance, typical use), "
        "e.g. 'most common', 'formal', 'literary', 'of a person'.",
        "also: comma-separated words from the group that are genuinely interchangeable for this "
        "cue INCLUDING its hint. Sharing an English translation does not make words interchangeable. "
        "For example, agire is not an alternative to recitare when the hint specifies acting in theatre. "
        "Leave also empty when the distinction rules the other word out.",
        "Return one entry per word id in every group.",
    ),
    schema=_obj(
        {
            "words": {
                "type": "array",
                "items": _obj(
                    {
                        "id": _STR,
                        "hint": _STR,
                        "also": _STR,
                    }
                ),
            }
        }
    ),
)

LEECH_HELP = TaskSpec(
    name="leech_help",
    instruction="The learner keeps failing these flashcards. For each, write one short "
    "memory aid that would make it stick.",
    rules=(
        "mnemonic: at most 160 characters — a vivid association, a sound-alike, a contrast "
        "with the word it's being confused with, or a tiny example sentence. No markdown.",
        "Return one entry per card id.",
    ),
    schema=_obj({"cards": {"type": "array", "items": _obj({"id": _STR, "mnemonic": _STR})}}),
)

MISTAKE_CARDS = TaskSpec(
    name="mistake_cards",
    instruction="Turn the learner's translation mistakes into flashcards: for each mistake, "
    "the short English prompt and the correct Italian chunk (not the whole sentence).",
    rules=(
        "One card per distinct mistake; skip accent-only errors.",
        "italian: the corrected chunk (2–6 words); english: what it means; note: the rule (≤ 120 chars).",
    ),
    schema=_obj(
        {
            "cards": {
                "type": "array",
                "items": _obj(
                    {
                        "italian": _STR,
                        "english": _STR,
                        "note": _STR,
                    }
                ),
            }
        }
    ),
    cache=False,
)


CARD_AUDITS = TaskSpec(
    name="card_audits",
    instruction="Audit these automatically generated Italian flashcards. Return one verdict per card id.",
    rules=_AUDIT_RULES,
    schema=_obj(
        {
            "cards": {
                "type": "array",
                "items": _obj(
                    {
                        "id": _STR,
                        "verdict": {"type": "string", "enum": ["pass", "warn", "fail"]},
                        "severity": {"type": "integer", "minimum": 0, "maximum": 5},
                        "categories": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": [
                                    "correctness",
                                    "grammar",
                                    "naturalness",
                                    "consistency",
                                    "fact",
                                ],
                            },
                        },
                        "issues": _STR,
                        "suggestion": _STR,
                    }
                ),
            }
        }
    ),
    timeout=None,  # [ai] timeout
)


CUE_CHECK = TaskSpec(
    name="cue_check",
    instruction="Each flashcard asks the learner to produce the Italian word from its English prompt "
    "and hint. Rewrite prompts and hints that give the answer away. Return one entry per input id.",
    rules=(
        "A prompt or hint gives the answer away when it offers an English word that looks like the "
        "Italian answer, as an alternative, in brackets or in the hint (precedence → la precedenza, "
        "licence → la licenza).",
        "Remove those look-alikes when another natural translation remains: 'priority (precedence; right "
        "of way)' → 'priority (right of way)'; 'licence, permit' → 'permit'; 'fight, combat' → 'fight'.",
        "Keep a look-alike when it is the only natural English for this meaning (nation → la nazione, "
        "piano (instrument) → il pianoforte), or when the prompt would otherwise point to a different "
        "word; then return the prompt unchanged.",
        "The result must still clearly mean this Italian word, not a different one (for categorico, "
        "'categorical, flat' must not become just 'flat'). The given hint separates this word from "
        "others with the same English: keep that job, minus any look-alike ('precedence; right of way' "
        "→ 'right of way').",
        "english: the prompt to show; hint: the hint to show (at most 4 words, or empty). Never "
        "include Italian.",
    ),
    schema=_obj({"cues": {"type": "array", "items": _obj({"id": _STR, "english": _STR, "hint": _STR})}}),
)


NOUN_NUMBER = TaskSpec(
    name="noun_number",
    instruction="For each Italian noun, decide which form its flashcard should teach for the meaning "
    "it is learned with (english, note). Return one entry per input id.",
    rules=(
        "Teach the most common usage. number = plural when Italians mostly use this noun in the plural, "
        "so a learner should learn the plural (gli occhiali = glasses, le forbici = scissors, i "
        "pantaloni = trousers, gli spaghetti). This includes plurals that are much more common than the "
        "singular with a shifted meaning: choose plural and give that meaning (i soldi = money rather "
        "than il soldo = penny; i mobili = furniture rather than il mobile = piece of furniture).",
        "number = singular when this meaning is normally only used in the singular and a plural would "
        "sound odd to a learner (la gente = people, il latte = milk, la fame = hunger, il sangue = blood).",
        "number = both for an ordinary countable noun (la casa / le case): the card shows the singular "
        "and its plural. When unsure, both.",
        "english: only when number is plural, the natural English for the plural as the card's prompt "
        "(glasses, scissors, money, furniture, spaghetti); otherwise empty.",
    ),
    schema=_obj(
        {
            "nouns": {
                "type": "array",
                "items": _obj(
                    {
                        "id": _STR,
                        "number": {"type": "string", "enum": ["singular", "plural", "both"]},
                        "english": _STR,
                    }
                ),
            }
        }
    ),
)


FACT_CHECK = TaskSpec(
    name="fact_check",
    instruction="Check the 'did you know?' fact shown on each Italian flashcard (the word and its "
    "meaning are given). Return one entry per input id.",
    rules=(
        "keep: the fact is accurate and genuinely helps an English speaker remember this word.",
        "fix: mostly good, but one claim must go or be corrected. text: the corrected fact in the same "
        "plain style (at most 220 characters, no markdown, not starting with the word). Only remove or "
        "correct claims; never add new ones.",
        "drop: inaccurate or doubtful, dull, or mainly about something other than this word.",
        "English words in a fact must be everyday words a typical adult native English speaker knows. "
        "A claim that English borrowed or uses an Italian word is only fine for common English words "
        "(pizza, piano, balcony); remove it for words most people have never heard (passeggiata, "
        "estival, aestivation), and drop the fact if nothing useful is left.",
        "text: empty unless the verdict is fix. reason: a few words.",
    ),
    schema=_obj(
        {
            "facts": {
                "type": "array",
                "items": _obj(
                    {
                        "id": _STR,
                        "verdict": {"type": "string", "enum": ["keep", "fix", "drop"]},
                        "text": _STR,
                        "reason": _STR,
                    }
                ),
            }
        }
    ),
    timeout=None,  # [ai] timeout
)


FILM_GLOSS = TaskSpec(
    name="film_gloss",
    instruction="Give a literal, word-for-word English gloss of each Italian film line, so a learner "
    "can see what every Italian word means. Return one entry per input id.",
    rules=(
        "words: one entry per Italian word, in the original order. Never merge, skip or reorder words.",
        "it: the Italian word exactly as written, with any punctuation attached (e.g. 'minuti.'). "
        "Together the it values must reproduce the whole line.",
        "en: the most literal English for that word alone, in this context, with the same punctuation "
        "attached. Do not smooth it into natural English.",
        "Join several English words for one Italian word with hyphens: Sono (subject implied) → 'I-am', "
        "andarci → 'to-go-there', dell' → 'of-the', c'è → 'there-is'.",
        "Keep names and numbers as they are.",
    ),
    schema=_obj(
        {
            "lines": {
                "type": "array",
                "items": _obj(
                    {"id": _STR, "words": {"type": "array", "items": _obj({"it": _STR, "en": _STR})}}
                ),
            }
        }
    ),
    timeout=None,  # [ai] timeout
)
