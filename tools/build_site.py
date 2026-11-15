"""Build the static website in ``site/`` from your finished decks.

Not part of the pipeline: ``./run.sh`` never calls this. Run it by hand
(``python3 tools/build_site.py``, standard library only) after the decks in
``decks/`` have changed, then commit ``site/`` and push; Render serves it.

Writes public copies of the decks to ``site/downloads/`` and ``site/index.html``
(from ``tools/index.template.html``, with real counts) and ``site/samples/``.
Each copy is the original package with

* the ``Tutto bene`` tag removed (that list is gone and never published),
* ``Dormant`` verb decks renamed ``More tenses`` and un-suspended.

Film decks and film example lines are published on purpose; the ``Tutto bene``
deck never is.
"""

from __future__ import annotations

import datetime
import html as _html
import json
import re
import sqlite3
import hashlib
import unicodedata
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DECKS_DIR = ROOT / "decks"
SITE_DIR = ROOT / "site"
TOOLS_DIR = ROOT / "tools"
# The textbook list is purged: its deck is never published and its tag never ships.
PRIVATE_DECKS = {"italian_core_tutto_bene"}
PRIVATE_TAGS = {"list::tutto-bene"}

# Real cards shown on the page: (deck file stem, Italian field).
SAMPLES = [
    ("italian_cils_a1", "il caffè"),
    ("italian_cils_a1", "la farmacia"),
    ("italian_cils_a1", "il pane"),
    ("italian_core_fluent_forever_625", "il tifoso"),
    ("italian_core_fluent_forever_625", "il cane"),
    ("italian_core_fluent_forever_625", "il sole"),
    ("italian_core_fluent_forever_625", "la strada"),
    ("italian_core_fluent_forever_625", "il mare"),
    ("italian_verbs_learning_presente", "scendi"),
    ("italian_cognates", "generoso"),
    ("italian_noun_phrases", "la mia piscina"),
]
_FIELDS = ["Italian", "English", "Hint", "Labels", "Details", "Forms", "IPA", "Example", "Note", "Fact",
           "FactTitle", "Image", "Audio", "Recognition", "Production"]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sanitise(src: Path, dst: Path) -> dict:
    """Write a public copy of ``src`` to ``dst``; return its counts."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        with zipfile.ZipFile(src) as z:
            z.extractall(tmp)
        db = sqlite3.connect(tmp / "collection.anki2")
        rows = db.execute("SELECT id, flds, tags FROM notes").fetchall()
        for nid, flds, tags in rows:
            kept = " ".join(t for t in tags.split() if t not in PRIVATE_TAGS)
            db.execute("UPDATE notes SET tags=? WHERE id=?", (f" {kept} ", nid))
        db.execute("UPDATE cards SET queue=0 WHERE queue=-1")
        decks = json.loads(db.execute("SELECT decks FROM col").fetchone()[0])
        for d in decks.values():
            d["name"] = d["name"].replace("::Dormant::", "::More tenses::")
        db.execute("UPDATE col SET decks=?", (json.dumps(decks),))
        counts = {
            "notes": db.execute("SELECT count(*) FROM notes").fetchone()[0],
            "cards": db.execute("SELECT count(*) FROM cards").fetchone()[0],
            "tutto_left": db.execute("SELECT count(*) FROM notes WHERE tags LIKE '%tutto%'").fetchone()[0],
        }
        db.commit()
        db.close()
        # Re-zip: Anki wants the collection and media map first, media files numbered.
        dst.parent.mkdir(parents=True, exist_ok=True)
        part = dst.with_suffix(".apkg.tmp")
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as out:
            for f in sorted(tmp.iterdir(), key=lambda p: (p.name not in ("collection.anki2", "media"), p.name)):
                out.write(f, f.name, compress_type=zipfile.ZIP_STORED if f.name.isdigit() else zipfile.ZIP_DEFLATED)
        part.replace(dst)
    return counts


def _samples(public: dict[str, Path]) -> list[dict]:
    """Copy the sample cards' fields, picture and recording into site/samples/."""
    import html

    out_dir = SITE_DIR / "samples"
    out_dir.mkdir(exist_ok=True)
    found = []
    for stem, italian in SAMPLES:
        with zipfile.ZipFile(public[stem]) as z, tempfile.TemporaryDirectory() as tmp:
            media = {v: k for k, v in json.loads(z.read("media")).items()}
            z.extract("collection.anki2", tmp)
            db = sqlite3.connect(Path(tmp) / "collection.anki2")
            for (flds,) in db.execute("SELECT flds FROM notes"):
                row = dict(zip(_FIELDS, flds.split("\x1f")))
                if html.unescape(row["Italian"]) != italian:
                    continue
                ascii_name = unicodedata.normalize("NFKD", html.unescape(italian)).encode("ascii", "ignore").decode()
                slug = re.sub(r"\W+", "-", ascii_name).strip("-")
                img = re.search(r'src="([^"]+)"', row["Image"]).group(1)
                snd = re.search(r"\[sound:([^\]]+)\]", row["Audio"]).group(1)
                (out_dir / f"{slug}.jpg").write_bytes(z.read(media[img]))
                (out_dir / f"{slug}.mp3").write_bytes(z.read(media[snd]))
                ex = re.search(r"<i>(.*?)</i><br><span class=\"literal\">(.*?)</span><br><span class=\"src\">(.*?)</span>", row["Example"])
                pills = re.findall(r'class="pill [^"]*">([^<]+)<', row["Labels"])
                found.append(
                    {
                        "slug": slug,
                        "deck": stem,
                        "recognition": row["Recognition"] == "1",
                        "production": row["Production"] == "1",
                        "pills": pills,
                        "example": [html.unescape(re.sub(r"<[^>]+>", "", x)) for x in ex.groups()] if ex else [],
                        **{k.lower(): html.unescape(row[k]) for k in
                           ("Italian", "English", "Hint", "Details", "Forms", "IPA", "Note", "Fact", "FactTitle")},
                    }
                )
                break
            else:
                raise SystemExit(f"sample not found: {italian!r} in {stem}")
            db.close()
    return found


