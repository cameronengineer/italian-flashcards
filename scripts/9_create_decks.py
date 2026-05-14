#!/usr/bin/env python3
"""Create Anki decks (.apkg files) from anki_cards."""

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

# Stable model ID (must be consistent across runs)
MODEL_ID = 1944521879

DECK_NAMES: dict[str, str] = {
    "nouns": "Italian - Nouns",
    "verbs_infinito": "Italian - Verbs Infinitive",
    "verbs_presente": "Italian - Verbs Presente",
    "verbs_passatoprossimo": "Italian - Verbs Passato Prossimo",
    "verbs_imperfetto": "Italian - Verbs Imperfetto",
    "verbs_imperativo": "Italian - Verbs Imperativo",
}


def deck_id_for(deck_key: str) -> int:
    """Derive a stable deck ID from the deck key via MD5."""
    digest = hashlib.md5(deck_key.encode("utf-8")).hexdigest()
    return (int(digest[:8], 16) % (1 << 30)) + (1 << 30)


def build_model() -> genanki.Model:
    """Build the Anki card model."""
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

hr#answer {
  border: 0;
  border-top: 1px solid #ddd;
  margin: 20px 0;
}
"""

    qfmt = """
<div class="card-container">
  {{FrontLabels}}
  <div class="front-text">{{FrontText}}</div>
</div>
"""

    afmt = """
{{FrontSide}}
<hr id="answer">
<div class="card-container">
  {{#BackHighlight}}<div class="back-highlight">{{BackHighlight}}</div>{{/BackHighlight}}
  {{#BackText}}<div class="back-text">{{BackText}}</div>{{/BackText}}
</div>
"""

    return genanki.Model(
        MODEL_ID,
        "Italian Card Model",
        fields=[
            {"name": "FrontText"},
            {"name": "FrontLabels"},
            {"name": "BackHighlight"},
            {"name": "BackText"},
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
    deck_key: str, deck_name: str, model: genanki.Model, connection: sqlite3.Connection
) -> tuple[genanki.Deck, int]:
    """Build a deck from anki_cards in the database."""
    deck_id = deck_id_for(deck_key)
    deck = genanki.Deck(deck_id, deck_name)
    
    rows = connection.execute(
        """
        SELECT
            front_text,
            front_labels,
            back_highlight,
            back_text,
            guid
        FROM anki_cards
        WHERE deck = ?
        ORDER BY id
        """,
        (deck_key,),
    ).fetchall()
    
    notes_added = 0
    for row in rows:
        guid = row["guid"]
        
        note = genanki.Note(
            model=model,
            guid=guid,
            fields=[
                row["front_text"],
                labels_html(row["front_labels"]),
                row["back_highlight"],
                row["back_text"] or "",
            ],
        )
        deck.add_note(note)
        notes_added += 1
    
    return deck, notes_added


def print_banner() -> None:
    title = "9 Create Anki decks"
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


def main() -> None:
    print_banner()
    parser = argparse.ArgumentParser(
        description="Create Anki .apkg files from anki_cards."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args()

    DECKS_DIR.mkdir(parents=True, exist_ok=True)
    
    with sqlite3.connect(args.db, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        
        # Get all unique decks
        deck_rows = connection.execute(
            "SELECT DISTINCT deck FROM anki_cards ORDER BY deck"
        ).fetchall()
        
        deck_keys = [row["deck"] for row in deck_rows]
    
    model = build_model()
    
    for deck_key in deck_keys:
        deck_name = DECK_NAMES.get(deck_key, deck_key.replace("_", " ").title())
        output_path = DECKS_DIR / f"{deck_key}.apkg"
        
        with sqlite3.connect(args.db, timeout=30) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            
            deck, notes_added = build_deck(deck_key, deck_name, model, connection)
        
        package = genanki.Package(deck)
        package.write_to_file(str(output_path))
        
        print(f"  {deck_name:<40} {notes_added:>5} notes → {output_path.name}")
    
    print(f"\nDone. Decks saved to {DECKS_DIR}")


if __name__ == "__main__":
    main()
