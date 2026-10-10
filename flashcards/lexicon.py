"""The lexicon: one record per root word, built from lists + Kaikki + rules.

``sync()`` reads every list in ``lists.toml``, resolves each row to a
lexeme (lemma + part of speech), fills everything a dictionary or a rule
knows (gender, plural, adjective forms, conjugation table, auxiliary, IPA,
Commons audio, etymology, frequency, cognate rule) and records which lists
each lexeme belongs to. Claude never runs here — it is queued afterwards
(``flashcards.queue``) to write prompts, verify, and add facts.

Facts and images belong to the root: every card of a lexeme (its forms,
phrases …) shows the root's image. Existing images are reused by content
hash; nothing is ever deleted.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
import hashlib
import io
import csv
from dataclasses import asdict
from uuid import uuid4
import sqlite3
from functools import lru_cache
from pathlib import Path

from . import kaikki, lists as lists_mod, overrides, report
from .db import transaction
from .runtime import atomic_text, atomic_json
from .facts import normalise as fact_key
from .italian import cognate, definite_article, normalise_spelling, plain, with_article
from .paths import INPUTS_DIR, LEXICON_DIR
from .util import md5_hex, print_banner

#: Our POS names ← Kaikki's.
_POS_FROM_KAIKKI = {
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
    "phrase": "phrase",
    "prep_phrase": "phrase",
    "proverb": "phrase",
    "contraction": "prep",
    "particle": "adv",
    "name": "name",
    "character": "letter",
}
#: Our POS → the Kaikki POS to look up (explicit: inverting the map above
#: would silently pick e.g. "contraction" for "prep").
_KAIKKI_FOR = {
    "noun": "noun",
    "verb": "verb",
    "adj": "adj",
    "adv": "adv",
    "pron": "pron",
    "conj": "conj",
    "prep": "prep",
    "intj": "intj",
    "num": "num",
    "article": "article",
    "phrase": "phrase",
    "name": "name",
    "letter": "character",
    "det": "det",
}


def lexeme_id(lemma: str, pos: str) -> str:
    return md5_hex(f"lex::{lemma}::{pos}")


# ── Reference data ─────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def frequency() -> dict[str, tuple[int, float | None]]:
    """lemma → (rank, zipf) from SUBTLEX (first = most frequent wordform)."""
    path = INPUTS_DIR / "freqdic" / "subtlex-it.cleaned.csv"
    out: dict[str, tuple[int, float | None]] = {}
    if not path.exists():
        return out
    for rank, r in enumerate(lists_mod.subtlex_rows(path), start=1):
        lemma = normalise_spelling((r.get("dom_lemma") or "").strip().lower())
        if lemma and lemma != "<unknown>" and lemma not in out:
            out[lemma] = (rank, lists_mod._zipf(r.get("zipf")))
    return out


def existing_image(*texts: str | None) -> str | None:
    """The first text whose image already exists on disk (by content hash)."""
    from .assets import image_for

    for t in texts:
        if not t or not t.strip():
            continue
        if image_for(t.strip()):
            return t.strip()
    return None


def image_exists(key: str | None) -> bool:
    return bool(key) and existing_image(key) is not None


# ── Resolution ─────────────────────────────────────────────────────────────


def _pick_entry(item: lists_mod.Item) -> dict | None:
    if not kaikki.available() or not item.lemma:
        return None
    want = _KAIKKI_FOR.get(item.pos or "")
    if item.pos == "phrase":
        found = kaikki.entries(item.lemma.strip("!?.… "))
        return found[0] if found else None
    found = kaikki.entries(item.lemma, want) if want else []
    if not found and want == "article":
        found = kaikki.entries(item.lemma, "det")
    if not found and item.pos:
        # Never fall back to a different part of speech when the list says
        # what it is (the letter "di" is not the preposition "di").
        found = [e for e in kaikki.entries(item.lemma) if _POS_FROM_KAIKKI.get(e.get("pos")) == item.pos]
    if not found and not item.pos:
        found = [e for e in kaikki.entries(item.lemma) if e.get("pos") not in ("name", "character")]
    if not found and " " not in item.lemma:
        # An inflected form in the list (CILS "tante") → its lemma ("tanto").
        for lemma, pos, _tags in kaikki.lemmas_for(item.lemma):
            if want and pos != want:
                continue
            found = kaikki.entries(lemma, pos)
            if found:
                break
    if not found:
        return None
    evidence = set(re.findall(r"[a-z]+", (item.english or "").lower())) - {"a", "the", "to", "of", "and"}

    def score(entry):
        words = set(re.findall(r"[a-z]+", " ".join(kaikki.glosses(entry)).lower()))
        return len(evidence & words)

    return max(found, key=score)


def _resolve(item: lists_mod.Item, l: lists_mod.ListDef) -> dict:
    """Lexeme record (dict of columns) for one list row."""
    entry = _pick_entry(item)
    if not entry and not item.pos and " " in item.lemma.strip():
        item.pos = "phrase"
        item.display = item.display or item.raw
    pos = item.pos or (_POS_FROM_KAIKKI.get(entry["pos"]) if entry else None) or "word"
    if entry and pos not in ("phrase", "num", "letter"):
        pos = _POS_FROM_KAIKKI.get(entry["pos"], pos) if not item.pos else pos
    lemma = (
        normalise_spelling(item.lemma.strip())
        if pos in ("phrase", "letter", "num")
        else normalise_spelling((entry["word"] if entry else item.lemma).strip())
    )
    if pos not in ("phrase", "letter") and lemma[:1].isupper() and lemma[1:].islower():
        lemma = lemma.lower()
    rec: dict = {
        "id": lexeme_id(lemma, pos),
        "lemma": lemma,
        "pos": pos,
        "in_kaikki": 1 if entry else 0,
        "gender": None,
        "plural": None,
        "forms_json": None,
        "irregular": 0,
        "ipa": None,
        "commons_audio": None,
        "etymology": None,
        "display": item.display or lemma,
    }
    prov: dict = {}
    if entry:
        rec["ipa"] = kaikki.ipa(entry)
        rec["commons_audio"] = kaikki.audio_url(entry)
        rec["etymology"] = entry.get("etymology_text")
        prov.update({k: "kaikki" for k in ("ipa", "etymology") if rec[k]})
        if pos == "noun":
            rec["gender"] = kaikki.gender(entry)
            rec["plural"] = kaikki.plural(entry)
            rec["forms_json"] = json.dumps({"noun_forms": kaikki.noun_forms(entry)}, ensure_ascii=False)
        elif pos == "adj":
            rec["forms_json"] = json.dumps(kaikki.adjective_forms(entry), ensure_ascii=False)
        elif pos == "verb":
            info = kaikki.verb_info(entry)
            rec["forms_json"] = json.dumps(info, ensure_ascii=False)
            rec["irregular"] = 1 if info.get("irregular") else 0
    if pos == "noun":
        rec["gender"] = rec["gender"] or item.gender
        if rec["gender"] in ("masculine", "feminine"):
            rec["display"] = with_article(definite_article(lemma, rec["gender"]), lemma)
        elif item.gender:
            rec["display"] = with_article(definite_article(lemma, item.gender), lemma)
        prov["gender"] = "kaikki" if entry and kaikki.gender(entry) else ("list" if item.gender else None)
    rank, zipf = frequency().get(lemma.lower(), (item.context.get("freq_rank"), item.context.get("zipf")))
    rec["freq_rank"], rec["zipf"] = rank, zipf
    gloss = (kaikki.glosses(entry)[0] if entry and kaikki.glosses(entry) else item.english) or ""
    first_word = gloss.split(",")[0].split(";")[0].removeprefix("to ").strip().split(" ")[0]
    if first_word and pos not in ("phrase", "letter", "num"):
        score, rule = cognate(lemma, first_word)
        if score >= 0.88 and score < 1.0:
            rec["cognate_rule"], rec["cognate_score"] = rule or "similar spelling", round(score, 3)
    rec["provenance"] = json.dumps({k: v for k, v in prov.items() if v}, sort_keys=True)
    return rec


# ── Sync ───────────────────────────────────────────────────────────────────

_LEXEME_COLS = [
    "id",
    "lemma",
    "pos",
    "display",
    "gender",
    "plural",
    "forms_json",
    "irregular",
    "ipa",
    "commons_audio",
    "etymology",
    "in_kaikki",
    "zipf",
    "freq_rank",
    "cognate_rule",
    "cognate_score",
    "provenance",
]


def sync(conn: sqlite3.Connection) -> dict:
    """Stage every source before atomically publishing memberships and observations."""
    if report.VERBOSE:
        print_banner("lexicon — resolve lists to root words")
    defs = lists_mod.load()
    errors = lists_mod.validate(defs)
    if errors:
        raise ValueError("lists.toml / plan.toml errors: " + "; ".join(errors))
    if not kaikki.available():
        raise ValueError("Kaikki index missing — run `flashcards kaikki import` first.")
    staged = []
    generation = uuid4().hex
    # One raw spelling can be several roots (movie "molto" adv + det), so compare sets.
    old: dict[tuple[str, str], set[str]] = defaultdict(set)
    for r in conn.execute("SELECT list_id,raw,lexeme_id FROM list_items"):
        old[(r["list_id"], r["raw"])].add(r["lexeme_id"])
    for l in defs:
        if l.extras.get("optional") and (not l.path or not l.path.exists()):
            continue
        items = lists_mod.read(l)
        previous = sum(len(ids) for (lid, _raw), ids in old.items() if lid == l.id)
        minimum = 1 - float(l.extras.get("max_drop_ratio", 0.5))
        if previous >= 20 and len(items) < previous * minimum and not l.extras.get("allow_empty"):
            raise ValueError(
                f"{l.id}: source shrank from {previous} to {len(items)}; inspect before setting allow_empty=true"
            )
        source_hash = (
            hashlib.sha256(l.path.read_bytes()).hexdigest()
            if l.path
            else md5_hex(json.dumps(asdict(l), default=str, sort_keys=True))
        )
        staged.append((l, [(it, _resolve(it, l)) for it in items if it.lemma], source_hash))
    stats, repairs = {}, []
    raw_to_lexeme = {}
    with transaction(conn):
        human = {(r[0], r[1]) for r in conn.execute("SELECT lexeme_id, field FROM overrides")}
        # Lists taken out of lists.toml (an optional list whose file is missing is not "removed").
        old_list_paths = {
            r["id"]: json.loads(r["settings"] or "{}").get("path")
            for r in conn.execute("SELECT id, settings FROM lists")
        }
        removed = sorted(set(old_list_paths) - {l.id for l in defs})
        conn.execute("DELETE FROM source_observations")
        conn.execute("DELETE FROM list_items")
        conn.execute("DELETE FROM lists")
        for l, items, source_hash in staged:
            source = str(l.path.relative_to(INPUTS_DIR)) if l.path else l.id
            conn.execute(
                "INSERT INTO lists(id,kind,title,deck,settings) VALUES(?,?,?,?,?)",
                (
                    l.id,
                    l.kind,
                    l.title,
                    l.deck,
                    json.dumps(
                        {
                            "type": l.type,
                            "facts": l.facts,
                            "recognition_only": l.recognition_only,
                            "path": source,
                            **l.extras,
                        }
                    ),
                ),
            )
            seen, n_new = set(), 0
            for rank, (item, rec) in enumerate(items, 1):
                lid = rec["id"]
                previous = conn.execute("SELECT * FROM lexemes WHERE id=?", (lid,)).fetchone()
                if previous is None:
                    n_new += 1
                    conn.execute(
                        f"INSERT INTO lexemes ({', '.join(_LEXEME_COLS)}) VALUES ({', '.join('?' * len(_LEXEME_COLS))})",
                        tuple(rec.get(k) for k in _LEXEME_COLS),
                    )
                else:
                    prov = json.loads(previous["provenance"] or "{}")
                    cols = [
                        c
                        for c in _LEXEME_COLS[3:]
                        if c != "provenance"
                        and prov.get(c) != "human"
                        and (lid, "forms" if c == "forms_json" else c) not in human
                    ]
                    if cols:
                        conn.execute(
                            f"UPDATE lexemes SET {', '.join(f'{c}=?' for c in cols)} WHERE id=?",
                            (*[rec.get(c) for c in cols], lid),
                        )
                    merged = {**json.loads(rec["provenance"] or "{}"), **prov}
                    conn.execute(
                        "UPDATE lexemes SET provenance=? WHERE id=?",
                        (json.dumps(merged, sort_keys=True), lid),
                    )
                conn.execute(
                    "INSERT INTO source_observations VALUES(?,?,?,?,?,?,?)",
                    (
                        l.id,
                        str(item.context.get("row", rank)),
                        lid,
                        item.raw,
                        json.dumps(asdict(item), ensure_ascii=False),
                        source_hash,
                        generation,
                    ),
                )
                if lid not in seen:
                    conn.execute(
                        "INSERT INTO list_items(list_id,lexeme_id,rank,raw,hint,context) VALUES(?,?,?,?,?,?)",
                        (
                            l.id,
                            lid,
                            rank,
                            item.raw,
                            item.english or None,
                            json.dumps(item.context, ensure_ascii=False),
                        ),
                    )
                    seen.add(lid)
                elif l.kind == "movie":
                    # Several analyzed lemmas can resolve to the same root.
                    # Preserve their total token mass in the deduplicated list.
                    prior_context = json.loads(
                        conn.execute(
                            "SELECT context FROM list_items WHERE list_id=? AND lexeme_id=?", (l.id, lid)
                        ).fetchone()[0]
                        or "{}"
                    )
                    prior_context["count"] = prior_context.get("count", 0) + item.context.get("count", 0)
                    prior_context["forms"] = list(
                        dict.fromkeys(prior_context.get("forms", []) + item.context.get("forms", []))
                    )
                    times = [
                        t for t in (prior_context.get("first_seen"), item.context.get("first_seen")) if t
                    ]
                    if times:
                        prior_context["first_seen"] = min(times, key=lambda t: tuple(map(int, t.split(":"))))
                    conn.execute(
                        "UPDATE list_items SET context=? WHERE list_id=? AND lexeme_id=?",
                        (json.dumps(prior_context, ensure_ascii=False), l.id, lid),
                    )
                raw_to_lexeme[(source, item.raw.strip().lower())] = lid
                raw_to_lexeme.setdefault((source, rec["lemma"].lower()), lid)
                prior = old.get((l.id, item.raw), set())
                if prior and lid not in prior:
                    for was in sorted(prior):
                        repairs.append(
                            {
                                "list": l.id,
                                "raw": item.raw,
                                "old": was,
                                "new": lid,
                                "lemma": rec["lemma"],
                                "pos": rec["pos"],
                            }
                        )
            stats[l.id] = {"items": len(seen), "new_lexemes": n_new}
            report.detail(f"  {l.id:<18} {len(seen):>5} words (+{n_new} new)")
        # Only unambiguous one-to-one identity changes may acquire an alias.
        destinations = {}
        for repair in repairs:
            destinations.setdefault(repair["old"], set()).add(repair["new"])
        for old_id, targets in destinations.items():
            active = conn.execute("SELECT 1 FROM list_items WHERE lexeme_id=?", (old_id,)).fetchone()
            if len(targets) != 1 or active:
                continue  # split identities require review; never guess scheduling transfer
            target = next(iter(targets))
            for ct in ("vocab", "phrase"):
                old_key = f"{ct}:{old_id}:0"
                target_pos = conn.execute("SELECT pos FROM lexemes WHERE id=?", (target,)).fetchone()[0]
                new_key = f"{'phrase' if target_pos == 'phrase' else 'vocab'}:{target}:0"
                conn.execute(
                    "INSERT OR IGNORE INTO identity_aliases VALUES(?,?,?,CURRENT_TIMESTAMP)",
                    (old_key, new_key, "input normalization repair"),
                )
            for o in conn.execute("SELECT * FROM overrides WHERE lexeme_id=?", (old_id,)).fetchall():
                if not conn.execute(
                    "SELECT 1 FROM overrides WHERE lexeme_id=? AND field=?", (target, o["field"])
                ).fetchone():
                    overrides.set_override(
                        conn,
                        target,
                        o["field"],
                        json.loads(o["value_json"]),
                        reason="migrated input identity; review required",
                    )
        if removed:
            stats["_removed"] = {
                "lists": removed,
                "cards": _retire_removed_lists(conn, removed, old_list_paths),
            }
        # Repaired targets keep their status: new roots are verified by Claude
        # like any other, and correct existing roots must not be demoted.
        conn.execute("DELETE FROM entry_lexeme")
        _map_legacy(conn, raw_to_lexeme, commit=False)
        overrides.apply(conn)
        conn.execute("INSERT OR REPLACE INTO metadata VALUES('source_generation',?)", (generation,))
        conn.execute("INSERT OR REPLACE INTO metadata VALUES('dictionary_version',?)", (kaikki.version(),))
        conn.execute("DELETE FROM metadata WHERE key='notes_generation'")
    _assign_images(conn)
    from .paths import PROJECT_ROOT

    atomic_json(
        PROJECT_ROOT / "audit_reports" / "input_repairs_latest.json",
        {"generation": generation, "repairs": repairs},
    )
    report.detail(f"  {len(repairs)} source identity changes recorded; existing media retained")
    return stats


_RESOLVER_SOURCES = (
    "lexicon.py",
    "lists.py",
    "srt.py",
    "italian.py",
    "kaikki.py",
    "domain.py",
    "overrides.py",
)


def inputs_signature() -> str:
    """Everything the lexicon is computed from: lists.toml, each list's source
    file, the frequency list, the dictionary, and the resolver code itself."""
    h = hashlib.sha256(lists_mod.LISTS_PATH.read_bytes())
    for l in lists_mod.load():
        if l.path and l.path.exists():
            h.update(str(l.path.relative_to(INPUTS_DIR)).encode())
            h.update(hashlib.sha256(l.path.read_bytes()).digest())
    freq = INPUTS_DIR / "freqdic" / "subtlex-it.cleaned.csv"
    if freq.exists():
        h.update(hashlib.sha256(freq.read_bytes()).digest())
    h.update(kaikki.version().encode())
    for name in _RESOLVER_SOURCES:
        h.update((Path(__file__).parent / name).read_bytes())
    return h.hexdigest()


def sync_if_changed(conn: sqlite3.Connection) -> dict | None:
    """``sync`` unless nothing it depends on changed since the last one (returns None then)."""
    signature = inputs_signature()
    row = conn.execute("SELECT value FROM metadata WHERE key='lexicon_inputs'").fetchone()
    if row and row[0] == signature and conn.execute("SELECT 1 FROM list_items LIMIT 1").fetchone():
        return None
    stats = sync(conn)
    conn.execute("INSERT OR REPLACE INTO metadata VALUES('lexicon_inputs',?)", (signature,))
    conn.commit()
    return stats


def _retire_removed_lists(conn: sqlite3.Connection, removed: list[str], paths: dict) -> int:
    """Mark the Anki notes of removed lists for retirement; returns how many.

    Covers v4 notes whose root no longer belongs to any list, and v3 notes made
    from the removed lists' files. The Anki sync then deletes only the
    unstudied ones, after re-checking each; studied notes always stay.
    """
    reason = "list removed from lists.toml: " + ", ".join(removed)
    still = {r[0] for r in conn.execute("SELECT DISTINCT lexeme_id FROM list_items")}
    keys = [
        key
        for (key,) in conn.execute("SELECT key FROM identity WHERE kind='v4'")
        if key.split(":")[0] in ("vocab", "phrase", "form", "nphrase") and key.split(":")[1] not in still
    ]
    files = [paths[i] for i in removed if paths.get(i)]
    if files:
        marks = ",".join("?" * len(files))
        keys += [
            r[0]
            for r in conn.execute(
                "SELECT c.natural_key || '|' || c.direction FROM cards c JOIN entries e ON e.id = c.entry_id "
                f"WHERE e.source_path IN ({marks})",
                files,
            )
        ]
    conn.executemany(
        "INSERT OR IGNORE INTO retirements(key, reason) VALUES(?, ?)", [(k, reason) for k in keys]
    )
    report.detail(f"  removed lists {removed}: {len(keys)} Anki notes marked for retirement (unstudied only)")
    return len(keys)


def _map_legacy(conn: sqlite3.Connection, raw_to_lexeme: dict, *, commit=True) -> None:
    """Link v3 entries to lexemes so their studied notes can be adopted."""
    rows = conn.execute("SELECT id, source_path, natural_id, italian FROM entries").fetchall()
    mapped = 0
    for r in rows:
        lex = raw_to_lexeme.get(
            (r["source_path"], (r["natural_id"] or "").strip().lower())
        ) or raw_to_lexeme.get((r["source_path"], (r["italian"] or "").strip().lower()))
        if lex:
            conn.execute(
                "INSERT OR REPLACE INTO entry_lexeme (entry_id, lexeme_id) VALUES (?, ?)", (r["id"], lex)
            )
            mapped += 1
    if commit:
        conn.commit()
    report.detail(f"  linked {mapped} of {len(rows)} legacy entries to lexemes")


def _assign_images(conn: sqlite3.Connection) -> None:
    """Reuse an existing image per root where one exists; never delete."""
    legacy = {}
    for r in conn.execute(
        """
        SELECT el.lexeme_id, e.italian, e.infinitive, e.singular
        FROM entry_lexeme el JOIN entries e ON e.id = el.entry_id
        """
    ):
        legacy.setdefault(r["lexeme_id"], []).extend([r["italian"], r["infinitive"], r["singular"]])
    have = 0
    for r in conn.execute("SELECT id, lemma, display, image_key FROM lexemes").fetchall():
        if r["image_key"] and image_exists(r["image_key"]):
            have += 1
            continue
        key = existing_image(r["display"], r["lemma"], *legacy.get(r["id"], []))
        conn.execute("UPDATE lexemes SET image_key = ? WHERE id = ?", (key or r["lemma"], r["id"]))
        have += bool(key)
    conn.commit()
    report.detail(f"  images: {have} roots reuse an existing image")


# ── Git export ─────────────────────────────────────────────────────────────


#: Exported text sharing this many consecutive words with a film line is left out.
_QUOTE_WORDS = 5


def _ngrams(text: str) -> set[tuple[str, ...]]:
    words = re.findall(r"\w+", (text or "").casefold())
    return {tuple(words[i : i + _QUOTE_WORDS]) for i in range(len(words) - _QUOTE_WORDS + 1)}


def _film_ngrams(conn: sqlite3.Connection) -> set[tuple[str, ...]]:
    """Word sequences of every film line known to the database."""
    grams: set[tuple[str, ...]] = set()
    for (ctx,) in conn.execute("SELECT context FROM list_items WHERE context IS NOT NULL"):
        grams |= _ngrams(json.loads(ctx).get("example") or "")
    for (payload,) in conn.execute("SELECT payload FROM source_observations"):
        grams |= _ngrams((json.loads(payload).get("context") or {}).get("example") or "")
    return grams


def export_jsonl(conn: sqlite3.Connection) -> Path:
    """lexicon/lexemes.jsonl + lexicon/lists.jsonl (sorted, diff-friendly).

    Movie example lines are deliberately left out (subtitle text stays
    private); everything else needed to rebuild the content is here.
    """
    LEXICON_DIR.mkdir(exist_ok=True)
    # Subtitle text never goes to git, including where AI text quotes a film line.
    film = _film_ngrams(conn)

    def private(text) -> bool:
        return bool(film and text and _ngrams(text) & film)

    senses: dict[str, list] = {}
    for s in conn.execute("SELECT * FROM senses WHERE active=1 ORDER BY lexeme_id, idx"):
        senses.setdefault(s["lexeme_id"], []).append(
            {
                "idx": s["idx"],
                "features": json.loads(s["features"] or "{}"),
                **{
                    k: s[k]
                    for k in ("prompt", "hint", "register", "note", "also", "provenance", "source_id")
                    if s[k] and not private(s[k])
                },
            }
        )
    facts = {
        r["word"]: r
        for r in conn.execute("SELECT * FROM word_facts WHERE has_fact = 1")
        if not private(r["fact"])
    }
    out = LEXICON_DIR / "lexemes.jsonl"
    with io.StringIO() as fh:
        for r in conn.execute(
            "SELECT * FROM lexemes WHERE id IN (SELECT lexeme_id FROM list_items) ORDER BY lemma, pos"
        ):
            f = facts.get(fact_key(r["lemma"]))
            rec = {
                "id": r["id"],
                "lemma": r["lemma"],
                "pos": r["pos"],
                "display": r["display"],
                "gender": r["gender"],
                "plural": r["plural"],
                "english_plural": r["english_plural"],
                "forms": json.loads(r["forms_json"]) if r["forms_json"] else None,
                "irregular": bool(r["irregular"]),
                "ipa": r["ipa"],
                "etymology": r["etymology"],
                "freq_rank": r["freq_rank"],
                "cognate_rule": r["cognate_rule"],
                "senses": senses.get(r["id"]),
                "fact": {"kind": f["kind"], "text": f["fact"], "confidence": f["confidence"]} if f else None,
                "status": r["status"],
                "provenance": json.loads(r["provenance"] or "{}"),
            }
            fh.write(
                json.dumps(
                    {k: v for k, v in rec.items() if v not in (None, [], {})},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
        atomic_text(out, fh.getvalue())
    with io.StringIO() as fh:
        for r in conn.execute(
            """SELECT li.list_id, li.rank, li.raw, li.hint, l.lemma, l.pos, li.context
               FROM list_items li JOIN lexemes l ON l.id = li.lexeme_id ORDER BY li.list_id, li.rank"""
        ):
            ctx = json.loads(r["context"]) if r["context"] else {}
            ctx.pop("example", None)  # subtitle text never leaves the local DB
            rec = {
                "list": r["list_id"],
                "rank": r["rank"],
                "raw": r["raw"],
                "hint": r["hint"],
                "lemma": r["lemma"],
                "pos": r["pos"],
                "context": ctx or None,
            }
            fh.write(
                json.dumps(
                    {k: v for k, v in rec.items() if v is not None}, ensure_ascii=False, sort_keys=True
                )
                + "\n"
            )
        atomic_text(LEXICON_DIR / "lists.jsonl", fh.getvalue())
    buf = io.StringIO()
    writer = csv.writer(buf)
    columns = ("key", "guid", "kind", "first_seen", "note_id", "profile", "card_ids")
    writer.writerow(columns)
    for row in conn.execute("SELECT * FROM identity ORDER BY key"):
        writer.writerow([row[c] for c in columns])
    atomic_text(LEXICON_DIR / "identity.csv", buf.getvalue())
    return out


def import_human_edits(conn: sqlite3.Connection) -> int:
    path = LEXICON_DIR / "lexemes.jsonl"
    if not path.exists():
        return 0
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    n = 0
    with transaction(conn):
        for rec in records:
            human = [k for k, v in (rec.get("provenance") or {}).items() if v == "human"]
            for field in human:
                if not conn.execute("SELECT 1 FROM lexemes WHERE id=?", (rec["id"],)).fetchone():
                    raise ValueError(
                        f"Human override references missing lexeme {rec['id']}; restore the authoritative database first"
                    )
                overrides.set_override(conn, rec["id"], field, rec.get(field))
                n += 1
        overrides.apply(conn)
        if n:
            conn.execute("DELETE FROM metadata WHERE key='notes_generation'")
    return n


__all__ = ["sync", "export_jsonl", "import_human_edits", "lexeme_id", "frequency", "image_exists", "plain"]
