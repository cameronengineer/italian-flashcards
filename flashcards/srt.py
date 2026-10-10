"""Subtitles → words.

Parses an ``.srt`` file, cleans the cues (ads, tags, positioning codes,
speaker dashes, hearing-impaired brackets, music notes), tokenises them,
and lemmatises every token:

1. SUBTLEX-IT's wordform → dominant lemma map (frequency-aware, so *sono*
   → *essere*, not *sonare*);
2. the Kaikki form index (every inflected form Wiktionary knows);
3. clitic stripping for forms like *dimmelo* / *andiamoci*.

Character names are dropped (capitalised mid-sentence and never seen in
lowercase). The result is one :class:`lists.Item` per lemma with its count,
first appearance and an example cue — the cue text is stored only in the
local database (subtitle text is never committed).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import kaikki
from .italian import normalise_spelling, plain, tokens
from .paths import INPUTS_DIR

_TIME = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->")
_AD = re.compile(
    r"(yts|yify|opensubtitles|subtitles? by|sottotitoli (a cura|by)|scaricato da|www\.|\.com|\.mx|\.org)",
    re.I,
)
_CLEAN = [
    (re.compile(r"<[^>]+>"), " "),  # <i> … </i>
    (re.compile(r"\{\\[^}]*\}"), " "),  # {\an8}
    (re.compile(r"\[[^\]]*\]|\([^)]*\)"), " "),  # [ride] (sospira)
    (re.compile(r"[♪♫#]"), " "),
    (re.compile(r"^\s*[-–—]\s*", re.M), ""),  # speaker dashes
]


@dataclass
class Cue:
    index: int
    start_ms: int
    text: str


def parse(path: Path) -> list[Cue]:
    raw = path.read_text(encoding="utf-8-sig", errors="replace").replace("\r\n", "\n")
    cues = []
    for block in re.split(r"\n\s*\n", raw):
        lines = [l for l in block.strip().split("\n") if l.strip()]
        if len(lines) < 2:
            continue
        m = next((_TIME.search(l) for l in lines[:2] if _TIME.search(l)), None)
        if not m:
            continue
        h, mi, s, ms = map(int, m.groups())
        text = " ".join(l for l in lines if not _TIME.search(l) and not l.strip().isdigit())
        if _AD.search(text):
            continue
        for pattern, repl in _CLEAN:
            text = pattern.sub(repl, text)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            cues.append(Cue(len(cues), ((h * 60 + mi) * 60 + s) * 1000 + ms, text))
    return cues


def timestamp(ms: int) -> str:
    s = ms // 1000
    return f"{s // 3600:d}:{s % 3600 // 60:02d}:{s % 60:02d}"


# ── Lemmatisation ──────────────────────────────────────────────────────────

_SUBTLEX_POS = {
    "VER": "verb",
    "NOM": "noun",
    "ADJ": "adj",
    "ADV": "adv",
    "PRO": "pron",
    "PRE": "prep",
    "CON": "conj",
    "DET": "det",
    "ART": "article",
    "INT": "intj",
    "NUM": "num",
    "NPR": "name",
}
_KAIKKI_POS = {
    "noun": "noun",
    "verb": "verb",
    "adj": "adj",
    "adv": "adv",
    "pron": "pron",
    "conj": "conj",
    "prep": "prep",
    "intj": "intj",
    "num": "num",
    "det": "det",
    "article": "article",
    "name": "name",
    "prep_phrase": "phrase",
    "phrase": "phrase",
    "contraction": "prep",
    "particle": "adv",
}
_POS_PREF = ["verb", "noun", "adj", "adv", "pron", "prep", "conj", "article", "intj", "num", "phrase"]
_ELISION = {
    "l'": "il",
    "un'": "uno",
    "c'": "ci",
    "d'": "di",
    "m'": "mi",
    "t'": "ti",
    "s'": "si",
    "v'": "vi",
    "n'": "ne",
    "dell'": "di",
    "all'": "a",
    "dall'": "da",
    "nell'": "in",
    "sull'": "su",
    "quell'": "quello",
    "quest'": "questo",
    "nessun'": "nessuno",
    "buon'": "buono",
    "sant'": "santo",
    "dov'": "dove",
    "com'": "come",
    "cos'": "cosa",
    "po'": "poco",
    "anch'": "anche",
    "senz'": "senza",
}
_CLITICS = [
    "gliele",
    "glielo",
    "gliela",
    "glieli",
    "gliene",
    "melo",
    "mela",
    "meli",
    "mele",
    "telo",
    "tela",
    "selo",
    "sela",
    "celo",
    "velo",
    "mene",
    "tene",
    "sene",
    "cene",
    "vene",
    "gli",
    "lo",
    "la",
    "li",
    "le",
    "ne",
    "ci",
    "vi",
    "mi",
    "ti",
    "si",
]


@lru_cache(maxsize=1)
def subtlex_forms() -> dict[str, tuple[str, str]]:
    path = INPUTS_DIR / "freqdic" / "subtlex-it.cleaned.csv"
    out: dict[str, tuple[str, str]] = {}
    if not path.exists():
        return out
    import csv

    with path.open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh, delimiter=";"):
            form = (r.get("wordform") or "").lower()
            lemma = (r.get("dom_lemma") or "").strip()
            pos = _SUBTLEX_POS.get((r.get("dom_pos") or "").split(":")[0])
            if form and lemma and lemma != "<unknown>" and pos and form not in out:
                out[form] = (normalise_spelling(lemma.lower()), pos)
    return out


_ARTICLE_LEMMA = {
    "il": "il",
    "lo": "il",
    "la": "il",
    "l'": "il",
    "i": "il",
    "gli": "il",
    "le": "il",
    "un": "un",
    "uno": "un",
    "una": "un",
    "un'": "un",
}


def lemmatise(token: str) -> tuple[str, str] | None:
    """(lemma, pos) for one lowercase token, or None if unknown.

    Articles collapse to their dictionary entries: la/lo/gli… → il, una → un.
    """
    if token in _ARTICLE_LEMMA:
        return _ARTICLE_LEMMA[token], "article"
    got = _lemmatise(token)
    if got and got[1] == "article":
        return _ARTICLE_LEMMA.get(got[0], got[0]), "article"
    return got


def _lemmatise(token: str) -> tuple[str, str] | None:
    if token in _ELISION:
        lemma = _ELISION[token]
        return (
            (lemma, "article")
            if lemma in ("il", "uno")
            else (lemma, "prep" if lemma in ("di", "a", "da", "in", "su") else "pron")
        )
    hit = subtlex_forms().get(token)
    if hit and hit[1] != "name":
        return hit
    cands = [(l, _KAIKKI_POS.get(p)) for l, p, _t in kaikki.lemmas_for(token)]
    cands = [(l, p) for l, p in cands if p]
    if hit and hit[1] == "name":
        return hit
    if cands:
        self_lemma = [c for c in cands if plain(c[0]) == plain(token) and c[1] != "name"]
        pool = self_lemma or [c for c in cands if c[1] != "name"] or cands
        pool.sort(key=lambda c: _POS_PREF.index(c[1]) if c[1] in _POS_PREF else 99)
        return normalise_spelling(pool[0][0]), pool[0][1]
    for clitic in _CLITICS:  # dimmelo → dimme(lo) → dire …
        if token.endswith(clitic) and len(token) > len(clitic) + 2:
            stem = token[: -len(clitic)]
            for candidate in (
                stem,
                stem + "e",
                stem[:-1] + "e" if stem.endswith(("ar", "er", "ir")) else stem,
            ):
                got = _lemmatise(candidate) if candidate != token else None
                if got and got[1] == "verb":
                    return got
            break
    return None


# ── Items ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AnalyzedToken:
    text: str
    cue: int
    status: str
    lemma: str | None = None
    pos: str | None = None


def analyze(path: Path):
    """One token accounting policy for ingestion, coverage, and local datasets."""
    cues = parse(path)
    lowercase = set()
    capital_mid = set()
    for cue in cues:
        for i, tok in enumerate(tokens(cue.text)):
            if tok[:1].islower():
                lowercase.add(tok.lower())
            elif i > 0 and tok[:1].isupper():
                capital_mid.add(tok.lower())
    out = []
    for cue in cues:
        for tok in tokens(cue.text):
            if tok.isdigit():
                out.append(AnalyzedToken(tok, cue.index, "ignored_number"))
                continue
            low = tok.lower()
            got = lemmatise(low)
            if (low in capital_mid and low not in lowercase) or (got and got[1] == "name"):
                out.append(AnalyzedToken(tok, cue.index, "excluded_name"))
                continue
            out.append(
                AnalyzedToken(tok, cue.index, "resolved" if got else "unresolved", *(got or (None, None)))
            )
    return cues, out


def word_items(path: Path) -> list:
    from .domain import Item

    cues, stream = analyze(path)
    counts = Counter()
    first = {}
    example = {}
    forms_seen = defaultdict(Counter)
    for token in stream:
        if token.status != "resolved":
            continue
        key = (token.lemma, token.pos)
        counts[key] += 1
        forms_seen[key][token.text.lower()] += 1
        first.setdefault(key, token.cue)
        previous = example.get(key)
        n = len(tokens(cues[token.cue].text))
        if previous is None or (6 <= n <= 14 and not 6 <= len(tokens(cues[previous].text)) <= 14):
            example[key] = token.cue
    items = []
    for (lemma, pos), n in sorted(counts.items(), key=lambda kv: (-kv[1], first[kv[0]], kv[0])):
        ex = cues[example[(lemma, pos)]]
        items.append(
            Item(
                raw=lemma,
                lemma=lemma,
                pos=pos,
                context={
                    "count": n,
                    "first_seen": timestamp(cues[first[(lemma, pos)]].start_ms),
                    "example": ex.text,
                    "example_at": timestamp(ex.start_ms),
                    "forms": [f for f, _ in forms_seen[(lemma, pos)].most_common(4)],
                },
            )
        )
    return items