# Where the .apkg files are hosted (GitHub release assets — too big for git).
REPO = "cameronengineer/italian-flashcards"
RELEASE_TAG = "decks"
DOWNLOAD_BASE = f"https://github.com/{REPO}/releases/download/{RELEASE_TAG}/"
RENDER_SERVICE = "parola-italian-flashcards"

# (title, blurb, [(deck file stem, name, one-line description)])
GROUPS = [
    (
        "start",
        "Start here",
        "No Italian yet? Begin with these two.",
        [
            ("italian_core_fluent_forever_625", "Fluent Forever 625", "The everyday basics: family, food, home, body, time. The best first deck."),
            ("italian_basics_numbers", "Numbers", "The ones that trip everyone up (13 vs 30, 60 vs 70), from 0 up to a million."),
        ],
    ),
    (
        "levels",
        "Words by level",
        "Based on the CILS exam levels. Each level already includes the words from the levels below it, so you only need one.",
        [
            ("italian_cils_a1", "Level A1 · Beginner", "{new_a1} words for shopping, family, food and getting around."),
            ("italian_cils_a2", "Level A2 · Elementary", "Everyday conversation: work, travel, health, plans. {new_a2} new words, plus all of A1."),
            ("italian_cils_b1", "Level B1 · Intermediate", "Handle most situations, tell stories, give opinions. {new_b1} new words, plus A1 and A2."),
            ("italian_cils_b2", "Level B2 · Upper-intermediate", "Follow films and news; argue a point with nuance. {new_b2} new words, plus A1 to B1."),
        ],
    ),
    (
        "films",
        "Learn from a film",
        "Each word comes with the real line where it is spoken, plus a word-for-word translation. Watch the film afterwards and it clicks.",
        [
            ("italian_movies_le_otto_montagne", "Le otto montagne (2022)", "The words of this drama, in the order you’ll meet them. Italian to English."),
            ("italian_movies_benvenuti_al_sud", "Benvenuti al Sud (2010)", "The words of this comedy, in the order you’ll meet them. Italian to English."),
        ],
    ),
    (
        "common",
        "The words Italians use most",
        "Ranked by how often they turn up in real film and TV dialogue.",
        [("italian_frequency", "Most common verbs & nouns", "About 1,400 words, most useful first.")],
    ),
    (
        "verbs",
        "Verbs, one tense at a time",
        "Each card is a single verb form, like “we will eat” → mangeremo. Do one tense until it clicks.",
        [
            ("italian_verbs_learning_presente", "Present", "I eat, you eat, they eat."),
            ("italian_verbs_learning_passato_prossimo", "Past · I have eaten", "The everyday past tense."),
            ("italian_verbs_learning_imperfetto", "Past · I used to eat", "Habits and scenes from the past."),
            ("italian_verbs_learning_futuro_semplice", "Future", "I will eat."),
            ("italian_verbs_learning_presente_progressivo", "Right now", "I am eating (sto mangiando)."),
            ("italian_verbs_learning_imperativo_con_tu", "Commands · informal", "Eat! Come here! (to a friend)"),
            ("italian_verbs_learning_imperativo_con_lei", "Commands · polite", "Please sit. (to a stranger)"),
            ("italian_verbs_more_tenses_condizionale_presente", "Would", "I would eat."),
            ("italian_verbs_more_tenses_condizionale_passato", "Would have", "I would have eaten."),
            ("italian_verbs_more_tenses_imperativo_con_noi", "Let’s…", "Let’s eat!"),
            ("italian_verbs_more_tenses_imperativo_con_voi", "Commands · to a group", "Eat, everyone!"),
        ],
    ),
    (
        "extras",
        "Extra practice",
        "Small decks that fix common stumbling blocks.",
        [
            ("italian_noun_phrases", "The little words", "il, la, un, del, nel, mio… in front of 3,000 real nouns. English → Italian only."),
            ("italian_cognates", "Words you already know", "Italian words that look like English: generoso = generous."),
        ],
    ),
]


