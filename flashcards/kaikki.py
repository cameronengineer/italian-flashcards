"""Kaikki / Wiktionary: the non-AI reference dictionary.

`kaikki.org <https://kaikki.org/dictionary/Italian/>`_ publishes English
Wiktionary as JSON Lines (wiktextract, Ylonen 2022; content CC BY-SA). The
Italian file (~770 MB) is **streamed** straight into a compact SQLite index
at ``data/kaikki.sqlite`` — the raw file is never stored:

* ``entries`` — lemma entries (pruned JSON, zlib-compressed), by word + POS;
* ``forms``   — every inflected form → its lemma (from inflection tables and
  from Wiktionary's "form of" entries); this is the lemmatiser used for
  movie subtitles.

Helpers turn an entry into what the pipeline needs: gender, plural,
conjugation table (mapped to our tenses), auxiliary, participles, IPA,
Commons pronunciation audio, etymology text, senses and synonyms. Stress
marks Wiktionary writes on inner vowels are normalised away.
"""

from __future__ import annotations

import json
import atexit
import re
import hashlib
import tempfile
from pathlib import Path
import sqlite3
import time
import urllib.request
import zlib
from contextlib import closing
from functools import lru_cache
from typing import Iterator

from .italian import normalise_spelling, plain
from .paths import DATA_DIR

