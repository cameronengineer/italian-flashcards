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
SAMPLES = [('italian_cils_a1', 'il caffè'), ('italian_verbs_learning_presente', 'vedo'), ('italian_noun_phrases', 'il loro mondo'), ('italian_basics_numbers', 'undici'), ('italian_cils_a2', 'la fine'), ('italian_cils_b1', 'il passaggio'), ('italian_cils_b2', 'vestirsi'), ('italian_cognates', 'possibile'), ('italian_core_fluent_forever_625', 'il cane'), ('italian_frequency', 'la grazia'), ('italian_movies_benvenuti_al_sud', 'questo'), ('italian_movies_le_otto_montagne', 'ci'), ('italian_verbs_more_tenses_condizionale_passato', 'avremmo visto'), ('italian_verbs_more_tenses_condizionale_presente', 'vedrebbe'), ('italian_verbs_more_tenses_imperativo_con_noi', 'pensiamo'), ('italian_verbs_more_tenses_imperativo_con_voi', 'chiedete'), ('italian_verbs_learning_futuro_semplice', 'vedrai'), ('italian_verbs_learning_imperativo_con_lei', 'pensi'), ('italian_verbs_learning_imperativo_con_tu', 'chiedi'), ('italian_verbs_learning_imperfetto', 'vedevamo'), ('italian_verbs_learning_passato_prossimo', 'hanno visto'), ('italian_verbs_learning_presente_progressivo', 'stiamo vedendo')]
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
        for nid, _flds, tags in rows:
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
                row = dict(zip(_FIELDS, flds.split("\x1f"), strict=False))
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

