"""How a card looks — the single source of truth for card presentation.

Every mode describes its cards as :class:`Card` objects; nothing else
decides labels, the details line, the fun-fact block or the note template.
That keeps every deck visually identical:

* **Front:** image (if any) → label pills → prompt → audio (Italian side only).
* **Back:** the answer → a muted *details* line (dictionary form, grammar)
  → an optional *did you know?* fact → audio.

Labels are generated here from structured data (never by the AI), always
in the same order: type, tense, subject, phrase, preposition, number, then
the source's own pill.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, replace

from .grammar import TENSE_DISPLAY

# ── Card spec ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Card:
    """One logical card; it becomes two notes (en→it and it→en)."""

    entry_id: str
    natural_key: str
    deck: str
    english: str
    italian: str
    labels: str
    details: str | None = None
    audio_text: str | None = None
    image_text: str | None = None
    #: Word whose fun fact (``word_facts``) appears on the back, if any.
    fact_word: str | None = None

    def without_media(self, *, audio: bool, image: bool) -> "Card":
        return replace(
            self,
            audio_text=self.audio_text if audio else None,
            image_text=self.image_text if image else None,
        )


# ── Labels ─────────────────────────────────────────────────────────────────

PERSON_LABEL = {
    "io": "io", "tu": "tu", "lui_lei": "lui / lei", "noi": "noi",
    "voi": "voi", "loro": "loro", "Lei": "Lei",
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


# ── Text ───────────────────────────────────────────────────────────────────


def merge_english(item: dict) -> str:
    """English for a card from an enrichment answer: ``english (disambiguation) [usage]``."""
    english = (item.get("english") or "").strip()
    if not english:
        return ""
    dis = (item.get("disambiguation") or "").strip()
    if dis:
        english = f"{english} ({dis})"
    note = (item.get("usage_note") or "").strip()
    if note:
        english = f"{english} [{note}]"
    return english


def with_article(article: str | None, word: str | None) -> str:
    """'la' + 'casa' → 'la casa'; "l'" + 'amico' → "l'amico"."""
    word = (word or "").strip()
    article = (article or "").strip()
    if not article:
        return word
    return f"{article}{word}" if article.endswith("'") else f"{article} {word}"


def join_details(*parts: str | None) -> str | None:
    text = " · ".join(p for p in parts if p)
    return text or None


FACT_TITLES = {
    "etymology": "Origin",
    "english_link": "English link",
    "false_friend": "False friend",
    "culture": "Did you know?",
    "usage": "Usage",
}


def back_html(details: str | None, fact: str | None, fact_kind: str | None) -> str:
    """The BackText field: details line + optional fact block (escaped)."""
    parts = []
    if details:
        parts.append(f'<div class="details">{html.escape(details)}</div>')
    if fact:
        title = FACT_TITLES.get(fact_kind or "", "Did you know?")
        parts.append(
            f'<div class="fact"><span class="fact-title">{title}</span>'
            f"{html.escape(fact)}</div>"
        )
    return "".join(parts)


# ── Note model ─────────────────────────────────────────────────────────────

#: Stable model id + name — DO NOT CHANGE without forcing a re-import. Anki
#: may hold the model under a renamed copy ("Italian Card Model+++"); sync
#: finds it by its fields (``anki.pipeline_note_query``).
MODEL_ID = 1944521879
MODEL_NAME = "Italian Card Model"
TEMPLATE_NAME = "Italian Card"
MODEL_FIELDS = [
    "FrontText", "FrontLabels", "FrontAudio", "BackHighlight",
    "BackText", "Audio", "Image", "SortKey",
]

CSS = """
.card {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  font-size: 18px;
  text-align: center;
  color: #333;
  background-color: #f4f4f9;
  padding: 10px;
}
.card-container {
  background-color: white;
  border-radius: 15px;
  padding: 20px;
  box-shadow: 0 2px 5px rgba(0,0,0,0.1);
  max-width: 90%;
  margin: 0 auto;
}
.meta-row {
  display: flex;
  justify-content: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 16px;
}
.pill {
  display: inline-block;
  padding: 4px 12px;
  border-radius: 999px;
  font-size: 0.75em;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.pill.type        { background-color: #f3e8ff; color: #6b21a8; }
.pill.tense       { background-color: #ede9fe; color: #5b21b6; }
.pill.subject     { background-color: #fce7f3; color: #9d174d; }
.pill.phrase      { background-color: #fef3c7; color: #92400e; }
.pill.preposition { background-color: #fef9c3; color: #713f12; }
.pill.number      { background-color: #e0f2fe; color: #075985; }
.pill.source      { background-color: #fee2e2; color: #991b1b; }
.pill.noun        { background-color: #d1fae5; color: #065f46; }
.pill.infinitive  { background-color: #dbeafe; color: #1e40af; }
.front-text { font-size: 2em; font-weight: 700; color: #2c3e50; line-height: 1.3; }
.back-highlight { font-size: 2em; font-weight: 700; color: #e74c3c; margin-bottom: 12px; }
.back-text .details { font-size: 1.1em; color: #6b7280; font-style: italic; line-height: 1.5; }
.back-text .fact {
  margin: 16px auto 0;
  max-width: 34em;
  padding: 10px 14px;
  border-left: 4px solid #f59e0b;
  border-radius: 8px;
  background-color: #fffbeb;
  color: #78350f;
  font-size: 0.85em;
  line-height: 1.5;
  text-align: left;
}
.back-text .fact-title {
  display: block;
  font-size: 0.75em;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: #b45309;
  margin-bottom: 2px;
}
.card-image { margin-top: 14px; }
.card-image img { max-height: 540px; max-width: 100%; width: auto; height: auto; border-radius: 10px; }
hr#answer { border: 0; border-top: 1px solid #ddd; margin: 20px 0; }
.nightMode .card, .night_mode .card { background-color: #1f2937; color: #e5e7eb; }
.nightMode .card-container, .night_mode .card-container { background-color: #111827; }
.nightMode .front-text, .night_mode .front-text { color: #f3f4f6; }
.nightMode .back-text .details, .night_mode .back-text .details { color: #9ca3af; }
.nightMode .back-text .fact, .night_mode .back-text .fact { background-color: #3b2a12; color: #fde68a; }
.nightMode .back-text .fact-title, .night_mode .back-text .fact-title { color: #fbbf24; }
"""

QFMT = """
<div class="card-container">
  {{#Image}}<div class="card-image">{{Image}}</div>{{/Image}}
  {{FrontLabels}}
  <div class="front-text">{{FrontText}}</div>
  {{FrontAudio}}
</div>
"""

AFMT = """
{{FrontSide}}
<hr id="answer">
<div class="card-container">
  {{#BackHighlight}}<div class="back-highlight">{{BackHighlight}}</div>{{/BackHighlight}}
  {{#BackText}}<div class="back-text">{{BackText}}</div>{{/BackText}}
  {{Audio}}
</div>
"""
