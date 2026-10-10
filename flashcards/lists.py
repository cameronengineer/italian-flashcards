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
from pathlib import Path

from . import srt
from .italian import normalise_spelling, number_words, plain
from .paths import INPUTS_DIR, PROJECT_ROOT
from .domain import Item, ListDef

LISTS_PATH = PROJECT_ROOT / "lists.toml"
PLAN_PATH = PROJECT_ROOT / "plan.toml"

KINDS = {"csv", "cils", "verbs", "avere", "subtlex", "numbers", "movie"}
_ARTICLE = re.compile(r"^(?:(un'|l')(?=\w)|(gli|uno|una|il|lo|la|le|un|i)\s+(?=\w))", re.I)


# ── Manifest ───────────────────────────────────────────────────────────────


def load(path: Path = LISTS_PATH) -> list[ListDef]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    out = []
    known = {"id", "kind", "title", "deck", "path", "type", "pos", "facts", "recognition_only", "disabled"}
    for d in data.get("list", []):
        extra_keys = {
            "verb_limit",
            "noun_limit",
            "optional",
            "allow_empty",
            "shareable",
            "image_policy",
            "max_drop_ratio",
        }
        if set(d) - known - extra_keys:
            raise ValueError(f"lists.toml: unknown keys {sorted(set(d) - known - extra_keys)}")
        for key in ("facts", "recognition_only", "disabled", "optional", "allow_empty", "shareable"):
            if key in d and type(d[key]) is not bool:
                raise ValueError(f"lists.toml: {key} must be boolean")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", str(d.get("id", ""))):
            raise ValueError("lists.toml: id must be a lowercase slug")
        if d.get("path") and not (INPUTS_DIR / d["path"]).resolve().is_relative_to(INPUTS_DIR.resolve()):
            raise ValueError("lists.toml: input path must stay within inputs/")
        if d.get("image_policy", "optional") not in {"required", "optional", "disabled"}:
            raise ValueError("lists.toml: invalid image_policy")
        if d.get("disabled"):
            continue
        if d.get("kind") not in KINDS:
            raise ValueError(f"lists.toml: list {d.get('id')!r} has unknown kind {d.get('kind')!r}")
        out.append(
            ListDef(
                id=d["id"],
                kind=d["kind"],
                title=d.get("title", d["id"]),
                deck=d.get("deck") or f"Italian::{d.get('title', d['id'])}",
                path=(INPUTS_DIR / d["path"]) if d.get("path") else None,
                type=d.get("type"),
                pos=d.get("pos"),
                facts=bool(d.get("facts", True)),
                recognition_only=bool(d.get("recognition_only", False)),
                extras={k: v for k, v in d.items() if k not in known},
            )
        )
    ids = [l.id for l in out]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"lists.toml: duplicate list ids {sorted(dupes)}")
    return out


def load_plan(path: Path = PLAN_PATH) -> dict:
    from datetime import date

    plan = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    allowed = {"new_cards_per_day", "horizon_days", "priority", "conjugation", "cards"}
    if set(plan) - allowed:
        raise ValueError(f"plan.toml: unknown keys {sorted(set(plan) - allowed)}")
    for key in ("new_cards_per_day", "horizon_days"):
        if key in plan and (type(plan[key]) is not int or plan[key] <= 0):
            raise ValueError(f"plan.toml: {key} must be a positive integer")
    for table, keys in (
        ("cards", {"noun_phrases", "cognates", "patterns"}),
        ("conjugation", {"tenses", "full_forms_top", "max_full_verbs"}),
    ):
        if not isinstance(plan.get(table, {}), dict) or set(plan.get(table, {})) - keys:
            raise ValueError(f"plan.toml: invalid {table} settings")
    for key, value in plan.get("cards", {}).items():
        if type(value) is not bool:
            raise ValueError(f"plan.toml: cards.{key} must be boolean")
    from .grammar import TENSES

    conjugation = plan.get("conjugation", {})
    if any(t not in TENSES for t in conjugation.get("tenses", [])):
        raise ValueError("plan.toml: unknown conjugation tense")
    for key in ("full_forms_top", "max_full_verbs"):
        if key in conjugation and (type(conjugation[key]) is not int or conjugation[key] < 0):
            raise ValueError(f"plan.toml: {key} must be a nonnegative integer")
    seen = set()
    for p in plan.get("priority", []):
        if not isinstance(p, dict) or set(p) - {"list", "by"} or not p.get("list") or p["list"] in seen:
            raise ValueError("plan.toml: invalid or repeated priority")
        seen.add(p["list"])
        if p.get("by"):
            date.fromisoformat(str(p["by"]))
    return plan