def _size(n: int) -> str:
    return f"{n / 1e6:.1f} MB" if n >= 1e6 else f"{n / 1e3:.0f} KB"


def _deck_groups(decks: dict, new_at: dict) -> str:
    e = _html.escape
    fill = {f"new_{k}": f"{v:,}" for k, v in new_at.items()}
    out = []
    for gid, title, blurb, items in GROUPS:
        cards = []
        for stem, name, desc in items:
            d = decks[stem]
            desc = desc.format(**fill)
            cards.append(
                f'''<article class="deck">
  <div>
    <h4>{e(name)}</h4>
    <p>{e(desc)}</p>
  </div>
  <div class="deck-foot">
    <span class="meta">{d["cards"]:,} cards · {_size(d["bytes"])}</span>
    <a class="btn btn-small" href="{DOWNLOAD_BASE}{e(d["file"])}" rel="nofollow">Download</a>
  </div>
</article>'''
            )
        out.append(
            f'''<div class="group" id="{gid}">
  <div class="group-head"><h3>{e(title)}</h3><p>{e(blurb)}</p></div>
  <div class="deck-grid">
{chr(10).join(cards)}
  </div>
</div>'''
        )
    return "\n".join(out)


def render_page(data: dict) -> None:
    """site/index.template.html → site/index.html with real counts."""
    decks = data["decks"]
    missing = {s for _g, _t, _b, items in GROUPS for s, _n, _d in items} - set(decks)
    if missing:
        raise SystemExit(f"page lists decks that were not built: {sorted(missing)}")
    page = (TOOLS_DIR / "index.template.html").read_text(encoding="utf-8")
    st = data["stats"]
    swaps = {
        "{{DECKS}}": _deck_groups(decks, st["new_at"]),
        "{{WORDS}}": f"{st['words'] // 100 * 100:,}+",
        "{{VERB_FORMS}}": f"{st['verb_forms'] // 100 * 100:,}+",
        "{{FACT_PERCENT}}": f"{round(st['fact_share'] * 100 / 5) * 5}%",
        "{{DECK_COUNT}}": str(len(decks)),
        "{{TOTAL_SIZE}}": f"{sum(d['bytes'] for d in decks.values()) / 1e6:,.0f} MB",
        "{{SAMPLES_JSON}}": json.dumps(data["samples"], ensure_ascii=False).replace("</", "<\\/"),
        "{{BUILT}}": datetime.date.today().strftime("%B %Y"),
    }
    for key, value in swaps.items():
        page = page.replace(key, value)
    (SITE_DIR / "index.html").write_text(page, encoding="utf-8")