KAIKKI_URL = "https://kaikki.org/dictionary/Italian/kaikki.org-dictionary-Italian.jsonl"
KAIKKI_DB = DATA_DIR / "kaikki.sqlite"
ATTRIBUTION = "Wiktionary (CC BY-SA) via kaikki.org"

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id      INTEGER PRIMARY KEY,
    word    TEXT NOT NULL,          -- normalised spelling
    plain   TEXT NOT NULL,          -- lowercase, no accents (lookup key)
    pos     TEXT NOT NULL,
    data    BLOB NOT NULL           -- zlib(JSON), pruned
);
CREATE INDEX IF NOT EXISTS idx_entries_plain ON entries(plain, pos);
CREATE TABLE IF NOT EXISTS forms (
    plain   TEXT NOT NULL,          -- the inflected form, lookup key
    form    TEXT NOT NULL,
    lemma   TEXT NOT NULL,
    pos     TEXT NOT NULL,
    tags    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_forms_plain ON forms(plain);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS remote (url TEXT PRIMARY KEY, body TEXT);
"""

_SKIP_FORM_TAGS = {"table-tags", "inflection-template", "class", "romanization"}


def connect(path: Path | None = None) -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path or KAIKKI_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    if "attempted_at" not in {r[1] for r in conn.execute("PRAGMA table_info(remote)")}:
        conn.execute("ALTER TABLE remote ADD COLUMN attempted_at TEXT")
    return conn


def available() -> bool:
    if not KAIKKI_DB.exists():
        return False
    try:
        with closing(sqlite3.connect(f"file:{KAIKKI_DB}?mode=ro", uri=True)) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'imported_at'").fetchone()
        return row is not None
    except sqlite3.Error:
        return False


# ── Import ─────────────────────────────────────────────────────────────────


def _prune(d: dict) -> dict:
    senses = []
    for s in d.get("senses", []):
        senses.append(
            {
                k: v
                for k, v in {
                    "glosses": s.get("glosses"),
                    "tags": s.get("tags"),
                    "form_of": [f.get("word") for f in s.get("form_of", []) if f.get("word")] or None,
                    "synonyms": [x.get("word") for x in s.get("synonyms", []) if x.get("word")][:8] or None,
                    "examples": [
                        {"text": e.get("text"), "english": e.get("english") or e.get("translation")}
                        for e in s.get("examples", [])[:2]
                        if e.get("text")
                    ]
                    or None,
                }.items()
                if v
            }
        )
    return {
        "word": d.get("word"),
        "pos": d.get("pos"),
        "head": [h.get("expansion") for h in d.get("head_templates", []) if h.get("expansion")][:2],
        "etymology_text": d.get("etymology_text"),
        "etymology_templates": [
            {"name": t.get("name"), "args": t.get("args")} for t in d.get("etymology_templates", [])[:6]
        ],
        "forms": [
            {"form": f.get("form"), "tags": f.get("tags", [])}
            for f in d.get("forms", [])
            if f.get("form") and not _SKIP_FORM_TAGS & set(f.get("tags", []))
        ],
        "senses": senses,
        "sounds": [
            {k: s[k] for k in ("ipa", "mp3_url", "ogg_url", "tags") if k in s}
            for s in d.get("sounds", [])
            if "ipa" in s or "mp3_url" in s or "ogg_url" in s
        ][:6],
        "hyphenation": (d.get("hyphenations") or [{}])[0].get("parts") if d.get("hyphenations") else None,
        "irregular": any(
            f.get("form") == "irregular" and "table-tags" in f.get("tags", []) for f in d.get("forms", [])
        ),
    }


def _is_form_entry(d: dict) -> bool:
    senses = d.get("senses", [])
    return bool(senses) and all(s.get("form_of") for s in senses)


def _lines(url: str) -> Iterator[bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "italian-flashcards (personal study)"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        for line in resp:
            yield line


def _import_stream(url: str, target: Path, *, progress_every: int = 50_000) -> dict:
    """Stream the Kaikki Italian JSONL into ``data/kaikki.sqlite``."""
    started = time.time()
    counts = {"lines": 0, "entries": 0, "form_entries": 0, "forms": 0}
    digest = hashlib.sha256()
    with closing(connect(target)) as conn:
        conn.execute("DELETE FROM entries")
        conn.execute("DELETE FROM forms")
        conn.execute("DELETE FROM meta")
        entry_rows: list[tuple] = []
        form_rows: list[tuple] = []

        def flush() -> None:
            conn.executemany("INSERT INTO entries (word, plain, pos, data) VALUES (?, ?, ?, ?)", entry_rows)
            conn.executemany(
                "INSERT INTO forms (plain, form, lemma, pos, tags) VALUES (?, ?, ?, ?, ?)", form_rows
            )
            conn.commit()
            entry_rows.clear()
            form_rows.clear()

        for raw in _lines(url):
            digest.update(raw)
            counts["lines"] += 1
            try:
                d = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid dictionary JSON at line {counts['lines']}") from exc
            if d.get("lang_code") not in (None, "it"):
                continue
            word = normalise_spelling(d.get("word") or "")
            pos = d.get("pos") or ""
            if not word:
                continue
            if _is_form_entry(d):
                counts["form_entries"] += 1
                for s in d["senses"]:
                    for f in s.get("form_of", []):
                        lemma = normalise_spelling(f.get("word") or "")
                        if lemma:
                            form_rows.append((plain(word), word, lemma, pos, " ".join(s.get("tags", []))))
                continue
            pruned = _prune(d)
            entry_rows.append(
                (word, plain(word), pos, zlib.compress(json.dumps(pruned, ensure_ascii=False).encode()))
            )
            counts["entries"] += 1
            for f in pruned["forms"]:
                form = normalise_spelling(f["form"])
                if " " in form and form.split(" ")[0] not in ("mi", "ti", "si", "ci", "vi"):
                    continue
                form_rows.append((plain(form), form, word, pos, " ".join(f["tags"])))
                counts["forms"] += 1
            # the lemma is its own form
            form_rows.append((plain(word), word, word, pos, "lemma"))
            if len(entry_rows) >= 5000 or len(form_rows) >= 50_000:
                flush()
            if counts["lines"] % progress_every == 0:
                print(
                    f"  {counts['lines']:,} lines · {counts['entries']:,} entries · "
                    f"{time.time() - started:.0f}s",
                    flush=True,
                )
        flush()
        if not counts["entries"] or not counts["forms"]:
            raise ValueError("Dictionary import was empty/incomplete")
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('content_sha256',?)", (digest.hexdigest(),))
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('parser_version','5')")
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('imported_at', datetime('now'))")
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('source', ?)", (url,))
        conn.commit()
        conn.execute("ANALYZE")
    counts["seconds"] = round(time.time() - started)
    return counts


def import_stream(url: str = KAIKKI_URL, *, progress_every=50_000):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kaikki-import-", dir=DATA_DIR) as tmp:
        target = Path(tmp) / "index.sqlite"
        counts = _import_stream(url, target, progress_every=progress_every)
        with closing(sqlite3.connect(target)) as conn:
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Dictionary integrity check failed")
        if _ro.cache_info().currsize:
            _ro().close()
        _ro.cache_clear()
        if KAIKKI_DB.exists():
            # Keep the fetched etymology pages; they are slow to re-fetch.
            with closing(sqlite3.connect(target)) as conn:
                conn.execute("ATTACH DATABASE ? AS old", (str(KAIKKI_DB),))
                if conn.execute("SELECT 1 FROM old.sqlite_master WHERE name='remote'").fetchone():
                    cols = [r[1] for r in conn.execute("PRAGMA old.table_info(remote)")]
                    conn.execute(
                        f"INSERT OR IGNORE INTO remote({','.join(cols)}) SELECT {','.join(cols)} FROM old.remote"
                    )
                conn.commit()
            # One previous index is kept for rollback (it is regenerable data, not media).
            KAIKKI_DB.replace(DATA_DIR / "kaikki.previous.sqlite")
        target.replace(KAIKKI_DB)
        return counts


def version():
    if not KAIKKI_DB.exists():
        return "missing"
    with closing(sqlite3.connect(f"file:{KAIKKI_DB}?mode=ro", uri=True)) as conn:
        meta = dict(conn.execute("SELECT key,value FROM meta"))
    return meta.get("content_sha256") or meta.get("imported_at", "unknown")


# ── Lookup ─────────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _ro() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{KAIKKI_DB}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _close_read_connection():
    if _ro.cache_info().currsize:
        _ro().close()
        _ro.cache_clear()


atexit.register(_close_read_connection)


def entries(word: str, pos: str | None = None) -> list[dict]:
    """Lemma entries for a word (any POS unless given), best match first."""
    rows = (
        _ro()
        .execute(
            "SELECT word, pos, data FROM entries WHERE plain = ?" + (" AND pos = ?" if pos else ""),
            (plain(word), pos) if pos else (plain(word),),
        )
        .fetchall()
    )
    out = [json.loads(zlib.decompress(r["data"])) for r in rows]
    exact = normalise_spelling(word)
    out.sort(key=lambda e: (normalise_spelling(e["word"]) != exact, len(e.get("senses", [])) == 0))
    return out


def lemmas_for(form: str) -> list[tuple[str, str, str]]:
    """(lemma, pos, tags) for an inflected form, e.g. 'andiamo' → [('andare', 'verb', …)]."""
    rows = _ro().execute("SELECT lemma, pos, tags FROM forms WHERE plain = ?", (plain(form),)).fetchall()
    seen, out = set(), []
    for r in rows:
        key = (r["lemma"], r["pos"])
        if key not in seen:
            seen.add(key)
            out.append((r["lemma"], r["pos"], r["tags"]))
    return out


# ── Facts about one entry ──────────────────────────────────────────────────


def glosses(entry: dict) -> list[str]:
    out = []
    for s in entry.get("senses", []):
        for g in s.get("glosses", [])[-1:]:
            if g and g not in out:
                out.append(g)
    return out


def gender(entry: dict) -> str | None:
    # Only the headword's gender, before derivative/plural annotations. A
    # masculine diminutive in camera's expanded head is not camera's gender.
    heads = entry.get("head", [])
    head = " ".join(heads).split("(", 1)[0]
    match = re.search(r"\b(m|f)(?:\s*(?:or|and|/|,)\s*(m|f))?\b", head)
    if match:
        genders = {match[1], match[2]} - {None}
        return "both" if len(genders) == 2 else "masculine" if "m" in genders else "feminine"
    tags = {
        t
        for sense in entry.get("senses", [])
        for t in sense.get("tags", [])
        if not set(sense.get("tags", [])) & {"plural", "diminutive", "augmentative"}
    }
    genders = tags & {"masculine", "feminine"}
    return "both" if len(genders) == 2 else next(iter(genders), None)


def noun_forms(entry):
    singular = gender(entry)
    result = [{"form": entry["word"], "number": "singular", "gender": singular}]
    for item in entry.get("forms", []):
        tags = set(item.get("tags", []))
        if "plural" not in tags or tags & {"diminutive", "augmentative"}:
            continue
        genders = tags & {"masculine", "feminine"}
        result.append(
            {
                "form": normalise_spelling(item["form"]),
                "number": "plural",
                "gender": next(iter(genders)) if len(genders) == 1 else singular,
                "tags": sorted(tags),
            }
        )
    return result


def plural(entry: dict) -> str | None:
    for f in entry.get("forms", []):
        if "plural" in f["tags"] and not (
            {"feminine", "masculine"} & set(f["tags"]) and entry.get("pos") == "noun"
        ):
            return normalise_spelling(f["form"])
    for f in entry.get("forms", []):
        if "plural" in f["tags"]:
            return normalise_spelling(f["form"])
    return None


def adjective_forms(entry: dict) -> dict[str, str]:
    """{'ms': bello, 'fs': bella, 'mp': belli, 'fp': belle} where known."""
    out = {"ms": normalise_spelling(entry["word"])}
    for f in entry.get("forms", []):
        tags = set(f["tags"])
        key = ("f" if "feminine" in tags else "m" if "masculine" in tags else "") + (
            "p" if "plural" in tags else "s" if "singular" in tags else ""
        )
        if key in ("fs", "mp", "fp") and key not in out:
            out[key] = normalise_spelling(f["form"])
        elif key == "p" and "mp" not in out:  # invariable-gender plural (grande → grandi)
            out["mp"] = out["fp"] = normalise_spelling(f["form"])
    return out


def ipa(entry: dict) -> str | None:
    for s in entry.get("sounds", []):
        if s.get("ipa"):
            return s["ipa"]
    return None


def audio_url(entry: dict) -> str | None:
    for s in entry.get("sounds", []):
        if s.get("mp3_url"):
            return s["mp3_url"]
    return None


# Kaikki tag sets → our tense keys.
_PERSON_TAGS = {
    ("first-person", "singular"): "io",
    ("second-person", "singular"): "tu",
    ("singular", "third-person"): "lui_lei",
    ("first-person", "plural"): "noi",
    ("plural", "second-person"): "voi",
    ("plural", "third-person"): "loro",
}


def verb_info(entry: dict) -> dict:
    """Conjugation table + auxiliary + participles from a verb entry.

    Returns ``{"auxiliary", "past_participle", "gerund", "irregular",
    "reflexive", "forms": {tense: {person: form}}}`` with tenses
    presente, imperfetto, futuro_semplice, condizionale_presente,
    imperativo (tu, Lei, noi, voi), passato_remoto.
    """
    word = normalise_spelling(entry["word"])
    reflexive = word.endswith("si") and any(
        f["form"].startswith(("mi ", "ti ")) for f in entry.get("forms", [])
    )
    out: dict = {
        "auxiliary": None,
        "past_participle": None,
        "gerund": None,
        "irregular": bool(entry.get("irregular")),
        "reflexive": reflexive,
        "forms": {},
    }
    for f in entry.get("forms", []):
        tags = set(f["tags"])
        form = normalise_spelling(f["form"])
        if "auxiliary" in tags:
            aux = plain(form)
            out["auxiliary"] = aux if out["auxiliary"] in (None, aux) else "both"
            continue
        if tags == {"participle", "past"} and not out["past_participle"]:
            out["past_participle"] = form
            continue
        if tags == {"gerund"} and not out["gerund"]:
            out["gerund"] = form
            continue
        if "negative" in tags or "subjunctive" in tags:
            continue
        tense = None
        if "imperative" in tags:
            tense = "imperativo"
        elif "conditional" in tags:
            tense = "condizionale_presente"
        elif "indicative" in tags:
            if "imperfect" in tags:
                tense = "imperfetto"
            elif "future" in tags:
                tense = "futuro_semplice"
            elif "historic" in tags:
                tense = "passato_remoto"
            elif "present" in tags:
                tense = "presente"
        if not tense:
            continue
        if tense == "imperativo":
            if "formal" in tags and "singular" in tags:
                person = "Lei"
            elif "formal" in tags:
                continue
            elif "second-person" in tags and "singular" in tags:
                person = "tu"
            elif "first-person" in tags and "plural" in tags:
                person = "noi"
            elif "second-person" in tags and "plural" in tags:
                person = "voi"
            else:
                continue
        else:
            person = next((p for t, p in _PERSON_TAGS.items() if set(t) <= tags), None)
            if not person:
                continue
        out["forms"].setdefault(tense, {}).setdefault(person, form)
    return out


def etymology_source(entry: dict) -> tuple[str, str] | None:
    """(language code, word) the etymology borrows/inherits from, if any."""
    for t in entry.get("etymology_templates", []):
        if t.get("name") in ("bor", "bor+", "inh", "inh+", "der", "der+", "lbor"):
            args = t.get("args") or {}
            lang, word = args.get("2"), args.get("3")
            if lang and word:
                return lang, word
    return None


_LANG_NAMES = {
    "fr": "French",
    "la": "Latin",
    "LL.": "Latin",
    "ML.": "Latin",
    "la-lat": "Latin",
    "la-vul": "Latin",
    "grc": "Ancient Greek",
    "de": "German",
    "ar": "Arabic",
    "es": "Spanish",
    "en": "English",
    "pt": "Portuguese",
    "frk": "Frankish",
    "fro": "Old French",
    "ca": "Catalan",
    "nl": "Dutch",
    "tr": "Turkish",
    "gem": "Proto-Germanic",
    "lmo": "Lombard",
    "vec": "Venetian",
    "nap": "Neapolitan",
}


def source_etymology(lang: str, word: str) -> str | None:
    """One hop down the chain: the etymology text of the source word's own
    Wiktionary entry (e.g. French *robinet* → "From Robin …"). Cached."""
    name = _LANG_NAMES.get(lang)
    if not name or not word:
        return None
    w = word.split(",")[0].strip()
    if not w or "/" in w:
        return None
    first = w[0].lower()
    two = w[:2].lower()
    url = (
        f"https://kaikki.org/dictionary/{urllib.request.quote(name)}/meaning/"
        f"{urllib.request.quote(first)}/{urllib.request.quote(two)}/{urllib.request.quote(w)}.jsonl"
    )
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT body FROM remote WHERE url = ? AND (body != '' OR attempted_at>datetime('now','-1 hour'))",
            (url,),
        ).fetchone()
        if row is None:
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "italian-flashcards (personal study)"}
                )
                with urllib.request.urlopen(req, timeout=20) as resp:
                    body = resp.read().decode("utf-8")
            except Exception:  # noqa: BLE001 — missing page or offline: no chain
                body = ""
            conn.execute(
                "INSERT OR REPLACE INTO remote(url,body,attempted_at) VALUES (?, ?, datetime('now'))",
                (url, body),
            )
            conn.commit()
        else:
            body = row["body"]
    for line in body.splitlines():
        try:
            text = json.loads(line).get("etymology_text")
        except json.JSONDecodeError:
            continue
        if text:
            return text
    return None