def validate(lists: list[ListDef]) -> list[str]:
    errors = []
    for l in lists:
        needs_file = l.kind != "numbers" or l.path is not None
        if needs_file and (l.path is None or not l.path.exists()) and not l.extras.get("optional"):
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


def _detect_encoding(path: Path) -> str:
    try:
        path.read_bytes().decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "cp1252"


def _rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding=_detect_encoding(path)) as fh:
        reader = csv.DictReader(fh)
        headers = [(k or "").strip().lower().lstrip("\ufeff") for k in reader.fieldnames or []]
        if "italian" not in headers or "english" not in headers or len(set(headers)) != len(headers):
            raise ValueError(f"{path}: expected unique italian,english headers; got {headers}")
        rows = []
        for line, row in enumerate(reader, 2):
            if None in row or any(v is None for v in row.values()):
                raise ValueError(f"{path}:{line}: wrong number of columns")
            rows.append({k.strip().lower().lstrip("\ufeff"): v.strip() for k, v in row.items()})
        from .content_review import language_direction_warning
        from . import report

        for line, row in enumerate(rows, 2):
            if language_direction_warning(row.get("italian", ""), row.get("english", "")):
                report.line(
                    f"{path.name}:{line}: possible swapped language columns; review this row (mixed language is allowed)"
                )
        return rows


def split_article(text: str) -> tuple[str | None, str]:
    """('la', 'destra') for 'la destra'; (None, text) without an article."""
    text = text.replace("’", "'")
    m = _ARTICLE.match(text)
    if not m:
        return None, text
    return (m.group(1) or m.group(2)).lower(), text[m.end() :].strip()


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
    ("sostantivo", "noun"),
    ("s.m", "noun"),
    ("s.f", "noun"),
    ("aggettivo", "adj"),
    ("agg", "adj"),
    ("verbo", "verb"),
    ("v.t", "verb"),
    ("v.int", "verb"),
    ("v.rifl", "verb"),
    ("avverbio", "adv"),
    ("avv", "adv"),
    ("pronome", "pron"),
    ("pron", "pron"),
    ("interiezione", "intj"),
    ("inter", "intj"),
    ("congiunzione", "conj"),
    ("cong", "conj"),
    ("preposizione", "prep"),
    ("prep", "prep"),
    ("articolo", "article"),
    ("art", "article"),
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
        if l.pos == "letter":
            lemma = raw.split(" / ")[0].strip()
            out.append(
                Item(raw, lemma, "letter", english, display=raw, context={"variants": raw.split(" / ")})
            )
            continue
        if is_phrase(raw) and not l.pos:
            out.append(Item(raw=raw, lemma=raw, pos="phrase", english=english, display=raw))
            continue
        variant = _first_variant(raw)
        article, word = split_article(variant) if not l.pos or l.pos == "noun" else (None, variant)
        bare = word.strip("!¡?¿.,;: ")
        out.append(
            Item(
                raw=raw,
                lemma=bare,
                pos=l.pos or ("noun" if article else None),
                english=english,
                gender=article_gender(article),
                display=raw if l.pos in ("intj", "letter") else None,
                context={"variants": raw.split(" / ")} if "/" in raw else {},
            )
        )
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
        out.append(
            Item(
                raw=raw,
                lemma=lemma,
                pos=pos,
                english=r.get("english", ""),
                gender=article_gender(article),
                context={"cils_function": r.get("function", "")},
            )
        )
    return out


def _read_verbs(l: ListDef) -> list[Item]:
    return [
        Item(raw=r["italian"], lemma=r["italian"].strip().lower(), pos="verb", english=r.get("english", ""))
        for r in _rows(l.path)
        if r.get("italian")
    ]


