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
from datetime import datetime
from uuid import uuid4
from ..runtime import atomic_json
from ..util import sha256_file
from ..quality import reasons, require_current_notes
from ..assets import locate

import genanki

from ..db import connect
from ..kaikki import ATTRIBUTION
from ..notes import CSS, FIELDS, MODEL_NAME, TEMPLATES
from ..paths import DECKS_DIR
from ..util import md5_hex, print_banner, slugify

SHARE_MODEL_ID = 1944521999
_MEDIA = re.compile(r'<img src="([^"]+)"|\[sound:([^\]]+)\]')


def run(*, decks: list[str] | None = None, out: Path | None = None, include_private: bool = False) -> int:
    print_banner("share — export ready notes as .apkg")
    model = genanki.Model(
        SHARE_MODEL_ID,
        MODEL_NAME,
        fields=[{"name": f} for f in FIELDS],
        templates=[{"name": t["Name"], "qfmt": t["Front"], "afmt": t["Back"]} for t in TEMPLATES],
        css=CSS,
        sort_field_index=0,
    )
    with closing(connect()) as conn:
        require_current_notes(conn)
        sql = "SELECT * FROM v4_notes WHERE ready = 1"
        params: list = []
        if decks:
            sql += " AND (" + " OR ".join("deck = ? OR deck LIKE ?" for _ in decks) + ")"
            for d in decks:
                params += [d, d + "::%"]
        rows = conn.execute(sql + " ORDER BY sort_order", params).fetchall()
        allowed = []
        for row in rows:
            if reasons(conn, row["key"], row["lexeme_id"], json.loads(row["fields_json"])):
                continue
            lineage = [
                json.loads(r[0] or "{}")
                for r in conn.execute(
                    "SELECT l.settings FROM lists l JOIN list_items li ON li.list_id=l.id WHERE li.lexeme_id=?",
                    (row["lexeme_id"],),
                )
            ]
            if not include_private and (not lineage or any(not l.get("shareable", False) for l in lineage)):
                continue
            allowed.append(row)
        rows = allowed
        generation = conn.execute("SELECT value FROM metadata WHERE key='source_generation'").fetchone()
        generation = generation[0] if generation else "unknown"
        dictionary = conn.execute("SELECT value FROM metadata WHERE key='dictionary_version'").fetchone()
        dictionary = dictionary[0] if dictionary else "unknown"

    if not rows:
        print(
            "  No publishable, shareable notes match. Set shareable=true only for sources you can share, or explicitly use --include-private."
        )
        return 1
    by_deck: dict[str, genanki.Deck] = {}
    media: set[str] = set()
    for r in rows:
        fields = json.loads(r["fields_json"])
        fields["Example"] = ""  # subtitle lines stay private
        for img, snd in _MEDIA.findall(" ".join(fields.values())):
            media.add(img or snd)
        deck = by_deck.setdefault(
            r["deck"],
            genanki.Deck(
                int(md5_hex(r["deck"])[:8], 16) % (1 << 30) + (1 << 30),
                r["deck"],
                description=f"Generated with italian-flashcards. Dictionary content: {ATTRIBUTION}.",
            ),
        )
        deck.add_note(
            genanki.Note(
                model=model,
                fields=[fields.get(f, "") for f in FIELDS],
                guid=genanki.guid_for(r["key"]),
                tags=r["tags"].split(),
            )
        )
    DECKS_DIR.mkdir(exist_ok=True)
    out = (
        out
        or DECKS_DIR
        / f"{slugify(decks[0] if decks else 'italian')}_v4_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:6]}.apkg"
    )
    if out.exists():
        raise ValueError(f"Export already exists; choose a new filename: {out}")
    pkg = genanki.Package(list(by_deck.values()))
    resolved = {m: locate(m) for m in sorted(media)}
    missing = [m for m, p in resolved.items() if p is None]
    if missing:
        raise ValueError(f"Missing or invalid export media: {missing}")
    pkg.media_files = [str(p) for p in resolved.values()]
    pkg.write_to_file(str(out))
    atomic_json(
        out.with_suffix(".manifest.json"),
        {
            "format": 1,
            "pipeline": "v4",
            "source_generation": generation,
            "dictionary_version": dictionary,
            "private_included": include_private,
            "notes": len(rows),
            "keys": [r["key"] for r in rows],
            "media": {m: sha256_file(p) for m, p in resolved.items()},
            "package_sha256": sha256_file(out),
            "attribution": ATTRIBUTION,
        },
    )
    print(f"  {len(rows)} notes in {len(by_deck)} deck(s), {len(pkg.media_files)} media files → {out}")
    return 0