_WORD_DECKS = [
    "italian_basics_numbers", "italian_cils_a1", "italian_cils_a2", "italian_cils_b1", "italian_cils_b2",
    "italian_core_fluent_forever_625", "italian_frequency",
    "italian_movies_le_otto_montagne", "italian_movies_benvenuti_al_sud",
]
_CILS = ["a1", "a2", "b1", "b2"]


def _stats(public: dict[str, Path]) -> dict:
    """Distinct words, CILS words new at each level, notes that have a fun fact.

    Every list keeps its own copy of a word, so deck totals overcount: the CILS
    levels contain the levels below them, and common words recur across decks.
    """
    import html

    words: set[str] = set()
    new_at: dict[str, int] = {}
    facts = total = 0
    for stem in _WORD_DECKS:
        level = stem.rsplit("_", 1)[-1] if stem.startswith("italian_cils_") else None
        with zipfile.ZipFile(public[stem]) as z, tempfile.TemporaryDirectory() as tmp:
            z.extract("collection.anki2", tmp)
            db = sqlite3.connect(Path(tmp) / "collection.anki2")
            lower = {f"list::cils-{x}" for x in _CILS[: _CILS.index(level)]} if level else None
            new = 0
            for flds, tags in db.execute("SELECT flds, tags FROM notes"):
                row = flds.split("\x1f")
                words.add(html.unescape(row[0]))
                total += 1
                facts += bool(row[9])
                if lower is not None and not lower & set(tags.split()):
                    new += 1
            db.close()
        if lower is not None:
            new_at[level] = new
    return {"words": len(words), "new_at": new_at, "fact_share": facts / total}


def main() -> int:
    print("Building site/ from decks/")
    out_dir = SITE_DIR / "downloads"
    out_dir.mkdir(parents=True, exist_ok=True)
    decks = {}
    for src in sorted(DECKS_DIR.glob("*.apkg")):
        if src.stem in PRIVATE_DECKS:
            print(f"  skip   {src.name} (private source)")
            continue
        dst = out_dir / src.name.replace("_dormant_", "_more_tenses_")
        counts = _sanitise(src, dst)
        if counts["tutto_left"]:
            raise SystemExit(f"{dst.name}: Tutto bene tag left after sanitising")
        decks[dst.stem] = {"file": dst.name, "bytes": dst.stat().st_size, "sha256": sha256_file(dst), **counts}
        print(f"  ok     {dst.name:58} {counts['notes']:>5} notes  {dst.stat().st_size / 1e6:5.1f} MB")
    samples = _samples({stem: out_dir / info["file"] for stem, info in decks.items()})
    data = {"decks": decks, "samples": samples}
    data["stats"] = _stats({stem: out_dir / info["file"] for stem, info in decks.items()})
    data["stats"]["verb_forms"] = sum(d["notes"] for stem, d in decks.items() if stem.startswith("italian_verbs_"))
    (TOOLS_DIR / "site_data.json").write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"  ok     {len(samples)} sample cards → site/samples/")
    render_page(data)
    print("  ok     site/index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
