"""Card presentation shared by every note type: label pills and fact titles.

Labels are generated here from structured data (never by the AI), always in
the same order: type, tense, subject, phrase, preposition, number, then the
source's own pill. The v4 notetype itself (fields, templates, CSS) lives in
:mod:`flashcards.notes`.
"""

from __future__ import annotations

import html
import re

from .grammar import TENSE_DISPLAY

# ── Labels ─────────────────────────────────────────────────────────────────

PERSON_LABEL = {
    "io": "io",
    "tu": "tu",
    "lui_lei": "lui / lei",
    "noi": "noi",
    "voi": "voi",
    "loro": "loro",
    "Lei": "Lei",
}

PHRASE_LABEL = {
    "definite": "definite",
    "indefinite": "indefinite",
    "articulated_preposition": "preposition",
    "demonstrative": "demonstrative",
    "possessive": "possessive",
}


def labels(
    *,
    type: str,
    tense: str | None = None,
    subject: str | None = None,
    phrase: str | None = None,
    preposition: str | None = None,
    number: str | None = None,
    extra: str | None = None,
) -> str:
    """Pipe-separated pills in the canonical order.

    ``type`` may be a bare value ('verb') or a full pill ('type: verb').
    ``extra`` is the source's ``label_pill`` (e.g. 'source: italki').
    """
    type_pill = type if ":" in type else f"type: {type}"
    parts = [type_pill]
    for name, value in (
        ("tense", TENSE_DISPLAY.get(tense, tense) if tense else None),
        ("subject", PERSON_LABEL.get(subject, subject) if subject else None),
        ("phrase", PHRASE_LABEL.get(phrase, phrase) if phrase else None),
        ("preposition", preposition),
        ("number", number),
    ):
        if value:
            parts.append(f"{name}: {value}")
    if extra:
        parts.append(extra)
    return " | ".join(parts)


def labels_html(front_labels: str | None) -> str:
    if not front_labels or not front_labels.strip():
        return ""
    chips: list[str] = []
    for part in front_labels.split("|"):
        part = part.strip()
        label, value = part.split(": ", 1) if ": " in part else (part, part)
        css_class = re.sub(r"[^a-z0-9-]", "-", label.strip().lower())
        chips.append(f'<span class="pill {css_class}">{html.escape(value.strip())}</span>')
    return '<div class="meta-row">' + "".join(chips) + "</div>"


FACT_TITLES = {
    "etymology": "Origin",
    "english_link": "English link",
    "false_friend": "False friend",
    "culture": "Did you know?",
    "usage": "Usage",
}


# ── The v3 notetype (only to find and adopt studied notes made before v4) ───

#: Anki may hold it under a renamed copy ("Italian Card Model+++"), so it is
#: matched by name prefix and exact field list (``anki.pipeline_models``).
LEGACY_MODEL_NAME = "Italian Card Model"
LEGACY_MODEL_FIELDS = [
    "FrontText",
    "FrontLabels",
    "FrontAudio",
    "BackHighlight",
    "BackText",
    "Audio",
    "Image",
    "SortKey",
]
