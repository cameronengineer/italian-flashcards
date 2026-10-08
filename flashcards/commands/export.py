"""``export`` command — write one .apkg per deck from the cards table.

Presentation (fields, templates, CSS, labels, back text) comes from
:mod:`flashcards.cards`, so every deck renders identically.
"""

from __future__ import annotations

import html
import sqlite3
from contextlib import closing
from pathlib import Path

import genanki

from ..cards import AFMT, CSS, MODEL_FIELDS, MODEL_ID, MODEL_NAME, QFMT, TEMPLATE_NAME, back_html, labels_html
from ..db import connect, managed_decks
from ..paths import (
    AUDIO_DIR, AUDIO_DIR_COMPRESSED,
    IMAGE_DIR, IMAGE_DIR_COMPRESSED,
    DECKS_DIR, ensure_dirs,
)
from ..util import audio_filename, image_filename, md5_hex, note_key, print_banner, slugify


def build_model() -> genanki.Model:
    return genanki.Model(
        MODEL_ID,
        MODEL_NAME,
        fields=[{"name": f} for f in MODEL_FIELDS],
        templates=[{"name": TEMPLATE_NAME, "qfmt": QFMT, "afmt": AFMT}],
        css=CSS,
    )


def deck_file(deck_name: str) -> Path:
    return DECKS_DIR / f"{slugify(deck_name)}.apkg"


def deck_id_for(deck_name: str) -> int:
    digest = md5_hex(deck_name)
    return (int(digest[:8], 16) % (1 << 30)) + (1 << 30)


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
        SELECT id, natural_key, direction, front_text, front_labels, back_highlight,
               back_text, audio_text, image_text, fact, fact_kind, guid, sort_order
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
                html.escape(r["front_text"]), labels_html(r["front_labels"]),
                front_audio,
                html.escape(r["back_highlight"]),
                back_html(r["back_text"], r["fact"], r["fact_kind"]),
                back_audio,
                # SortKey carries the note's stable identity (not its
                # position): sync uses it to find orphans and looks the
                # position up in the DB. Keeping it stable also means
                # unchanged notes aren't rewritten on every import.
                image_field, note_key(r["natural_key"], r["direction"]),
            ],
        ))
        notes += 1
    # Shared media (one image per verb across ~46 form cards) would otherwise
    # be zipped into the package once per note.
    media_files = list(dict.fromkeys(media_files))
    return deck, media_files, notes, miss_audio, miss_image


def run() -> dict:
    print_banner("export — write .apkg files")
    ensure_dirs()
    model = build_model()
    total_notes = total_miss_audio = total_miss_image = 0
    summary: dict = {}
    with closing(connect()) as conn:
        decks = managed_decks(conn)
        for deck_name in decks:
            out = deck_file(deck_name)
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
