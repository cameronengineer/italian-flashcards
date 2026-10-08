"""``share`` command — export ready v4 notes as an .apkg to give to someone.

Your own Anki is synced directly (``flashcards apply``); this is only for
sharing. Content from Wiktionary is CC BY-SA, so the deck description
carries the attribution; film example lines are left out of shared decks.
"""

from __future__ import annotations

import json
import re
from contextlib import closing
from pathlib import Path

import genanki

from ..db import connect
from ..kaikki import ATTRIBUTION
from ..notes import CSS, FIELDS, MODEL_NAME, TEMPLATES
from ..paths import DECKS_DIR
from ..reconcile import _media_path
from ..util import md5_hex, print_banner, slugify

SHARE_MODEL_ID = 1944521999
_MEDIA = re.compile(r'<img src="([^"]+)"|\[sound:([^\]]+)\]')


def run(*, decks: list[str] | None = None, out: Path | None = None) -> int:
    print_banner("share — export ready notes as .apkg")
    model = genanki.Model(
        SHARE_MODEL_ID, MODEL_NAME, fields=[{"name": f} for f in FIELDS],
        templates=[{"name": t["Name"], "qfmt": t["Front"], "afmt": t["Back"]} for t in TEMPLATES],
        css=CSS, sort_field_index=0,
    )
    with closing(connect()) as conn:
        sql = "SELECT key, deck, fields_json, tags FROM v4_notes WHERE ready = 1"
        params: list = []
        if decks:
            sql += " AND (" + " OR ".join("deck = ? OR deck LIKE ?" for _ in decks) + ")"
            for d in decks:
                params += [d, d + "::%"]
        rows = conn.execute(sql + " ORDER BY sort_order", params).fetchall()
    if not rows:
        print("  No ready notes match.")
        return 1
    by_deck: dict[str, genanki.Deck] = {}
    media: set[str] = set()
    for r in rows:
        fields = json.loads(r["fields_json"])
        fields["Example"] = ""  # subtitle lines stay private
        for img, snd in _MEDIA.findall(" ".join(fields.values())):
            media.add(img or snd)
        deck = by_deck.setdefault(r["deck"], genanki.Deck(
            int(md5_hex(r["deck"])[:8], 16) % (1 << 30) + (1 << 30), r["deck"],
            description=f"Generated with italian-flashcards. Dictionary content: {ATTRIBUTION}."))
        deck.add_note(genanki.Note(model=model, fields=[fields.get(f, "") for f in FIELDS],
                                   guid=genanki.guid_for(r["key"]), tags=r["tags"].split()))
    DECKS_DIR.mkdir(exist_ok=True)
    out = out or DECKS_DIR / f"{slugify(decks[0] if decks else 'italian')}.apkg"
    pkg = genanki.Package(list(by_deck.values()))
    pkg.media_files = [str(p) for p in (_media_path(m) for m in sorted(media)) if p]
    pkg.write_to_file(str(out))
    print(f"  {len(rows)} notes in {len(by_deck)} deck(s), {len(pkg.media_files)} media files → {out}")
    return 0
