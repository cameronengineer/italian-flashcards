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
import sqlite3
from functools import lru_cache
from pathlib import Path

from . import kaikki, lists as lists_mod
from .facts import normalise as fact_key
from .italian import cognate, definite_article, normalise_spelling, plain, with_article
from .paths import (
    IMAGE_DIR, IMAGE_DIR_COMPRESSED, INPUTS_DIR, LEXICON_DIR,
)
from .util import md5_hex, print_banner

#: Our POS names ← Kaikki's.
_POS_FROM_KAIKKI = {
    "noun": "noun", "verb": "verb", "adj": "adj", "adv": "adv", "pron": "pron",
    "conj": "conj", "prep": "prep", "intj": "intj", "num": "num", "det": "article",
    "article": "article", "phrase": "phrase", "prep_phrase": "phrase", "proverb": "phrase",
    "contraction": "prep", "particle": "adv", "name": "name", "character": "letter",
}
#: Our POS → the Kaikki POS to look up (explicit: inverting the map above
#: would silently pick e.g. "contraction" for "prep").
_KAIKKI_FOR = {
    "noun": "noun", "verb": "verb", "adj": "adj", "adv": "adv", "pron": "pron",
    "conj": "conj", "prep": "prep", "intj": "intj", "num": "num", "article": "article",
    "phrase": "phrase", "name": "name", "letter": "character",
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
    for t in texts:
        if not t or not t.strip():
            continue
        h = md5_hex(t.strip())
        if (IMAGE_DIR_COMPRESSED / f"{h}.jpg").exists() or (IMAGE_DIR / f"{h}.png").exists():
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
        found = [e for e in kaikki.entries(item.lemma)
                 if _POS_FROM_KAIKKI.get(e.get("pos")) == item.pos]
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
    return found[0] if found else None


def _resolve(item: lists_mod.Item, l: lists_mod.ListDef) -> dict:
    """Lexeme record (dict of columns) for one list row."""
    entry = _pick_entry(item)
    if not entry and not item.pos and " " in item.lemma.strip():
        item.pos = "phrase"
        item.display = item.display or item.raw
    pos = item.pos or (_POS_FROM_KAIKKI.get(entry["pos"]) if entry else None) or "word"
    if entry and pos not in ("phrase", "num", "letter"):
        pos = _POS_FROM_KAIKKI.get(entry["pos"], pos) if not item.pos else pos
    lemma = normalise_spelling(item.lemma.strip()) if pos in ("phrase", "letter", "num") else \
        normalise_spelling((entry["word"] if entry else item.lemma).strip())
    if pos not in ("phrase", "letter") and lemma[:1].isupper() and lemma[1:].islower():
        lemma = lemma.lower()
    rec: dict = {
        "id": lexeme_id(lemma, pos), "lemma": lemma, "pos": pos,
        "in_kaikki": 1 if entry else 0, "gender": None, "plural": None,
        "forms_json": None, "irregular": 0, "ipa": None, "commons_audio": None,
        "etymology": None, "display": item.display or lemma,
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
    "id", "lemma", "pos", "display", "gender", "plural", "forms_json", "irregular", "ipa",
    "commons_audio", "etymology", "in_kaikki", "zipf", "freq_rank", "cognate_rule",
    "cognate_score", "provenance",
]


def sync(conn: sqlite3.Connection) -> dict:
    """Rebuild lists + list_items from lists.toml; upsert lexemes."""
    print_banner("lexicon — resolve lists to root words")
    defs = lists_mod.load()
    errors = lists_mod.validate(defs)
    if errors:
        raise SystemExit("lists.toml / plan.toml errors:\n  - " + "\n  - ".join(errors))
    if not kaikki.available():
        raise SystemExit("Kaikki index missing — run `flashcards kaikki import` first.")
    stats = {}
    raw_to_lexeme: dict[tuple[str, str], str] = {}
    conn.execute("DELETE FROM lists")
    for l in defs:
        items = lists_mod.read(l)
        conn.execute(
            "INSERT INTO lists (id, kind, title, deck, settings) VALUES (?, ?, ?, ?, ?)",
            (l.id, l.kind, l.title, l.deck, json.dumps({
                "type": l.type, "facts": l.facts, "recognition_only": l.recognition_only,
                "path": str(l.path.relative_to(INPUTS_DIR)) if l.path else None, **l.extras,
            })),
        )
        conn.execute("DELETE FROM list_items WHERE list_id = ?", (l.id,))
        n_new = 0
        seen: set[str] = set()
        for rank, item in enumerate(items, start=1):
            if not item.lemma:
                continue
            rec = _resolve(item, l)
            if rec["id"] in seen:
                continue
            seen.add(rec["id"])
            existed = conn.execute("SELECT 1 FROM lexemes WHERE id = ?", (rec["id"],)).fetchone()
            if not existed:
                n_new += 1
                conn.execute(
                    f"INSERT INTO lexemes ({', '.join(_LEXEME_COLS)}) VALUES ({', '.join('?' * len(_LEXEME_COLS))})",
                    tuple(rec[c] if c in rec else None for c in _LEXEME_COLS),
                )
            else:
                # Deterministic fields refresh from Kaikki unless a human edited them.
                prov = json.loads(conn.execute("SELECT provenance FROM lexemes WHERE id = ?", (rec["id"],)).fetchone()[0] or "{}")
                cols = [c for c in _LEXEME_COLS[3:] if prov.get(c) != "human"]
                conn.execute(
                    f"UPDATE lexemes SET {', '.join(f'{c} = ?' for c in cols)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (*[rec.get(c) for c in cols], rec["id"]),
                )
            conn.execute(
                "INSERT INTO list_items (list_id, lexeme_id, rank, raw, hint, context) VALUES (?, ?, ?, ?, ?, ?)",
                (l.id, rec["id"], rank, item.raw, item.english or None,
                 json.dumps(item.context, ensure_ascii=False) if item.context else None),
            )
            raw_to_lexeme[(str(l.path.relative_to(INPUTS_DIR)) if l.path else l.id, item.raw.strip().lower())] = rec["id"]
            # CILS legacy entries were keyed by the "italian" column
            raw_to_lexeme.setdefault((str(l.path.relative_to(INPUTS_DIR)) if l.path else l.id, rec["lemma"].lower()), rec["id"])
        stats[l.id] = {"items": len(seen), "new_lexemes": n_new}
        print(f"  {l.id:<18} {len(seen):>5} words  (+{n_new} new lexemes)")
    conn.commit()
    _map_legacy(conn, raw_to_lexeme)
    _assign_images(conn)
    _carry_facts(conn)
    total = conn.execute("SELECT COUNT(*) FROM lexemes WHERE id IN (SELECT lexeme_id FROM list_items)").fetchone()[0]
    print(f"  {total} distinct root words across {len(defs)} lists")
    return stats


def _map_legacy(conn: sqlite3.Connection, raw_to_lexeme: dict) -> None:
    """Link v3 entries to lexemes so their studied notes can be adopted."""
    rows = conn.execute("SELECT id, source_path, natural_id, italian FROM entries").fetchall()
    mapped = 0
    for r in rows:
        lex = raw_to_lexeme.get((r["source_path"], (r["natural_id"] or "").strip().lower())) or \
            raw_to_lexeme.get((r["source_path"], (r["italian"] or "").strip().lower()))
        if lex:
            conn.execute("INSERT OR REPLACE INTO entry_lexeme (entry_id, lexeme_id) VALUES (?, ?)", (r["id"], lex))
            mapped += 1
    conn.commit()
    print(f"  linked {mapped} of {len(rows)} legacy entries to lexemes")


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
    print(f"  images: {have} roots reuse an existing image")


def _carry_facts(conn: sqlite3.Connection) -> None:
    """Facts from the earlier backfill become drafts keyed by lexeme lemma."""
    n = conn.execute(
        "SELECT COUNT(*) FROM lexemes l JOIN word_facts f ON f.word = lower(l.lemma)"
    ).fetchone()[0]
    print(f"  {n} roots already have a draft fun-fact answer")


# ── Git export ─────────────────────────────────────────────────────────────


def export_jsonl(conn: sqlite3.Connection) -> Path:
    """lexicon/lexemes.jsonl + lexicon/lists.jsonl (sorted, diff-friendly).

    Movie example lines are deliberately left out (subtitle text stays
    private); everything else needed to rebuild the content is here.
    """
    LEXICON_DIR.mkdir(exist_ok=True)
    senses: dict[str, list] = {}
    for s in conn.execute("SELECT * FROM senses ORDER BY lexeme_id, idx"):
        senses.setdefault(s["lexeme_id"], []).append(
            {k: s[k] for k in ("prompt", "hint", "register", "note", "also") if s[k]})
    facts = {r["word"]: r for r in conn.execute("SELECT * FROM word_facts WHERE has_fact = 1")}
    out = LEXICON_DIR / "lexemes.jsonl"
    with out.open("w", encoding="utf-8") as fh:
        for r in conn.execute(
            "SELECT * FROM lexemes WHERE id IN (SELECT lexeme_id FROM list_items) ORDER BY lemma, pos"
        ):
            f = facts.get(fact_key(r["lemma"]))
            rec = {
                "id": r["id"], "lemma": r["lemma"], "pos": r["pos"], "display": r["display"],
                "gender": r["gender"], "plural": r["plural"], "english_plural": r["english_plural"],
                "forms": json.loads(r["forms_json"]) if r["forms_json"] else None,
                "irregular": bool(r["irregular"]), "ipa": r["ipa"],
                "etymology": r["etymology"], "freq_rank": r["freq_rank"],
                "cognate_rule": r["cognate_rule"], "senses": senses.get(r["id"]),
                "fact": {"kind": f["kind"], "text": f["fact"], "confidence": f["confidence"]} if f else None,
                "status": r["status"], "provenance": json.loads(r["provenance"] or "{}"),
            }
            fh.write(json.dumps({k: v for k, v in rec.items() if v not in (None, [], {})},
                                ensure_ascii=False, sort_keys=True) + "\n")
    with (LEXICON_DIR / "lists.jsonl").open("w", encoding="utf-8") as fh:
        for r in conn.execute(
            """SELECT li.list_id, li.rank, li.raw, li.hint, l.lemma, l.pos, li.context
               FROM list_items li JOIN lexemes l ON l.id = li.lexeme_id ORDER BY li.list_id, li.rank"""
        ):
            ctx = json.loads(r["context"]) if r["context"] else {}
            ctx.pop("example", None)  # subtitle text never leaves the local DB
            rec = {"list": r["list_id"], "rank": r["rank"], "raw": r["raw"], "hint": r["hint"],
                   "lemma": r["lemma"], "pos": r["pos"], "context": ctx or None}
            fh.write(json.dumps({k: v for k, v in rec.items() if v is not None},
                                ensure_ascii=False, sort_keys=True) + "\n")
    with (LEXICON_DIR / "identity.csv").open("w", encoding="utf-8") as fh:
        fh.write("key,guid,kind,first_seen\n")
        for r in conn.execute("SELECT key, guid, kind, first_seen FROM identity ORDER BY key"):
            fh.write(f"{r['key']},{r['guid']},{r['kind']},{r['first_seen']}\n")
    return out


def import_human_edits(conn: sqlite3.Connection) -> int:
    """Apply fields marked ``"provenance": {field: "human"}`` in lexemes.jsonl.

    Edit a line in ``lexicon/lexemes.jsonl``, set the field's provenance to
    "human", and the next build keeps your value forever.
    """
    path = LEXICON_DIR / "lexemes.jsonl"
    if not path.exists():
        return 0
    n = 0
    simple = {"display", "gender", "plural", "english_plural", "ipa", "etymology"}
    for line in path.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        human = [k for k, v in (rec.get("provenance") or {}).items() if v == "human"]
        if not human:
            continue
        row = conn.execute("SELECT provenance FROM lexemes WHERE id = ?", (rec["id"],)).fetchone()
        if not row:
            continue
        prov = json.loads(row["provenance"] or "{}")
        for field in human:
            if field in simple:
                conn.execute(f"UPDATE lexemes SET {field} = ? WHERE id = ?", (rec.get(field), rec["id"]))
            elif field == "senses":
                conn.execute("DELETE FROM senses WHERE lexeme_id = ?", (rec["id"],))
                for i, s in enumerate(rec.get("senses") or []):
                    conn.execute(
                        "INSERT INTO senses (lexeme_id, idx, prompt, hint, register, note, also, provenance) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 'human')",
                        (rec["id"], i, s.get("prompt"), s.get("hint"), s.get("register"), s.get("note"), s.get("also")),
                    )
            prov[field] = "human"
            n += 1
        conn.execute("UPDATE lexemes SET provenance = ? WHERE id = ?", (json.dumps(prov, sort_keys=True), rec["id"]))
    conn.commit()
    return n


__all__ = ["sync", "export_jsonl", "import_human_edits", "lexeme_id", "frequency", "image_exists", "plain"]