def _read_avere(l: ListDef) -> list[Item]:
    return [
        Item(
            raw=r["italian"],
            lemma=r["italian"].strip(),
            pos="phrase",
            english=r.get("english", ""),
            display=r["italian"].strip(),
            context={"avere": True},
        )
        for r in _rows(l.path)
        if r.get("italian")
    ]


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
        out.append(
            Item(
                raw=lemma,
                lemma=lemma.lower(),
                pos="verb" if pos == "VER" else "noun",
                context={"freq_rank": file_rank, "zipf": _zipf(r.get("zipf"))},
            )
        )
    return out


def number_set(seed: int = 7) -> list[int]:
    """The numbers worth a card: 0–100, the shapes that teach the rules
    (101, 108, 180, round hundreds/thousands), plus a fixed random sample."""
    nums = list(range(0, 101))
    nums += [101, 102, 103, 108, 110, 111, 118, 123, 180, 199]
    nums += list(range(200, 1001, 100)) + [1001, 1100, 1200, 1500, 1999]
    nums += [
        2000,
        2001,
        2023,
        2026,
        3000,
        5000,
        10000,
        20000,
        21000,
        50000,
        100000,
        200000,
        500000,
        1_000_000,
        2_000_000,
        1_500_000,
    ]
    rng = random.Random(seed)
    for lo, hi, n in ((101, 999, 15), (1000, 9999, 12), (10000, 999999, 10)):
        nums += rng.sample(range(lo, hi), n)
    return sorted(set(nums))


def _english_number(n: int) -> str:
    return f"{n:,}"


def _read_numbers(l: ListDef) -> list[Item]:
    """Read the curated CSV in study order, keeping numeric identities stable.

    The source uses ``11 / eleven,undici``. Explicit ``num`` items avoid
    article stripping (``un milione``) and never need AI enrichment. Manifests
    without a path retain the older deterministic generated list.
    """
    if l.path is None:
        rows = [(n, number_words(n), _english_number(n)) for n in number_set()]
    else:
        rows, seen = [], set()
        for line, row in enumerate(_rows(l.path), 2):
            digits, separator, english = row["english"].partition(" / ")
            if not re.fullmatch(r"0|[1-9][0-9]*", digits) or not separator or not english.strip():
                raise ValueError(f"{l.path}:{line}: expected '11 / eleven' in english")
            n = int(digits)
            if n in seen:
                raise ValueError(f"{l.path}:{line}: repeated number {n}")
            italian = normalise_spelling(row["italian"])
            expected = number_words(n)
            if italian != expected:
                raise ValueError(f"{l.path}:{line}: use the canonical spelling {expected!r} for {n}")
            seen.add(n)
            rows.append((n, italian, f"{_english_number(n)} / {english}"))
    return [
        Item(
            raw=str(n),
            lemma=italian,
            pos="num",
            english=english,
            display=italian,
            context={"number": n},
        )
        for n, italian, english in rows
    ]


def _read_movie(l: ListDef) -> list[Item]:
    """Words of a film, by frequency, with count, first appearance and an
    example cue index (the line text stays in the DB, never in git)."""
    return srt.word_items(l.path)


READERS = {
    "csv": _read_csv,
    "cils": _read_cils,
    "verbs": _read_verbs,
    "avere": _read_avere,
    "subtlex": _read_subtlex,
    "numbers": _read_numbers,
    "movie": _read_movie,
}


def read(l: ListDef) -> list[Item]:
    if l.extras.get("optional") and (l.path is None or not l.path.exists()):
        return []
    items = READERS[l.kind](l)
    if not items and not l.extras.get("allow_empty"):
        raise ValueError(f"{l.id}: source is empty; set allow_empty=true for intentional removal")
    for index, it in enumerate(items, 1):
        it.context.setdefault("row", index)
        it.lemma = normalise_spelling(it.lemma)
        if (
            it.pos not in ("phrase", "letter", "num")
            and it.lemma
            and it.lemma[0].isupper()
            and it.lemma[1:].islower()
        ):
            it.lemma = it.lemma.lower()
    return items


__all__ = ["ListDef", "Item", "load", "load_plan", "validate", "read", "plain"]
