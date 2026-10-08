"""The durable AI job queue.

``plan(conn)`` works out what still needs AI — prompts/verification/facts
for new roots, English prompts for verb forms, disambiguation hints for
prompts several words share, and images — and records it in ``ai_jobs``
in **study order** (deadline lists first). ``drain(conn)`` works through
the queue in batches: Claude (``claude -p``) for text, Codex for images.
Both wait out plan limits and resume by themselves; the queue survives
interruptions, so ``flashcards work`` simply carries on next time.

Builds never wait for the queue: cards are made from whatever is ready.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import defaultdict

from . import codex, kaikki
from .ai import AI, AIError
from .facts import normalise as fact_key
from .italian import compound, conjugation_class, progressive, strip_clitic
from .lexicon import image_exists
from .lists import load_plan
from .pool import run_pool
from .settings import settings
from .tasks import DISAMBIGUATE, LEXEME_ENRICH, PHRASE_ENRICH, VERB_PROMPTS
from .util import md5_hex, print_banner

BATCH = {"lexeme_enrich": 15, "phrase_enrich": 20, "verb_prompts": 3, "disambiguate": 8, "image": 1}
ORDER = ["lexeme_enrich", "phrase_enrich", "verb_prompts", "disambiguate", "image"]
DETERMINISTIC_POS = ("num", "letter")
MODEL_VERBS = {"-are": "parlare", "-ere": "credere", "-ire": "dormire", "-ire (isc)": "finire"}
COMPOUND_TENSES = ("passato_prossimo", "condizionale_passato", "presente_progressivo")


# ── Study order ────────────────────────────────────────────────────────────


def study_order(conn: sqlite3.Connection) -> dict[str, int]:
    """lexeme id → priority (lower = sooner), from plan.toml + list rank."""
    plan = load_plan()
    order = [p["list"] for p in plan.get("priority", [])]
    pos_of = {lid: i for i, lid in enumerate(order)}
    out: dict[str, int] = {}
    for r in conn.execute("SELECT list_id, lexeme_id, rank FROM list_items"):
        p = pos_of.get(r["list_id"], len(order)) * 100_000 + r["rank"]
        if p < out.get(r["lexeme_id"], 1 << 60):
            out[r["lexeme_id"]] = p
    return out


# ── Verb budget (also used by notes.py) ───────────────────────────────────


def full_form_verbs(conn: sqlite3.Connection) -> set[str]:
    """Verbs that get full conjugation cards: model verbs, irregular verbs,
    and the most frequent N (plan.toml [conjugation] full_forms_top)."""
    top = int(load_plan().get("conjugation", {}).get("full_forms_top", 120))
    rows = conn.execute(
        """SELECT id, lemma, irregular, freq_rank FROM lexemes
           WHERE pos = 'verb' AND forms_json IS NOT NULL
             AND (id IN (SELECT lexeme_id FROM list_items) OR lemma IN (?, ?, ?, ?))""",
        tuple(MODEL_VERBS.values()),
    ).fetchall()
    ranked = sorted((r for r in rows if r["freq_rank"]), key=lambda r: r["freq_rank"])[:top]
    keep = {r["id"] for r in ranked}
    keep |= {r["id"] for r in rows if r["irregular"] or r["lemma"] in MODEL_VERBS.values()}
    return keep


def plan_tenses() -> list[str]:
    return list(load_plan().get("conjugation", {}).get("tenses", [
        "presente", "passato_prossimo", "imperfetto", "futuro_semplice",
        "presente_progressivo", "imperativo", "condizionale_presente", "condizionale_passato",
    ]))


def verb_forms(lexeme: sqlite3.Row) -> dict[str, dict[str, tuple[str, str]]]:
    """{tense: {person: (display, audio)}} for the plan's tenses — simple
    tenses from Wiktionary, compound tenses built by rule."""
    info = json.loads(lexeme["forms_json"] or "{}")
    forms = info.get("forms", {})
    reflexive = bool(info.get("reflexive"))
    aux = info.get("auxiliary") or "avere"
    aux = "avere" if aux == "both" else aux
    pp = strip_clitic(info.get("past_participle") or "", reflexive)
    ger = strip_clitic(info.get("gerund") or "", reflexive)
    out: dict[str, dict[str, tuple[str, str]]] = {}
    for tense in plan_tenses():
        if tense in forms:
            out[tense] = {p: (f, f) for p, f in forms[tense].items()}
        elif tense == "passato_prossimo" and pp:
            out[tense] = {p: compound(aux, "presente", pp, p, reflexive) for p in ("io", "tu", "lui_lei", "noi", "voi", "loro")}
        elif tense == "condizionale_passato" and pp:
            out[tense] = {p: compound(aux, "condizionale", pp, p, reflexive) for p in ("io", "tu", "lui_lei", "noi", "voi", "loro")}
        elif tense == "presente_progressivo" and ger:
            out[tense] = {p: (progressive(ger, p, reflexive),) * 2 for p in ("io", "tu", "lui_lei", "noi", "voi", "loro")}
    return out


# ── Planning ───────────────────────────────────────────────────────────────


def _enqueue(conn, kind: str, subject: str, priority: int, payload: dict | None = None) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO ai_jobs (kind, subject, priority, payload) VALUES (?, ?, ?, ?)",
        (kind, subject, priority, json.dumps(payload, ensure_ascii=False) if payload else None),
    )
    if not cur.rowcount:
        conn.execute("UPDATE ai_jobs SET priority = ? WHERE kind = ? AND subject = ? AND status = 'pending'",
                     (priority, kind, subject))
    return bool(cur.rowcount)


def plan(conn: sqlite3.Connection) -> dict[str, int]:
    """Queue every job the current lexicon still needs. Returns new-job counts."""
    order = study_order(conn)
    added: dict[str, int] = defaultdict(int)
    live = conn.execute(
        "SELECT * FROM lexemes WHERE id IN (SELECT lexeme_id FROM list_items)"
    ).fetchall()
    has_senses = {r[0] for r in conn.execute("SELECT DISTINCT lexeme_id FROM senses")}
    for lx in live:
        pri = order.get(lx["id"], 1 << 30)
        if lx["pos"] in DETERMINISTIC_POS:
            pass
        elif lx["pos"] == "phrase":
            if lx["id"] not in has_senses:
                added["phrase_enrich"] += _enqueue(conn, "phrase_enrich", lx["id"], pri)
        elif lx["id"] not in has_senses:
            added["lexeme_enrich"] += _enqueue(conn, "lexeme_enrich", lx["id"], pri)
        if not image_exists(lx["image_key"]):
            added["image"] += _enqueue(conn, "image", lx["id"], pri)
    for m in conn.execute("SELECT id, italian FROM mistakes"):
        if not image_exists(m["italian"]):
            added["image"] += _enqueue(conn, "image", f"mistake:{m['id']}", 400)
    # verb forms needing English prompts
    have = defaultdict(set)
    for r in conn.execute("SELECT lexeme_id, tense, person FROM form_prompts"):
        have[r["lexeme_id"]].add((r["tense"], r["person"]))
    for vid in full_form_verbs(conn):
        lx = conn.execute("SELECT * FROM lexemes WHERE id = ?", (vid,)).fetchone()
        needed = {(t, p) for t, persons in verb_forms(lx).items() for p in persons}
        if needed - have[vid]:
            conn.execute("UPDATE ai_jobs SET status = 'pending' WHERE kind = 'verb_prompts' AND subject = ? AND status = 'done'", (vid,))
            added["verb_prompts"] += _enqueue(conn, "verb_prompts", vid, order.get(vid, 1 << 29))
    # shared prompts → disambiguation groups (only among ready roots)
    groups = defaultdict(list)
    for r in conn.execute(
        """SELECT s.lexeme_id, s.prompt, l.pos FROM senses s JOIN lexemes l ON l.id = s.lexeme_id
           WHERE s.idx = 0 AND l.status = 'ready' AND l.id IN (SELECT lexeme_id FROM list_items)
             AND coalesce(s.hint, '') = '' AND coalesce(s.also, '') = ''"""
    ):
        groups[(r["prompt"].strip().lower(), r["pos"])].append(r["lexeme_id"])
    for (prompt, _pos), ids in groups.items():
        if len(ids) > 1:
            subject = md5_hex(prompt + "|" + "|".join(sorted(ids)))
            added["disambiguate"] += _enqueue(conn, "disambiguate", subject, min(order.get(i, 1 << 30) for i in ids),
                                              {"prompt": prompt, "ids": sorted(ids)})
    conn.commit()
    return dict(added)


# ── Payloads ───────────────────────────────────────────────────────────────


def _list_glosses(conn, lexeme_id: str) -> list[str]:
    rows = conn.execute("SELECT DISTINCT hint FROM list_items WHERE lexeme_id = ? AND hint IS NOT NULL", (lexeme_id,))
    return [r[0] for r in rows][:3]


def _context_lines(conn, lexeme_id: str) -> list[str]:
    out = []
    for r in conn.execute("SELECT context FROM list_items WHERE lexeme_id = ? AND context IS NOT NULL", (lexeme_id,)):
        ex = json.loads(r[0]).get("example")
        if ex:
            out.append(ex)
    return out[:1]


def _lexeme_payload(conn, lx: sqlite3.Row) -> dict:
    entry = None
    if lx["in_kaikki"]:
        from .lexicon import _KAIKKI_FOR

        want = _KAIKKI_FOR.get(lx["pos"])
        found = (kaikki.entries(lx["lemma"], want) if want else []) or \
            (kaikki.entries(lx["lemma"], "det") if want == "article" else [])
        entry = found[0] if found else None
    chain = None
    if entry:
        src = kaikki.etymology_source(entry)
        if src and src[0] not in ("la", "LL.", "ML.", "la-lat", "la-vul"):  # borrowed words have the stories
            chain = kaikki.source_etymology(*src)
            if chain:
                conn.execute("UPDATE lexemes SET etymology_chain = ? WHERE id = ?", (chain, lx["id"]))
    draft = conn.execute("SELECT fact, kind FROM word_facts WHERE word = ? AND has_fact = 1",
                         (fact_key(lx["lemma"]),)).fetchone()
    return {k: v for k, v in {
        "id": lx["id"], "italian": lx["display"], "lemma": lx["lemma"], "pos": lx["pos"],
        "gender": lx["gender"],
        "dictionary_senses": (kaikki.glosses(entry)[:6] if entry else []),
        "list_glosses": _list_glosses(conn, lx["id"]),
        "contexts": _context_lines(conn, lx["id"]),
        "etymology": lx["etymology"], "etymology_source": chain,
        "draft_fact": draft["fact"] if draft else None,
    }.items() if v}


def _verb_payload(conn, lx: sqlite3.Row) -> dict:
    meaning = conn.execute("SELECT prompt FROM senses WHERE lexeme_id = ? ORDER BY idx", (lx["id"],)).fetchone()
    forms = {t: {p: d for p, (d, _a) in persons.items()} for t, persons in verb_forms(lx).items()}
    return {"id": lx["id"], "infinitive": lx["lemma"],
            "meaning": meaning[0] if meaning else (_list_glosses(conn, lx["id"]) or [""])[0],
            "forms": forms}


# ── Draining ───────────────────────────────────────────────────────────────


def _apply_lexeme(conn, word: dict, model: str) -> bool:
    lx = conn.execute("SELECT * FROM lexemes WHERE id = ?", (word["id"],)).fetchone()
    if not lx or not word.get("senses"):
        return False
    prov = json.loads(lx["provenance"] or "{}")
    if prov.get("senses") != "human":
        conn.execute("DELETE FROM senses WHERE lexeme_id = ?", (lx["id"],))
        for i, s in enumerate(word["senses"][:2]):
            conn.execute(
                "INSERT INTO senses (lexeme_id, idx, prompt, hint, register, note, provenance) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (lx["id"], i, s["prompt"].strip(), s.get("hint") or None, s.get("register") or None,
                 s.get("note") or None, f"claude:{model}"),
            )
        prov["senses"] = f"claude:{model}"
    issues = [i for i in word.get("issues") or [] if i.strip()]
    status = "ready" if word.get("verified", True) and not issues else "needs_review"
    conn.execute(
        "UPDATE lexemes SET english_plural = ?, status = ?, issues = ?, provenance = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (word.get("english_plural") or None, status, json.dumps(issues) if issues else None,
         json.dumps(prov, sort_keys=True), lx["id"]),
    )
    f = word.get("fact") or {}
    has = bool(f.get("has_fact")) and bool((f.get("text") or "").strip())
    conn.execute(
        "INSERT OR REPLACE INTO word_facts (word, has_fact, kind, fact, confidence, model) VALUES (?, ?, ?, ?, ?, ?)",
        (fact_key(lx["lemma"]), 1 if has else 0, f.get("kind") if has else None,
         (f.get("text") or "").strip() if has else None, float(f.get("confidence") or 0), f"claude:{model}"),
    )
    return True


def _apply_phrase(conn, p: dict, model: str) -> bool:
    if not p.get("prompt"):
        return False
    conn.execute("DELETE FROM senses WHERE lexeme_id = ?", (p["id"],))
    conn.execute(
        "INSERT INTO senses (lexeme_id, idx, prompt, hint, note, provenance) VALUES (?, 0, ?, ?, ?, ?)",
        (p["id"], p["prompt"].strip(), p.get("hint") or None, p.get("note") or None, f"claude:{model}"),
    )
    issues = [i for i in p.get("issues") or [] if i.strip()]
    conn.execute("UPDATE lexemes SET status = ?, issues = ? WHERE id = ?",
                 ("ready" if p.get("verified", True) and not issues else "needs_review",
                  json.dumps(issues) if issues else None, p["id"]))
    return True


def _run_text_batch(ai: AI, kind: str, jobs: list[sqlite3.Row], conn, lock) -> tuple[int, int]:
    with lock:
        if kind == "lexeme_enrich":
            rows = [conn.execute("SELECT * FROM lexemes WHERE id = ?", (j["subject"],)).fetchone() for j in jobs]
            task = LEXEME_ENRICH.task({"words": [_lexeme_payload(conn, r) for r in rows if r]})
        elif kind == "phrase_enrich":
            rows = [conn.execute("SELECT * FROM lexemes WHERE id = ?", (j["subject"],)).fetchone() for j in jobs]
            task = PHRASE_ENRICH.task({"phrases": [
                {"id": r["id"], "italian": r["display"], "list_glosses": _list_glosses(conn, r["id"])} for r in rows if r]})
        elif kind == "verb_prompts":
            rows = [conn.execute("SELECT * FROM lexemes WHERE id = ?", (j["subject"],)).fetchone() for j in jobs]
            task = VERB_PROMPTS.task({"verbs": [_verb_payload(conn, r) for r in rows if r]})
        else:  # disambiguate
            groups = []
            for j in jobs:
                p = json.loads(j["payload"])
                words = []
                for lid in p["ids"]:
                    r = conn.execute("SELECT l.display, l.pos, s.note FROM lexemes l LEFT JOIN senses s ON s.lexeme_id = l.id AND s.idx = 0 WHERE l.id = ?", (lid,)).fetchone()
                    if r:
                        words.append({"id": lid, "italian": r["display"], "pos": r["pos"], "note": r["note"] or ""})
                groups.append({"prompt": p["prompt"], "words": words})
            task = DISAMBIGUATE.task({"groups": groups})
        conn.commit()
    result = ai.run(task)
    model = task.model
    ok = failed = 0
    with lock:
        if kind == "lexeme_enrich":
            done = {w["id"] for w in result.get("words", []) if _apply_lexeme(conn, w, model)}
        elif kind == "phrase_enrich":
            done = {p["id"] for p in result.get("phrases", []) if _apply_phrase(conn, p, model)}
        elif kind == "verb_prompts":
            done = set()
            for v in result.get("verbs", []):
                for pr in v.get("prompts", []):
                    if pr.get("english"):
                        conn.execute("INSERT OR REPLACE INTO form_prompts VALUES (?, ?, ?, ?)",
                                     (v["id"], pr["tense"], pr["person"], pr["english"].strip()))
                done.add(v["id"])
        else:
            done = set()
            for w in result.get("words", []):
                conn.execute("UPDATE senses SET hint = coalesce(nullif(?, ''), hint), also = nullif(?, '') WHERE lexeme_id = ? AND idx = 0",
                             (w.get("hint", ""), w.get("also", ""), w["id"]))
            done = {j["subject"] for j in jobs}
        for j in jobs:
            if j["subject"] in done:
                conn.execute("UPDATE ai_jobs SET status = 'done', attempts = attempts + 1, error = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (j["id"],))
                ok += 1
            else:
                conn.execute("UPDATE ai_jobs SET attempts = attempts + 1, error = 'not answered', status = CASE WHEN attempts >= 2 THEN 'failed' ELSE 'pending' END, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (j["id"],))
                failed += 1
        conn.commit()
    return ok, failed


def _run_image(job: sqlite3.Row, conn, lock) -> bool:
    if job["subject"].startswith("mistake:"):
        with lock:
            m = conn.execute("SELECT * FROM mistakes WHERE id = ?", (job["subject"][8:],)).fetchone()
        if not m:
            return False
        codex.generate(m["italian"], m["italian"], m["english"], "phrase")
        return True
    with lock:
        lx = conn.execute("SELECT * FROM lexemes WHERE id = ?", (job["subject"],)).fetchone()
        sense = conn.execute("SELECT prompt FROM senses WHERE lexeme_id = ? ORDER BY idx", (job["subject"],)).fetchone()
        hint = conn.execute("SELECT hint FROM list_items WHERE lexeme_id = ? AND hint IS NOT NULL", (job["subject"],)).fetchone()
    if not lx:
        return False
    meaning = (sense[0] if sense else None) or (hint[0] if hint else None) or lx["lemma"]
    codex.generate(lx["image_key"] or lx["lemma"], lx["display"] or lx["lemma"], meaning, lx["pos"])
    return True


def drain(conn: sqlite3.Connection, *, kinds: list[str] | None = None, max_batches: int | None = None,
          workers: int | None = None) -> dict:
    """Work through pending jobs (study order). Waits out plan limits."""
    print_banner("work — AI queue")
    lock = threading.Lock()
    ai = AI(conn, lock)
    workers = workers or settings.ai.concurrency
    summary: dict[str, list[int]] = {}
    batches_run = 0
    for kind in kinds or ORDER:
        if kind == "image" and not codex.logged_in():
            n = conn.execute("SELECT COUNT(*) FROM ai_jobs WHERE kind = 'image' AND status = 'pending'").fetchone()[0]
            print(f"  image: {n} pending — Codex is not logged in (run `codex login`); skipping images for now.")
            continue
        size = BATCH[kind]
        while True:
            if max_batches is not None and batches_run >= max_batches:
                break
            take = size * (workers if max_batches is None else max(1, min(workers, max_batches - batches_run)))
            jobs = conn.execute(
                "SELECT * FROM ai_jobs WHERE kind = ? AND status = 'pending' ORDER BY priority, id LIMIT ?",
                (kind, take),
            ).fetchall()
            if not jobs:
                break
            groups = [jobs[i:i + size] for i in range(0, len(jobs), size)]
            if kind == "image":
                fn = lambda g: _run_image(g[0], conn, lock)  # noqa: E731
            else:
                fn = lambda g: _run_text_batch(ai, kind, g, conn, lock)  # noqa: E731
            for group, res in run_pool(groups, fn, workers=min(workers, len(groups)), label=f"{kind} batches",
                                       progress_every=max(1, len(groups)),
                                       describe=lambda g: g[0]["subject"][:12]):
                stats = summary.setdefault(kind, [0, 0])
                if isinstance(res, Exception):
                    with lock:
                        for j in group:
                            conn.execute(
                                "UPDATE ai_jobs SET attempts = attempts + 1, error = ?, status = CASE WHEN attempts >= 2 THEN 'failed' ELSE 'pending' END, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                                (str(res)[:500], j["id"]),
                            )
                        conn.commit()
                    stats[1] += len(group)
                    if isinstance(res, AIError) and "interrupted" in str(res):
                        raise KeyboardInterrupt
                elif kind == "image":
                    with lock:
                        conn.execute("UPDATE ai_jobs SET status = 'done', attempts = attempts + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (group[0]["id"],))
                        conn.commit()
                    stats[0] += 1
                else:
                    stats[0] += res[0]
                    stats[1] += res[1]
            batches_run += len(groups)
            done, failed = summary.get(kind, [0, 0])
            left = conn.execute("SELECT COUNT(*) FROM ai_jobs WHERE kind = ? AND status = 'pending'", (kind,)).fetchone()[0]
            print(f"  {kind}: {done} done, {failed} not yet, {left} pending", flush=True)
    return {k: {"done": v[0], "failed": v[1]} for k, v in summary.items()}


def status(conn: sqlite3.Connection) -> list[tuple]:
    return [tuple(r) for r in conn.execute(
        "SELECT kind, status, COUNT(*) FROM ai_jobs GROUP BY kind, status ORDER BY kind, status")]


def retry_failed(conn: sqlite3.Connection, kind: str | None = None) -> int:
    cur = conn.execute(
        "UPDATE ai_jobs SET status = 'pending', attempts = 0 WHERE status = 'failed'" + (" AND kind = ?" if kind else ""),
        (kind,) if kind else (),
    )
    conn.commit()
    return cur.rowcount


def refresh(conn: sqlite3.Connection, list_id: str) -> int:
    """Re-ask Claude for every root in a list (keeps human edits)."""
    ids = [r[0] for r in conn.execute("SELECT lexeme_id FROM list_items WHERE list_id = ?", (list_id,))]
    for lid in ids:
        conn.execute("UPDATE ai_jobs SET status = 'pending', attempts = 0 WHERE subject = ? AND kind IN ('lexeme_enrich', 'phrase_enrich')", (lid,))
        conn.execute("UPDATE lexemes SET status = 'new' WHERE id = ?", (lid,))
        conn.execute("DELETE FROM senses WHERE lexeme_id = ? AND coalesce(provenance, '') != 'human'", (lid,))
    conn.commit()
    return len(ids)


__all__ = ["plan", "drain", "status", "study_order", "full_form_verbs", "verb_forms",
           "plan_tenses", "MODEL_VERBS", "conjugation_class", "retry_failed", "refresh"]
