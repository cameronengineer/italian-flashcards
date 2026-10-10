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
    "If a draft_fact is given, keep it only if it is consistent with the sourced etymology; "
    "otherwise rewrite or drop it.",
    "fact.kind: etymology | english_link | false_friend | culture | usage; fact.confidence 0–1.",
)

_FEATURES = _obj(
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
        "source_id: select the supporting ID from meaning_evidence.candidates. Retain the same "
        "dictionary meaning ID when only wording changes; never merge distinct homonyms. "
        "features: give sense-specific persons, auxiliary and construction for verbs; [] means "
        "unrestricted persons. Event meanings use it/they, not I/we happen. For nouns give singular "
        "gender and the plural, plural gender and English plural that fit THIS meaning. "
        "Leave inapplicable feature strings empty. Do not guess a plural for an ambiguous meaning.",
        "english_plural: for nouns, the English plural of the prompt (house → houses); else empty.",
        "verified: false and list issues when the dictionary data looks wrong (gender, part of "
        "speech, meaning) — say what you believe is right. Empty issues when it checks out.",
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
                                    "features": _FEATURES,
                                }
                            ),
                        },
                        "english_plural": _STR,
                        "verified": _BOOL,
                        "issues": {"type": "array", "items": _STR},
                        "fact": _obj(
                            {
                                "has_fact": _BOOL,
                                "kind": {"type": "string", "enum": list(FACT_KINDS)},
                                "text": _STR,
                                "confidence": _CONF,
                            }
                        ),
                    }
                ),
            }
        }
    ),
    timeout=900,
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
                    }
                ),
            }
        }
    ),
    timeout=900,
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
        "Exclude modal imperatives that are not ordinarily used. Preserve usable existing prompts.",
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
    timeout=900,
)

DISAMBIGUATE = TaskSpec(
    name="disambiguate",
    instruction="Each group is several Italian words whose flashcards would show the same "
    "English prompt. Give each word a short hint so the learner knows which one "
    "is wanted, and list the other words as also-acceptable answers.",
    rules=(
        "hint: at most 4 words that teach the real difference (register, nuance, typical use), "
        "e.g. 'most common', 'formal', 'literary', 'of a person'.",
        "also: comma-separated other words from the group that would also be correct.",
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
    timeout=900,
)
