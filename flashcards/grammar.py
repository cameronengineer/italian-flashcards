"""Italian grammar constants shared across modes.

These are *language facts* (tense names, persons, article forms) rather than
per-deck or per-source choices. They live here so every mode reads from one
place and the per-source JSON config stays focused on deck-level settings.

If you ever localise this pipeline to another language, this is the file to
swap.
"""

from __future__ import annotations

# ── Verb tenses ─────────────────────────────────────────────────────────────
TENSES = ("presente", "passato_prossimo", "imperfetto", "imperativo")

#: Display strings appended to ``source.deck`` to build per-tense deck names.
#:   ``"Italian - Verbs" + " " + "Presente" = "Italian - Verbs Presente"``
TENSE_DISPLAY: dict[str, str] = {
    "presente": "Presente",
    "passato_prossimo": "Passato Prossimo",
    "imperfetto": "Imperfetto",
    "imperativo": "Imperativo",
}

#: Personal pronouns accepted in verb_forms.person. ``Lei`` is the formal-you
#: imperative.
VERB_PERSONS = ("io", "tu", "lui_lei", "noi", "voi", "loro", "Lei")


# ── Avere expression conjugation ────────────────────────────────────────────
AVERE_PERSONS: list[str] = ["io", "tu", "lui_lei", "noi", "voi", "loro"]

AVERE_CONJ: dict[str, str] = {
    "io": "ho", "tu": "hai", "lui_lei": "ha",
    "noi": "abbiamo", "voi": "avete", "loro": "hanno",
}

AVERE_SUBJ_LABEL: dict[str, str] = {
    "io": "io", "tu": "tu", "lui_lei": "lui / lei",
    "noi": "noi", "voi": "voi", "loro": "loro",
}

AVERE_SUBJ_EN: dict[str, str] = {
    "io": "I", "tu": "you", "lui_lei": "he / she",
    "noi": "we", "voi": "you all", "loro": "they",
}


# ── Noun phrase variants ────────────────────────────────────────────────────
#: Phrase families generated alongside the always-on definite phrase. The
#: noun mode picks ONE of these per noun (deterministically by md5 of the
#: lemma) to keep the deck digestible.
NOUN_PHRASE_OPTIONS: list[tuple[str, str]] = [
    ("indefinite", "indefinite"),
    ("articulated_preposition", "a"),
    ("articulated_preposition", "di"),
    ("articulated_preposition", "da"),
    ("articulated_preposition", "in"),
    ("articulated_preposition", "su"),
    ("demonstrative", "questo"),
    ("demonstrative", "quello"),
    ("possessive", "mio"),
    ("possessive", "tuo"),
    ("possessive", "suo"),
    ("possessive", "nostro"),
    ("possessive", "vostro"),
    ("possessive", "loro"),
]
