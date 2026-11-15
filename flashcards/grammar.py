"""Italian tense names: the identifiers used in plan.toml and note keys, and
how each is shown on a card and in a deck name."""

from __future__ import annotations

# ── Verb tenses ─────────────────────────────────────────────────────────────
TENSES = (
    "presente",
    "presente_progressivo",
    "passato_prossimo",
    "imperfetto",
    "futuro_semplice",
    "imperativo",
    "condizionale_presente",
    "condizionale_passato",
)

#: ``presente_progressivo`` is the Italian present continuous (``stare`` in the
#: present + gerundio, e.g. ``sto parlando``).
#: ``futuro_semplice`` is the simple future (e.g. ``parlerò``).
#: ``condizionale_passato`` is the Italian conditional perfect (auxiliary in
#: the present conditional + past participle, e.g. ``avrei parlato``).
TENSE_DISPLAY: dict[str, str] = {
    "presente": "Presente",
    "presente_progressivo": "Presente Progressivo",
    "passato_prossimo": "Passato Prossimo",
    "imperfetto": "Imperfetto",
    "futuro_semplice": "Futuro Semplice",
    "imperativo": "Imperativo",
    "condizionale_presente": "Condizionale Presente",
    "condizionale_passato": "Condizionale Passato",
}
