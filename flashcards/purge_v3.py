"""``purge-v3`` — a one-off: remove the notes of the old pipeline from Anki.

Before the word-first lexicon, the pipeline made one note per card direction
on the notetype "Italian Card Model". Their cards have since been recreated on
the current notetype; this deletes what is left of the old notes, studied or
not, so each card exists once.

Only notes that are provably the pipeline's are touched: on the old notetype
(matched by name prefix and exact field list), carrying a ``SortKey`` the
pipeline issued (``v3_note_keys``, saved when the old tables were dropped), and
with every card in one of the pipeline's decks. Nothing is deleted without
``--apply``; the whole Anki collection is backed up first. No media is removed.

When no such notes are left, the saved keys are dropped and this module can
be deleted.
"""

from __future__ import annotations

import json
from contextlib import closing

from . import backup
from .anki import chunks, invoke, quote
from .db import connect
from .ownership import deck_owned
from .settings import settings

MODEL_NAME = "Italian Card Model"
MODEL_FIELDS = [
    "FrontText",
    "FrontLabels",
    "FrontAudio",
    "BackHighlight",
    "BackText",
    "Audio",
    "Image",
    "SortKey",
]


def models() -> list[str]:
    """The old notetype, whatever Anki named its copies ("Italian Card Model+++")."""
    return [
        m
        for m in invoke("modelNames")
        if m.startswith(MODEL_NAME) and invoke("modelFieldNames", modelName=m) == MODEL_FIELDS
    ]


def find(conn) -> list[dict]:
    """The old notes still in Anki: ``{"id", "cards", "studied"}`` each."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='v3_note_keys'").fetchone():
        return []
    keys = {r[0] for r in conn.execute("SELECT sort_key FROM v3_note_keys")}
    row = conn.execute("SELECT value FROM metadata WHERE key='v3_decks'").fetchone()
    decks = set(json.loads(row[0])) if row else set()
    names = models()
    if not keys or not names:
        return []
    query = (
        "("
        + " OR ".join(quote(f"note:{m}") for m in names)
        + ") ("
        + " OR ".join(quote(f"deck:{d}") for d in sorted(decks) + ["Italian::*"])
        + ")"
    )
    found = []
    for group in chunks(invoke("findNotes", query=query), 500):
        notes = [
            n
            for n in invoke("notesInfo", notes=group)
            if n and n.get("fields", {}).get("SortKey", {}).get("value", "").strip() in keys
        ]
        cards = [c for n in notes for c in n.get("cards", [])]
        deck_of = {c: d for d, cs in (invoke("getDecks", cards=cards) if cards else {}).items() for c in cs}
        info = {}
        for part in chunks(cards, 500):
            info.update({c["cardId"]: c for c in invoke("cardsInfo", cards=part) if c})
        for n in notes:
            cs = n.get("cards", [])
            if not cs or not all(deck_owned(deck_of.get(c, "")) or deck_of.get(c) in decks for c in cs):
                continue  # moved by you out of the pipeline's decks: never touched
            studied = any(
                info.get(c, {}).get("type", 0) != 0 or info.get(c, {}).get("reps", 0) > 0 for c in cs
            )
            found.append({"id": n["noteId"], "cards": cs, "studied": studied})
    return found


def run(*, apply: bool = False) -> int:
    profile = invoke("getActiveProfile")
    with closing(connect()) as conn:
        stored = conn.execute("SELECT value FROM metadata WHERE key='anki_profile'").fetchone()
        expected = settings.anki.profile or (stored[0] if stored else "")
        if expected and expected != profile:
            raise RuntimeError(f"Expected Anki profile {expected!r}, got {profile!r}; nothing was changed")
        notes = find(conn)
        studied = sum(n["studied"] for n in notes)
        print(
            f"  {len(notes):,} old notes in Anki ({studied:,} with study history, {len(notes) - studied:,} never studied)"
        )
        if not notes:
            conn.execute("DROP TABLE IF EXISTS v3_note_keys")
            conn.execute("DELETE FROM metadata WHERE key='v3_decks'")
            conn.commit()
            print("  Nothing left to remove.")
            return 0
        if not apply:
            print(
                "  Nothing was changed. Run a normal sync first, then `./run.sh purge-v3 --apply` to delete them."
            )
            return 0
        saved = backup.anki_snapshot(profile)
        if saved:
            print(f"  Anki backed up → backups/anki/{saved.name}")
        deleted = 0
        for group in chunks([n["id"] for n in notes], 500):
            invoke("deleteNotes", notes=group, timeout=300)
            deleted += len(group)
        left = find(conn)
        print(f"  Deleted {deleted:,} old notes; {len(left):,} left. No media was removed.")
        if not left:
            conn.execute("DROP TABLE IF EXISTS v3_note_keys")
            conn.execute("DELETE FROM metadata WHERE key='v3_decks'")
            conn.commit()
        return 0 if not left else 2
