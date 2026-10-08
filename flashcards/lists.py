"""Lists: what you want to learn, read from ``lists.toml``.

Each reader turns one input into :class:`Item` rows — a candidate lemma, a
part-of-speech hint, the source's own English gloss and context — which
:mod:`flashcards.lexicon` resolves to lexemes. Readers never call AI.
"""

from __future__ import annotations

import csv
import random
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import srt
from .csvio import _detect_encoding
from .italian import normalise_spelling, number_words, plain
from .paths import INPUTS_DIR, PROJECT_ROOT

LISTS_PATH = PROJECT_ROOT / "lists.toml"
PLAN_PATH = PROJECT_ROOT / "plan.toml"

KINDS = {"csv", "cils", "verbs", "avere", "subtlex", "numbers", "movie"}
_ARTICLE = re.compile(r"^(il|lo|la|i|gli|le|l'|un|uno|una|un')\s*(?=\w)", re.I)


@dataclass(frozen=True)
class ListDef:
    id: str
    kind: str
    title: str
    deck: str
    path: Path | None
    type: str | None = None
    pos: str | None = None
    facts: bool = True
    recognition_only: bool = False
    extras: dict = field(default_factory=dict)


@dataclass
class Item:
    raw: str                 # as written in the source
    lemma: str               # candidate dictionary form
    pos: str | None          # hint: noun / verb / adj / …; "phrase" for sentences
    english: str = ""        # the source's own gloss (a hint, not the answer)
    gender: str | None = None
    display: str | None = None
    context: dict = field(default_factory=dict)


# ── Manifest ───────────────────────────────────────────────────────────────


def load(path: Path = LISTS_PATH) -> list[ListDef]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    out = []
    known = {"id", "kind", "title", "deck", "path", "type", "pos", "facts", "recognition_only", "disabled"}
    for d in data.get("list", []):
        if d.get("disabled"):
            continue
        if d.get("kind") not in KINDS:
            raise ValueError(f"lists.toml: list {d.get('id')!r} has unknown kind {d.get('kind')!r}")
        out.append(ListDef(
            id=d["id"], kind=d["kind"], title=d.get("title", d["id"]),
            deck=d.get("deck") or f"Italian::{d.get('title', d['id'])}",
            path=(INPUTS_DIR / d["path"]) if d.get("path") else None,
            type=d.get("type"), pos=d.get("pos"),
            facts=bool(d.get("facts", True)),
            recognition_only=bool(d.get("recognition_only", False)),
            extras={k: v for k, v in d.items() if k not in known},
        ))
    ids = [l.id for l in out]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"lists.toml: duplicate list ids {sorted(dupes)}")
    return out


def load_plan(path: Path = PLAN_PATH) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def validate(lists: list[ListDef]) -> list[str]:
    errors = []
    for l in lists:
        if l.kind != "numbers" and (l.path is None or not l.path.exists()):
            errors.append(f"{l.id}: input file missing ({l.path})")
        if not l.deck.startswith("Italian::"):
            errors.append(f"{l.id}: deck {l.deck!r} should live under 'Italian::'")
    plan = load_plan()
    known = {l.id for l in lists}
    for p in plan.get("priority", []):
        if p.get("list") not in known:
            errors.append(f"plan.toml: priority list {p.get('list')!r} is not in lists.toml")
    return errors


# ── Helpers ────────────────────────────────────────────────────────────────


def _rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding=_detect_encoding(path)) as fh:
        return [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(fh)]


def split_article(text: str) -> tuple[str | None, str]:
    """('la', 'destra') for 'la destra'; (None, text) without an article."""
    m = _ARTICLE.match(text)
    if not m:
        return None, text
    return m.group(1).lower(), text[m.end():].strip()


def article_gender(article: str | None) -> str | None:
    if article in ("la", "le", "una", "un'"):
        return "feminine"
    if article in ("il", "lo", "i", "gli", "un", "uno"):
        return "masculine"
    return None


def _first_variant(text: str) -> str:
    """'bello/a/i/e' → 'bello'; 'lo studente/la studentessa' → 'lo studente';
    'uno (1)' → 'uno'."""
    text = re.sub(r"\s*\([^)]*\)", "", text.split(" / ")[0])
    return re.sub(r"/[^\s/]*", "", text).strip()


def is_phrase(text: str) -> bool:
    words = text.replace("…", " ").split()
    return len(words) >= 3 or "..." in text or "…" in text or text.rstrip().endswith(("?", "."))


_CILS_POS = [
    ("sostantivo", "noun"), ("s.m", "noun"), ("s.f", "noun"),
    ("aggettivo", "adj"), ("agg", "adj"),
    ("verbo", "verb"), ("v.t", "verb"), ("v.int", "verb"), ("v.rifl", "verb"),
    ("avverbio", "adv"), ("avv", "adv"),
    ("pronome", "pron"), ("pron", "pron"),
    ("interiezione", "intj"), ("inter", "intj"),
    ("congiunzione", "conj"), ("cong", "conj"),
    ("preposizione", "prep"), ("prep", "prep"),
    ("articolo", "article"), ("art", "article"),
    ("locuzione", "phrase"),
]


def cils_pos(function: str) -> str | None:
    first = re.split(r"\s[-–]\s", function.strip().lower())[0]
    for prefix, pos in _CILS_POS:
        if first.startswith(prefix):
            return pos
    return None


