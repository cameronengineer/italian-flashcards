#!/usr/bin/env python3
"""Create Anki decks (.apkg files) from anki_cards with audio and image support."""

from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
from pathlib import Path

import genanki

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "database.sqlite"
DECKS_DIR = PROJECT_ROOT / "decks"

# Media directories
AUDIO_DIR = PROJECT_ROOT / "media" / "audio_compressed"
AUDIO_DIR_FALLBACK = PROJECT_ROOT / "media" / "audio"
IMAGE_DIR = PROJECT_ROOT / "media" / "images_compressed"
IMAGE_DIR_FALLBACK = PROJECT_ROOT / "media" / "images"

# Stable model ID (must be consistent across runs)
MODEL_ID = 1944521879


def deck_filename(deck_name: str) -> str:
    """Derive a filesystem-safe filename from the deck name.

    E.g. 'Italian - Verbs Passato Prossimo' → 'italian_verbs_passato_prossimo'
    """
    slug = deck_name.lower()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    return slug.strip("_")


def deck_id_for(deck_name: str) -> int:
    """Derive a stable deck ID from the deck name via MD5."""
    digest = hashlib.md5(deck_name.encode("utf-8")).hexdigest()
    return (int(digest[:8], 16) % (1 << 30)) + (1 << 30)


def build_model() -> genanki.Model:
    """Build the Anki card model with audio and image support."""
    css = """
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

.pill.noun       { background-color: #d1fae5; color: #065f46; }
.pill.infinitive { background-color: #dbeafe; color: #1e40af; }
.pill.tense      { background-color: #ede9fe; color: #5b21b6; }
.pill.subject    { background-color: #fce7f3; color: #9d174d; }
.pill.phrase     { background-color: #fef3c7; color: #92400e; }
.pill.number     { background-color: #e0f2fe; color: #075985; }
.pill.preposition { background-color: #fef9c3; color: #713f12; }
.pill.type       { background-color: #f3e8ff; color: #6b21a8; }

.front-text {
  font-size: 2em;
  font-weight: 700;
  color: #2c3e50;
  line-height: 1.3;
}

.back-highlight {
  font-size: 2em;
  font-weight: 700;
  color: #e74c3c;
  margin-bottom: 12px;
}

.back-text {
  font-size: 1.2em;
  color: #2c3e50;
  font-style: italic;
  line-height: 1.5;
}

.card-image {
  margin-top: 14px;
}

.card-image img {
  max-height: 540px;
  max-width: 100%;
  width: auto;
  height: auto;
  border-radius: 10px;
}

hr#answer {
  border: 0;
  border-top: 1px solid #ddd;
  margin: 20px 0;
}
"""

    qfmt = """
<div class="card-container">
  {{#Image}}<div class="card-image">{{Image}}</div>{{/Image}}
  {{FrontLabels}}
  <div class="front-text">{{FrontText}}</div>
  {{FrontAudio}}
</div>
"""

    afmt = """
{{FrontSide}}
<hr id="answer">
<div class="card-container">
  {{#BackHighlight}}<div class="back-highlight">{{BackHighlight}}</div>{{/BackHighlight}}
  <div class="back-text">{{BackText}}</div>
  {{Audio}}
</div>
"""

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
        templates=[
            {
                "name": "Italian Card",
                "qfmt": qfmt,
                "afmt": afmt,
            }
        ],
        css=css,
    )


def audio_filename(text: str) -> str:
    """MD5-hash filename for audio."""
    digest = hashlib.md5(text.encode("utf-8")).hexdigest()
    return f"{digest}.mp3"


def image_filename(key: str, ext: str = "jpg") -> str:
    """MD5-hash filename for image."""
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    return f"{digest}.{ext}"


def resolve_audio(text: str) -> tuple[Path, str] | None:
    """Return (path, filename) for audio, preferring compressed version."""
    if not text or not text.strip():
        return None
    
    fname = audio_filename(text)
    compressed = AUDIO_DIR / fname
    if compressed.exists() and compressed.stat().st_size > 0:
        return compressed, fname
    
    original = AUDIO_DIR_FALLBACK / fname
    if original.exists() and original.stat().st_size > 0:
        return original, fname
    
    return None


def resolve_image(key: str) -> tuple[Path, str] | None:
    """Return (path, filename) for image, preferring compressed .jpg."""
    if not key or not key.strip():
        return None
    
    jpg_name = image_filename(key, "jpg")
    jpg_path = IMAGE_DIR / jpg_name
    if jpg_path.exists() and jpg_path.stat().st_size > 0:
        return jpg_path, jpg_name
    
    png_name = image_filename(key, "png")
    png_path = IMAGE_DIR_FALLBACK / png_name
    if png_path.exists() and png_path.stat().st_size > 0:
        return png_path, png_name
    
    return None