# (title, blurb, [(deck file stem, name, one-line description)])
GROUPS = [
    (
        "start",
        "Everyday vocabulary",
        "Two collections of everyday words and numbers.",
        [
            ("italian_core_fluent_forever_625", "Fluent Forever 625", "Family, food, home, body and time."),
            ("italian_basics_numbers", "Numbers", "Practice with easily confused numbers, including the teens and tens."),
        ],
    ),
    (
        "levels",
        "Words by level",
        "Based on the CILS exam levels. Each level includes the levels below it, so you only need one.",
        [
            ("italian_cils_a1", "Level A1 · Beginner", "{new_a1} words for shopping, family, food and getting around."),
            ("italian_cils_a2", "Level A2 · Elementary", "Everyday conversation: work, travel, health, plans. {new_a2} new words, plus all of A1."),
            ("italian_cils_b1", "Level B1 · Intermediate", "Situations, stories and opinions. {new_b1} new words, plus A1 and A2."),
            ("italian_cils_b2", "Level B2 · Upper-intermediate", "Vocabulary for films, news and discussion. {new_b2} new words, plus A1 to B1."),
        ],
    ),
    (
        "films",
        "Film vocabulary",
        "The words of a film, each with the line where it\u2019s spoken and a word-for-word translation.",
        [
            ("italian_movies_le_otto_montagne", "Le otto montagne (2022)", "A drama. Italian to English."),
            ("italian_movies_benvenuti_al_sud", "Benvenuti al Sud (2010)", "A comedy. Italian to English."),
        ],
    ),
    (
        "common",
        "Frequent words",
        "Ranked by how often they appear in film and TV dialogue.",
        [("italian_frequency", "Most common verbs & nouns", "Verbs and nouns from the SUBTLEX-IT frequency list.")],
    ),
    (
        "verbs",
        "Verbs, one tense at a time",
        "Each card is one verb form, like “we will eat” → mangeremo.",
        [
            ("italian_verbs_learning_presente", "Presente", "Present indicative: mangio (I eat)."),
            ("italian_verbs_learning_passato_prossimo", "Passato prossimo", "Compound past: ho mangiato (I ate / I have eaten)."),
            ("italian_verbs_learning_imperfetto", "Imperfetto", "Imperfect: mangiavo (I used to eat / I was eating)."),
            ("italian_verbs_learning_futuro_semplice", "Futuro semplice", "Simple future: mangerò (I will eat)."),
            ("italian_verbs_learning_presente_progressivo", "Presente progressivo", "Present progressive: sto mangiando (I am eating)."),
            ("italian_verbs_learning_imperativo_con_tu", "Imperativo · tu", "Informal singular imperative: mangia! (eat!)."),
            ("italian_verbs_learning_imperativo_con_lei", "Imperativo · Lei", "Formal singular imperative: si sieda (please sit down)."),
            ("italian_verbs_more_tenses_condizionale_presente", "Condizionale presente", "Present conditional: mangerei (I would eat)."),
            ("italian_verbs_more_tenses_condizionale_passato", "Condizionale passato", "Past conditional: avrei mangiato (I would have eaten)."),
            ("italian_verbs_more_tenses_imperativo_con_noi", "Imperativo · noi", "First-person plural imperative: mangiamo! (let’s eat!)."),
            ("italian_verbs_more_tenses_imperativo_con_voi", "Imperativo · voi", "Plural imperative: mangiate! (eat!)."),
        ],
    ),
    (
        "extras",
        "Extra practice",
        "Small decks for common stumbling blocks.",
        [
            ("italian_noun_phrases", "Noun phrases", "Articles, prepositions and possessives with Italian nouns: il, la, un, del, nel, mio… English → Italian."),
            ("italian_cognates", "Cognates", "Related Italian and English words: generoso = generous."),
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
        rows = []
        for stem, name, desc in items:
            d = decks[stem]
            rows.append(
                f'''<li>
  <div><span class="deck-name">{e(name)}</span><br>{e(desc.format(**fill))}<br><small>{d["cards"]:,} cards · {_size(d["bytes"])}</small></div>
  <a href="{DOWNLOAD_BASE}{e(d["file"])}" aria-label="Download {e(name)} deck">Download<span class="vh"> the {e(name)} deck</span> <span aria-hidden="true">↓</span></a>
</li>'''
            )
        out.append(f'<div class="deck-group" id="deck-{gid}"><div class="group-intro"><h3>{e(title)}</h3><p>{e(blurb)}</p></div><ul class="decks">{chr(10).join(rows)}</ul></div>')
    return "\n".join(out)


def _sample_html(s: dict, lazy: bool = False) -> str:
    e = _html.escape
    production = not s["recognition"]
    label = "Verb form" if s["deck"].startswith("italian_verbs_") else "Noun phrase" if s["deck"] == "italian_noun_phrases" else "Vocabulary"
    slug = e(s["slug"])
    loading = ' loading="lazy"' if lazy else ""
    # The alt text is the English only: on an English → Italian card the picture is the prompt.
    picture = (f'<img src="samples/{slug}.jpg" alt="Illustration for {e(s["english"])}"'
               f' width="512" height="384" decoding="async"{loading}>')
    audio = f'<audio controls preload="none" aria-label="Pronunciation of {e(s["italian"])}" src="samples/{slug}.mp3">Your browser does not support audio. <a href="samples/{slug}.mp3">Download the recording of {e(s["italian"])}</a>.</audio>'
    italian = f'<div class="word" lang="it">{e(s["italian"])}</div><div class="ipa">{e(s["ipa"])}</div>'
    english = f'<div class="word">{e(s["english"])}</div>'
    hint = f'<p class="det">{e(s["hint"])}</p>' if s["hint"] else ""
    front = picture + english + hint if production else italian + audio
    back = italian + audio if production else english + picture
    ex = ""
    if s["example"]:
        ex = f'<p class="ex"><span lang="it">{e(s["example"][0])}</span><br><small>{e(s["example"][1])}<br>{e(s["example"][2])}</small></p>'
    fact = f'<p class="fact"><b>{e(s["facttitle"] or "Did you know?")}</b> {e(s["fact"])}</p>' if s["fact"] else ""
    details = " · ".join(x for x in (s["details"], s["forms"]) if x)
    return f'''<div class="sample-panel" id="sample-{slug}" aria-labelledby="label-{slug}">
  <h3 id="label-{slug}" class="sample-label">{label} · {"English → Italian" if production else "Italian → English"}</h3>
  <div class="sample">
    <div class="side"><small>Front</small>{front}</div>
    <div class="side"><small>Back</small>{back}<p class="det">{e(details)}</p><p>{e(s["note"])}</p>{ex}{fact}</div>
  </div>
</div>'''


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
        "{{DECK_COUNT}}": str(len(decks)),
        "{{TOTAL_SIZE}}": f"{sum(d['bytes'] for d in decks.values()) / 1e6:,.0f} MB",
        # Only the first panel shows at load; the hidden ones fetch their picture when opened.
        "{{SAMPLE}}": "\n".join(_sample_html(sample, lazy=i > 0) for i, sample in enumerate(
            s for s in data["samples"]
            if s["deck"] in {"italian_cils_a1", "italian_verbs_learning_presente", "italian_noun_phrases"})),
        "{{BUILT}}": datetime.date.today().strftime("%B %Y"),
    }
    for key, value in swaps.items():
        page = page.replace(key, value)
    (SITE_DIR / "index.html").write_text(page, encoding="utf-8")
    # Work both as a standalone script and as an imported build helper.
    import sys
    try:
        from .site_pages import build
    except ImportError:  # python3 tools/build_site.py
        from site_pages import build
    build(data, sys.modules[__name__])


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


def _plain(value: str) -> str:
    import html

    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def _spread(rows: list, k: int) -> list:
    """``k`` rows evenly spaced through ``rows`` (all of them if there are fewer)."""
    if len(rows) <= k:
        return list(rows)
    return [rows[round(i * (len(rows) - 1) / (k - 1))] for i in range(k)]


# Conjugations shown in full on each tense page, if the deck has them.
_SHOWN_VERBS = ("vedere", "parlare")
_PERSONS = ("I ", "you all ", "you ", "he/she ", "we ", "they ")
_PERSON_ORDER = ("I ", "you ", "he/she ", "we ", "you all ", "they ")


def _person(english: str) -> int:
    for prefix in _PERSONS:
        if english.startswith(prefix):
            return _PERSON_ORDER.index(prefix)
    return len(_PERSON_ORDER)


def _preview(path: Path, stem: str | None = None) -> dict:
    """Source-derived text for the public deck guide.

    ``preview`` is a spread of the deck's words (for a CILS level, only the
    words new at that level); verb decks add ``verbs`` (every infinitive, with
    the form when the deck has one form per verb) and ``conjugations``.
    """
    stem = stem or path.stem
    with zipfile.ZipFile(path) as archive, tempfile.TemporaryDirectory() as folder:
        archive.extract("collection.anki2", folder)
        db = sqlite3.connect(Path(folder) / "collection.anki2")
        try:
            rows = [(f.split("\x1f"), tags.split()) for f, tags in
                    db.execute("SELECT flds, tags FROM notes ORDER BY sfld, id").fetchall()]
        finally:
            db.close()
    out: dict = {}
    if stem.startswith("italian_cils_"):
        level = stem.rsplit("_", 1)[-1]
        lower = {f"list::cils-{x}" for x in _CILS[: _CILS.index(level)]}
        rows = [(f, tags) for f, tags in rows if not lower & set(tags)]
    if not stem.startswith("italian_verbs_"):
        out["preview"] = [{"italian": _plain(f[0]), "english": _plain(f[1])} for f, _t in _spread(rows, 40)]
        return out
    out["preview"] = [{"italian": _plain(f[0]), "english": _plain(f[1])} for f, _t in _spread(rows, 12)]
    by_verb: dict[str, list] = {}
    for f, tags in rows:
        verb = next((t.removeprefix("verb::") for t in tags if t.startswith("verb::")), None)
        if verb:
            by_verb.setdefault(verb, []).append(f)
    verbs = []
    for verb in sorted(by_verb):
        forms = by_verb[verb]
        meaning = _plain(forms[0][4]).partition(" — ")[2]
        entry = {"infinitive": verb, "english": meaning}
        if len(forms) == 1:
            entry |= {"form": _plain(forms[0][0]), "prompt": _plain(forms[0][1])}
        verbs.append(entry)
    out["verbs"] = verbs
    out["conjugations"] = {
        verb: [{"italian": _plain(f[0]), "english": _plain(f[1])}
               for f in sorted(by_verb[verb], key=lambda f: (_person(_plain(f[1])), _plain(f[0])))]
        for verb in _SHOWN_VERBS if len(by_verb.get(verb, [])) >= 5
    }
    return out


def coverage(stems) -> tuple[list[str], list[str]]:
    """(decks the page lists but decks/ doesn't have, decks built but not on the page)."""
    listed = {stem for _gid, _title, _blurb, items in GROUPS for stem, _name, _desc in items}
    return sorted(listed - set(stems)), sorted(set(stems) - listed)


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
        decks[dst.stem] = {"file": dst.name, "bytes": dst.stat().st_size, "sha256": sha256_file(dst),
                           **_preview(dst), **counts}
        print(f"  ok     {dst.name:58} {counts['notes']:>5} notes  {dst.stat().st_size / 1e6:5.1f} MB")
    missing, unlisted = coverage(decks)
    if missing:
        raise SystemExit(
            f"decks/ has no package for {', '.join(missing)}: run ./run.sh first, or take them out of GROUPS"
        )
    for stem in unlisted:
        print(f"  note   {stem} is built but not on the site (add it to GROUPS in tools/build_site.py)")
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
