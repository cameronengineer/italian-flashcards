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


# ── Your own deck files (written by every run) ─────────────────────────────


def export_decks(conn) -> dict:
    """``decks/<deck>.apkg`` for every deck with ready notes — a portable copy
    of everything the run publishes (film lines included). A deck's file is
    rebuilt only when its notes change. Returns {"written": [...], "unchanged": n,
    "missing_media": n}.

    These are exports, not how your own Anki is updated (that happens directly
    through AnkiConnect); importing them into the same Anki profile would
    duplicate notes.
    """
    require_current_notes(conn)
    model = genanki.Model(
        SHARE_MODEL_ID,
        MODEL_NAME,
        fields=[{"name": f} for f in FIELDS],
        templates=[{"name": t["Name"], "qfmt": t["Front"], "afmt": t["Back"]} for t in TEMPLATES],
        css=CSS,
        sort_field_index=0,
    )
    by_deck: dict[str, list] = {}
    for row in conn.execute("SELECT * FROM v4_notes WHERE ready = 1 ORDER BY sort_order"):
        if not reasons(conn, row["key"], row["lexeme_id"], json.loads(row["fields_json"])):
            by_deck.setdefault(row["deck"], []).append(row)
    DECKS_DIR.mkdir(exist_ok=True)
    manifest_path = DECKS_DIR / ".exports.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = {}
    if not isinstance(manifest, dict):
        manifest = {}
    result = {"written": [], "unchanged": 0, "missing_media": 0, "archived": 0}
    current = {}
    names = {name: slugify(name) for name in by_deck}
    for slug in set(names.values()):
        matches = [name for name, value in names.items() if value == slug]
        if len(matches) > 1:
            for name in matches:
                names[name] += "-" + md5_hex(name)[:8]
    for deck_name, rows in sorted(by_deck.items()):
        out = DECKS_DIR / f"{names[deck_name]}.apkg"
        fields_of = {r["key"]: json.loads(r["fields_json"]) for r in rows}
        media = {img or snd for f in fields_of.values() for img, snd in _MEDIA.findall(" ".join(f.values()))}
        found = {m: locate(m) for m in sorted(media)}
        missing = [m for m, p in found.items() if p is None]
        result["missing_media"] += len(missing)
        # Never replace a complete package with one containing broken media.
        if missing:
            if deck_name in manifest:
                current[deck_name] = manifest[deck_name]
            continue
        signature = md5_hex(
            json.dumps(
                {
                    "format": 2,
                    "model": [MODEL_NAME, SHARE_MODEL_ID, FIELDS, TEMPLATES, CSS],
                    "notes": [(r["key"], r["fields_json"], r["tags"], r["sort_order"]) for r in rows],
                    "media": {m: [p.stat().st_size, p.stat().st_mtime_ns] for m, p in found.items()},
                },
                sort_keys=True,
            )
        )
        previous = manifest.get(deck_name, {})
        output_state = [out.stat().st_size, out.stat().st_mtime_ns] if out.exists() else None
        if (
            isinstance(previous, dict)
            and previous.get("signature") == signature
            and previous.get("file_state") == output_state
            and output_state
        ):
            result["unchanged"] += 1
            current[deck_name] = previous
            continue
        deck = genanki.Deck(
            int(md5_hex(deck_name)[:8], 16) % (1 << 30) + (1 << 30),
            deck_name,
            description=f"Generated with italian-flashcards. Dictionary content: {ATTRIBUTION}.",
        )
        for r in rows:
            fields = fields_of[r["key"]]
            note = genanki.Note(
                model=model,
                fields=[fields.get(f, "") for f in FIELDS],
                guid=genanki.guid_for(r["key"]),
                tags=r["tags"].split(),
                due=r["sort_order"],
            )
            if "fc::dormant" in note.tags:
                for card in note.cards:
                    card.suspend = True
            deck.add_note(note)
        pkg = genanki.Package([deck])
        pkg.media_files = [str(p) for p in found.values()]
        tmp = out.with_suffix(".apkg.tmp")
        pkg.write_to_file(str(tmp))
        tmp.replace(out)
        current[deck_name] = {
            "filename": out.name,
            "signature": signature,
            "file_state": [out.stat().st_size, out.stat().st_mtime_ns],
        }
        result["written"].append(out)
    # Archive obsolete generated packages, preserving all their embedded media.
    for name, previous in manifest.items():
        if name in by_deck:
            continue
        filename = (
            previous.get("filename", f"{slugify(name)}.apkg")
            if isinstance(previous, dict)
            else f"{slugify(name)}.apkg"
        )
        if Path(filename).name != filename or not filename.endswith(".apkg"):
            continue
        old = DECKS_DIR / filename
        if old.exists():
            archive = DECKS_DIR / "archive"
            archive.mkdir(exist_ok=True)
            old.replace(archive / f"{old.stem}-{uuid4().hex[:8]}.apkg")
            result["archived"] += 1
    atomic_json(manifest_path, current)
    return result