def labels_html(front_labels: str) -> str:
    """Convert front_labels to HTML chips."""
    if not front_labels or not front_labels.strip():
        return ""
    
    chips = []
    for part in front_labels.split("|"):
        part = part.strip()
        if ": " in part:
            label, value = part.split(": ", 1)
        else:
            label, value = part, part
        css_class = re.sub(r"[^a-z0-9-]", "-", label.strip().lower())
        chips.append(f'<span class="pill {css_class}">{value.strip()}</span>')
    
    return '<div class="meta-row">' + "".join(chips) + "</div>" if chips else ""


def build_deck(
    deck_name: str, model: genanki.Model, connection: sqlite3.Connection
) -> tuple[genanki.Deck, list[str], int, int, int]:
    """Build a deck from anki_cards in the database with audio and image support.
    
    Returns: (deck, media_files, notes_added, missing_audio, missing_image)
    """
    deck_id = deck_id_for(deck_name)
    deck = genanki.Deck(deck_id, deck_name)
    media_files: list[str] = []
    
    rows = connection.execute(
        """
        SELECT
            id,
            direction,
            front_text,
            front_labels,
            back_highlight,
            back_text,
            audio_text,
            image_text,
            guid
        FROM anki_cards
        WHERE deck = ?
        ORDER BY id
        """,
        (deck_name,),
    ).fetchall()
    
    notes_added = 0
    missing_audio = 0
    missing_image = 0
    
    for row in rows:
        guid = row["guid"]
        direction = row["direction"]
        
        # Resolve audio
        if row["audio_text"]:
            result = resolve_audio(row["audio_text"])
            if result:
                audio_path, fname = result
                audio_field = f"[sound:{fname}]"
                media_files.append(str(audio_path))
            else:
                audio_field = ""
                missing_audio += 1
        else:
            audio_field = ""
        
        # For it_to_en cards, audio plays on the front (when Italian is shown)
        # For en_to_it cards, audio plays on the back (when Italian answer is revealed)
        front_audio_field = audio_field if direction == "it_to_en" else ""
        back_audio_field = audio_field if direction == "en_to_it" else ""
        
        # Resolve image
        if row["image_text"]:
            result = resolve_image(row["image_text"])
            if result:
                img_path, fname = result
                image_field = f'<img src="{fname}">'
                media_files.append(str(img_path))
            else:
                image_field = ""
                missing_image += 1
        else:
            image_field = ""
        
        note = genanki.Note(
            model=model,
            guid=guid,
            fields=[
                row["front_text"],
                labels_html(row["front_labels"]),
                front_audio_field,
                row["back_highlight"],
                row["back_text"] or "",
                back_audio_field,
                image_field,
                str(row["id"]),  # SortKey — hidden field used by reorder script
            ],
        )
        deck.add_note(note)
        notes_added += 1
    
    return deck, media_files, notes_added, missing_audio, missing_image


def print_banner() -> None:
    title = "21 Create Anki decks"
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


def main() -> None:
    print_banner()
    parser = argparse.ArgumentParser(
        description="Create Anki .apkg files from anki_cards with audio and image support."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args()

    DECKS_DIR.mkdir(parents=True, exist_ok=True)
    
    with sqlite3.connect(args.db, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        
        # Get all unique deck names directly from the database
        deck_rows = connection.execute(
            "SELECT DISTINCT deck FROM anki_cards ORDER BY deck"
        ).fetchall()
        
        deck_names = [row["deck"] for row in deck_rows]
    
    model = build_model()
    
    total_notes = 0
    total_missing_audio = 0
    total_missing_image = 0
    
    for deck_name in deck_names:
        output_path = DECKS_DIR / f"{deck_filename(deck_name)}.apkg"
        
        with sqlite3.connect(args.db, timeout=30) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            
            deck, media_files, notes_added, missing_audio, missing_image = build_deck(
                deck_name, model, connection
            )
        
        package = genanki.Package(deck)
        package.media_files = media_files
        package.write_to_file(str(output_path))
        
        total_notes += notes_added
        total_missing_audio += missing_audio
        total_missing_image += missing_image
        
        print(f"  {deck_name:<40} {notes_added:>5} notes"
              f"  ({missing_audio} missing audio)"
              f"  ({missing_image} missing image)"
              f"  → {output_path.name}")
    
    print(f"\nDone."
          f"\n  Total notes written       : {total_notes}"
          f"\n  Total missing audio       : {total_missing_audio}"
          f"\n  Total missing image       : {total_missing_image}"
          f"\n  Output dir                : {DECKS_DIR}")


if __name__ == "__main__":
    main()