# ── Readers ────────────────────────────────────────────────────────────────


def _read_csv(l: ListDef) -> list[Item]:
    out = []
    for r in _rows(l.path):
        raw = r.get("italian", "")
        if not raw:
            continue
        english = r.get("english", "")
        if is_phrase(raw):
            out.append(Item(raw=raw, lemma=raw, pos="phrase", english=english, display=raw))
            continue
        variant = _first_variant(raw)
        article, word = split_article(variant)
        bare = word.strip("!¡?¿.,;: ")
        out.append(Item(
            raw=raw, lemma=bare, pos=("noun" if article else l.pos),
            english=english, gender=article_gender(article),
            display=raw if l.pos in ("intj", "letter") else None,
        ))
    return out


def _read_cils(l: ListDef) -> list[Item]:
    out = []
    for r in _rows(l.path):
        raw = r.get("italian", "")
        original = r.get("italian_original", "") or raw
        if not raw:
            continue
        pos = cils_pos(r.get("function", ""))
        article, _ = split_article(_first_variant(raw))
        lemma = _first_variant(original)
        if pos == "phrase" or is_phrase(lemma):
            out.append(Item(raw=raw, lemma=raw, pos="phrase", english=r.get("english", ""), display=raw))
            continue
        out.append(Item(
            raw=raw, lemma=lemma, pos=pos, english=r.get("english", ""),
            gender=article_gender(article),
            context={"cils_function": r.get("function", "")},
        ))
    return out


def _read_verbs(l: ListDef) -> list[Item]:
    return [Item(raw=r["italian"], lemma=r["italian"].strip().lower(), pos="verb", english=r.get("english", ""))
            for r in _rows(l.path) if r.get("italian")]


def _read_avere(l: ListDef) -> list[Item]:
    return [Item(raw=r["italian"], lemma=r["italian"].strip(), pos="phrase", english=r.get("english", ""),
                 display=r["italian"].strip(), context={"avere": True})
            for r in _rows(l.path) if r.get("italian")]


def subtlex_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh, delimiter=";"))


def _zipf(v: str | None) -> float | None:
    try:
        return float((v or "").replace(",", "."))
    except ValueError:
        return None


def _read_subtlex(l: ListDef) -> list[Item]:
    rows = subtlex_rows(l.path)
    limits = {"VER": int(l.extras.get("verb_limit", 400)), "NOM": int(l.extras.get("noun_limit", 1000))}
    taken = {"VER": 0, "NOM": 0}
    seen: set[tuple[str, str]] = set()
    out = []
    for file_rank, r in enumerate(rows, start=1):
        pos = r.get("dom_pos")
        if pos not in limits or taken[pos] >= limits[pos]:
            continue
        lemma = (r.get("dom_lemma") or "").strip()
        if not lemma or lemma == "<unknown>" or not lemma.isalpha() or len(lemma) < 2:
            continue
        key = (lemma.lower(), pos)
        if key in seen:
            continue
        seen.add(key)
        taken[pos] += 1
        out.append(Item(raw=lemma, lemma=lemma.lower(), pos="verb" if pos == "VER" else "noun",
                        context={"freq_rank": file_rank, "zipf": _zipf(r.get("zipf"))}))
    return out


def number_set(seed: int = 7) -> list[int]:
    """The numbers worth a card: 0–100, the shapes that teach the rules
    (101, 108, 180, round hundreds/thousands), plus a fixed random sample."""
    nums = list(range(0, 101))
    nums += [101, 102, 103, 108, 110, 111, 118, 123, 180, 199]
    nums += list(range(200, 1001, 100)) + [1001, 1100, 1200, 1500, 1999]
    nums += [2000, 2001, 2023, 2026, 3000, 5000, 10000, 20000, 21000, 50000,
             100000, 200000, 500000, 1_000_000, 2_000_000, 1_500_000]
    rng = random.Random(seed)
    for lo, hi, n in ((101, 999, 15), (1000, 9999, 12), (10000, 999999, 10)):
        nums += rng.sample(range(lo, hi), n)
    return sorted(set(nums))


def _english_number(n: int) -> str:
    return f"{n:,}"


def _read_numbers(l: ListDef) -> list[Item]:
    return [Item(raw=str(n), lemma=number_words(n), pos="num", english=_english_number(n),
                 display=number_words(n), context={"number": n})
            for n in number_set()]


def _read_movie(l: ListDef) -> list[Item]:
    """Words of a film, by frequency, with count, first appearance and an
    example cue index (the line text stays in the DB, never in git)."""
    return srt.word_items(l.path)


READERS = {
    "csv": _read_csv, "cils": _read_cils, "verbs": _read_verbs, "avere": _read_avere,
    "subtlex": _read_subtlex, "numbers": _read_numbers, "movie": _read_movie,
}


def read(l: ListDef) -> list[Item]:
    items = READERS[l.kind](l)
    for it in items:
        it.lemma = normalise_spelling(it.lemma)
        if it.pos not in ("phrase", "letter", "num") and it.lemma and it.lemma[0].isupper() and it.lemma[1:].islower():
            it.lemma = it.lemma.lower()
    return items


__all__ = ["ListDef", "Item", "load", "load_plan", "validate", "read", "plain"]
