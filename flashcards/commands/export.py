"""``export`` command — write .apkg files from cards + media."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import genanki

from ..db import connect, managed_decks
from ..paths import (
    AUDIO_DIR, AUDIO_DIR_COMPRESSED,
    IMAGE_DIR, IMAGE_DIR_COMPRESSED,
    DECKS_DIR, ensure_dirs,
)
from ..util import audio_filename, image_filename, md5_hex, print_banner, slugify

# Stable model ID — DO NOT CHANGE without forcing a re-import.
MODEL_ID = 1944521879

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
.pill.noun        { background-color: #d1fae5; color: #065f46; }
.pill.infinitive  { background-color: #dbeafe; color: #1e40af; }
.pill.tense       { background-color: #ede9fe; color: #5b21b6; }
.pill.subject     { background-color: #fce7f3; color: #9d174d; }
.pill.phrase      { background-color: #fef3c7; color: #92400e; }
.pill.number      { background-color: #e0f2fe; color: #075985; }
.pill.preposition { background-color: #fef9c3; color: #713f12; }
.pill.type        { background-color: #f3e8ff; color: #6b21a8; }
.pill.source      { background-color: #fee2e2; color: #991b1b; }
.front-text { font-size: 2em; font-weight: 700; color: #2c3e50; line-height: 1.3; }
.back-highlight { font-size: 2em; font-weight: 700; color: #e74c3c; margin-bottom: 12px; }
.back-text { font-size: 1.2em; color: #2c3e50; font-style: italic; line-height: 1.5; }
.card-image { margin-top: 14px; }
.card-image img { max-height: 540px; max-width: 100%; width: auto; height: auto; border-radius: 10px; }
hr#answer { border: 0; border-top: 1px solid #ddd; margin: 20px 0; }
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
  <div class="back-text">{{BackText}}</div>
  {{Audio}}
</div>
"""


def build_model() -> genanki.Model:
    return genanki.Model(
        MODEL_ID,
        "Italian Card Model",
        fields=[
            {"name": "FrontText"},
            {"name": "FrontLabels"},
            {"name": "FrontAudio"},
            {"name": "BackHighlight"},
            {"name": "BackText"},
            {"name": "Audio"},
            {"name": "Image"},
            {"name": "SortKey"},
        ],
        templates=[{"name": "Italian Card", "qfmt": QFMT, "afmt": AFMT}],
        css=CSS,
    )


def deck_id_for(deck_name: str) -> int:
    digest = md5_hex(deck_name)
    return (int(digest[:8], 16) % (1 << 30)) + (1 << 30)


def labels_html(front_labels: str | None) -> str:
    if not front_labels or not front_labels.strip():
        return ""
    chips: list[str] = []
    for part in front_labels.split("|"):
        part = part.strip()
        if ": " in part:
            label, value = part.split(": ", 1)
        else:
            label, value = part, part
        css_class = re.sub(r"[^a-z0-9-]", "-", label.strip().lower())
        chips.append(f'<span class="pill {css_class}">{value.strip()}</span>')
    return '<div class="meta-row">' + "".join(chips) + "</div>"


def resolve_audio(text: str) -> tuple[Path, str] | None:
    if not text or not text.strip():
        return None
    fname = audio_filename(text)
    compressed = AUDIO_DIR_COMPRESSED / fname
    if compressed.exists() and compressed.stat().st_size > 0:
        return compressed, fname
    original = AUDIO_DIR / fname
    if original.exists() and original.stat().st_size > 0:
        return original, fname
    return None


def resolve_image(key: str) -> tuple[Path, str] | None:
    if not key or not key.strip():
        return None
    jpg_name = image_filename(key, "jpg")
    jpg = IMAGE_DIR_COMPRESSED / jpg_name
    if jpg.exists() and jpg.stat().st_size > 0:
        return jpg, jpg_name
    png_name = image_filename(key, "png")
    png = IMAGE_DIR / png_name
    if png.exists() and png.stat().st_size > 0:
        return png, png_name
    return None


def build_deck(deck_name: str, model: genanki.Model, conn: sqlite3.Connection):
    deck = genanki.Deck(deck_id_for(deck_name), deck_name)
    media_files: list[str] = []
    rows = conn.execute(
        """
        SELECT id, direction, front_text, front_labels, back_highlight,
               back_text, audio_text, image_text, guid, sort_order
        FROM cards
        WHERE deck = ?
        ORDER BY sort_order
        """,
        (deck_name,),
    ).fetchall()
    notes = miss_audio = miss_image = 0
    for r in rows:
        audio_field = ""
        if r["audio_text"]:
            got = resolve_audio(r["audio_text"])
            if got:
                p, n = got
                audio_field = f"[sound:{n}]"
                media_files.append(str(p))
            else:
                miss_audio += 1
        front_audio = audio_field if r["direction"] == "it_to_en" else ""
        back_audio = audio_field if r["direction"] == "en_to_it" else ""

        image_field = ""
        if r["image_text"]:
            got = resolve_image(r["image_text"])
            if got:
                p, n = got
                image_field = f'<img src="{n}">'
                media_files.append(str(p))
            else:
                miss_image += 1

        deck.add_note(genanki.Note(
            model=model, guid=r["guid"],
            fields=[
                r["front_text"], labels_html(r["front_labels"]),
                front_audio,
                r["back_highlight"], r["back_text"] or "", back_audio,
                image_field, str(r["sort_order"]),
            ],
        ))
        notes += 1
    return deck, media_files, notes, miss_audio, miss_image


def run() -> dict:
    print_banner("export — write .apkg files")
    ensure_dirs()
    model = build_model()
    total_notes = total_miss_audio = total_miss_image = 0
    summary: dict = {}
    with connect() as conn:
        decks = managed_decks(conn)
        for deck_name in decks:
            out = DECKS_DIR / f"{slugify(deck_name)}.apkg"
            deck, media, notes, ma, mi = build_deck(deck_name, model, conn)
            pkg = genanki.Package(deck)
            pkg.media_files = media
            pkg.write_to_file(str(out))
            total_notes += notes
            total_miss_audio += ma
            total_miss_image += mi
            summary[deck_name] = {
                "notes": notes, "miss_audio": ma, "miss_image": mi, "file": out.name,
            }
            print(
                f"  {deck_name:<48} {notes:>5} notes "
                f"({ma} missing audio, {mi} missing image)  → {out.name}"
            )
    print(
        f"\nDone. total_notes={total_notes} "
        f"missing_audio={total_miss_audio} missing_image={total_miss_image}"
    )
    return summary
