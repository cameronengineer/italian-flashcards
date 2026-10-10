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
from uuid import uuid4
from pathlib import Path
import tempfile
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import codex, kaikki, overrides, report
from .db import transaction
from .runtime import writer_lock
from .validation import validate
from .assets import record
from .ai import AI, AIUnavailable
from .pool import STOP
from .settings import settings
from .facts import normalise as fact_key
from .italian import conjugation_class
from .planning import study_order, full_form_verbs, plan_tenses, verb_forms, MODEL_VERBS
from .lexicon import image_exists
from .tasks import DISAMBIGUATE, LEXEME_ENRICH, PHRASE_ENRICH, VERB_PROMPTS
from .util import md5_hex, print_banner

BATCH = {"lexeme_enrich": 15, "phrase_enrich": 20, "verb_prompts": 3, "disambiguate": 8, "image": 1}
ORDER = ["lexeme_enrich", "phrase_enrich", "verb_prompts", "disambiguate", "image"]
DETERMINISTIC_POS = ("num", "letter")
SPECS = {
    "lexeme_enrich": LEXEME_ENRICH,
    "phrase_enrich": PHRASE_ENRICH,
    "verb_prompts": VERB_PROMPTS,
    "disambiguate": DISAMBIGUATE,
}
TEXT_ROOTS = {
    "lexeme_enrich": "words",
    "phrase_enrich": "phrases",
    "verb_prompts": "verbs",
    "disambiguate": "groups",
}


# ── Planning ───────────────────────────────────────────────────────────────


# Payload fields that are not inputs: ``draft_fact`` is the previous answer
# (it changes every time the job runs) and the dictionary version is already
# reflected in the dictionary senses themselves. Fingerprinting them would
# reopen every finished job after each run or dictionary rebuild.
_VOLATILE = ("draft_fact", "dictionary_version", "existing_senses", "existing_prompts")


#: 2: the model is no longer part of a job's identity.
FINGERPRINT_VERSION = "2"


def _fingerprint(kind: str, payload: dict) -> str:
    """Hash of a job's inputs, prompt, schema and ``[ai] task_version`` (see ``_VOLATILE``).

    The model is deliberately left out: switching models only affects work
    still to do. To redo everything (say with a stronger model), bump
    ``task_version`` in settings.toml.
    """
    inputs = {k: v for k, v in payload.items() if k not in _VOLATILE}
    spec = SPECS.get(kind)
    if not spec:
        return md5_hex(json.dumps(inputs, sort_keys=True))
    task = spec.task(inputs)
    return md5_hex(
        json.dumps(
            {
                "task": task.name,
                "messages": task.messages(),
                "schema": task.schema,
                "version": settings.ai.task_version,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _enqueue(conn, kind, subject, priority, payload, *, reopen=False):
    fingerprint = _fingerprint(kind, payload)
    previous = conn.execute("SELECT * FROM ai_jobs WHERE kind=? AND subject=?", (kind, subject)).fetchone()
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if previous is None:
        conn.execute(
            "INSERT INTO ai_jobs(kind,subject,priority,payload,fingerprint) VALUES(?,?,?,?,?)",
            (kind, subject, priority, encoded, fingerprint),
        )
        return True
    changed = previous["fingerprint"] is not None and previous["fingerprint"] != fingerprint
    # ``reopen`` revives finished work whose output went missing; it never
    # revives a failed job (that would retry it forever) — `retry` does that.
    revive = previous["status"] == "cancelled" or (reopen and previous["status"] == "done")
    status = "pending" if changed or revive else previous["status"]
    conn.execute(
        "UPDATE ai_jobs SET priority=?,payload=?,fingerprint=?,status=?,"
        "attempts=CASE WHEN ? THEN 0 ELSE attempts END,"
        "next_retry_at=CASE WHEN ? THEN NULL ELSE next_retry_at END WHERE id=?",
        (priority, encoded, fingerprint, status, changed or revive, changed or revive, previous["id"]),
    )
    if changed and kind in ("lexeme_enrich", "phrase_enrich"):
        conn.execute(
            "UPDATE lexemes SET status=CASE WHEN status='needs_review' THEN status ELSE 'new' END WHERE id=?",
            (subject,),
        )
    if changed or revive:
        conn.execute("DELETE FROM metadata WHERE key='notes_generation'")
    return status == "pending" and previous["status"] != "pending"


def plan(conn):
    stored = conn.execute("SELECT value FROM metadata WHERE key='fingerprint_version'").fetchone()
    if not stored or stored[0] != FINGERPRINT_VERSION:
        # New fingerprint formula: re-record every job's fingerprint without reopening it.
        conn.execute("UPDATE ai_jobs SET fingerprint=NULL")
        conn.execute(
            "INSERT OR REPLACE INTO metadata VALUES('fingerprint_version',?)", (FINGERPRINT_VERSION,)
        )
    order = study_order(conn)
    from .planning import card_plan

    planned = card_plan(conn)
    selected = planned["roots"]
    added = defaultdict(int)
    senses = {r[0] for r in conn.execute("SELECT DISTINCT lexeme_id FROM senses WHERE active=1")}
    dictionary = kaikki.version()
    # Every (kind, subject) wanted now; anything else still pending is cancelled
    # (e.g. a verb that left the full-forms set, an image that now exists).
    active = set()

    def enqueue(kind, subject, priority, payload, **kw):
        active.add((kind, subject))
        return _enqueue(conn, kind, subject, priority, payload, **kw)

    for lx in conn.execute("SELECT * FROM lexemes WHERE id IN (SELECT lexeme_id FROM list_items)").fetchall():
        if lx["id"] not in selected:
            continue
        pri = order[lx["id"]]
        if lx["pos"] not in DETERMINISTIC_POS:
            kind = "phrase_enrich" if lx["pos"] == "phrase" else "lexeme_enrich"
            payload = (
                {"id": lx["id"], "italian": lx["display"], "list_glosses": _list_glosses(conn, lx["id"])}
                if kind == "phrase_enrich"
                else _lexeme_payload(conn, lx)
            )
            payload["dictionary_version"] = dictionary
            exists = conn.execute(
                "SELECT status FROM ai_jobs WHERE kind=? AND subject=?", (kind, lx["id"])
            ).fetchone()
            need = lx["id"] not in senses
            added[kind] += enqueue(
                kind, lx["id"], pri, payload, reopen=bool(need and exists and exists[0] == "done")
            )
            if not exists and not need:
                conn.execute("UPDATE ai_jobs SET status='done' WHERE kind=? AND subject=?", (kind, lx["id"]))
        policies = [
            json.loads(r[0] or "{}").get("image_policy", "optional")
            for r in conn.execute(
                "SELECT l.settings FROM lists l JOIN list_items li ON li.list_id=l.id WHERE li.lexeme_id=?",
                (lx["id"],),
            )
        ]
        if not image_exists(lx["image_key"]) and not (policies and all(p == "disabled" for p in policies)):
            # Images wait for usable semantics; deterministic subjects need no enrichment.
            if lx["pos"] in DETERMINISTIC_POS or lx["status"] == "ready":
                added["image"] += enqueue(
                    "image",
                    lx["id"],
                    pri,
                    {"image_key": lx["image_key"], "subject": lx["id"]},
                    reopen=True,
                )
    for vid in planned["verbs"]:
        lx = conn.execute("SELECT * FROM lexemes WHERE id=?", (vid,)).fetchone()
        if lx["status"] == "ready" and verb_forms(lx):
            added["verb_prompts"] += enqueue(
                "verb_prompts",
                vid,
                order.get(vid, 1 << 29),
                _verb_payload(conn, lx, planned["keys"] if planned.get("budget") is not None else None),
            )
    for m in conn.execute("SELECT id,italian FROM mistakes"):
        subject = "mistake:" + m["id"]
        if not image_exists(m["italian"]):
            added["image"] += enqueue("image", subject, 400, {"image_key": m["italian"]}, reopen=True)
    groups = defaultdict(list)
    for r in conn.execute(
        "SELECT s.*,l.pos,l.display FROM senses s JOIN lexemes l ON l.id=s.lexeme_id WHERE s.active=1 AND s.idx=0 AND l.status='ready' AND coalesce(s.hint,'')='' AND coalesce(s.also,'')=''"
    ):
        if r["lexeme_id"] in selected and not overrides.is_human(conn, r["lexeme_id"], "senses"):
            groups[(r["prompt"].strip().lower(), r["pos"])].append(dict(r))
    for (prompt, _pos), words in groups.items():
        if len(words) < 2:
            continue
        ids = sorted(w["lexeme_id"] for w in words)
        subject = md5_hex(prompt + "|" + str(ids))
        payload = {
            "prompt": prompt,
            "ids": ids,
            "words": [
                {"id": w["lexeme_id"], "italian": w["display"], "pos": w["pos"], "note": w["note"] or ""}
                for w in words
            ],
        }
        added["disambiguate"] += enqueue(
            "disambiguate", subject, min(order.get(i, 1 << 30) for i in ids), payload
        )
    for row in conn.execute("SELECT id,kind,subject FROM ai_jobs WHERE status='pending'").fetchall():
        if (row["kind"], row["subject"]) not in active:
            conn.execute(
                "UPDATE ai_jobs SET status='cancelled',error='no longer needed' WHERE id=?", (row["id"],)
            )
    conn.commit()
    return dict(added)


# ── Payloads ───────────────────────────────────────────────────────────────


def _list_glosses(conn, lexeme_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT hint FROM list_items WHERE lexeme_id = ? AND hint IS NOT NULL ORDER BY list_id, rank",
        (lexeme_id,),
    )
    glosses = [r[0] for r in rows]
    # Membership deduplication must not discard the evidence from repeated
    # rows (e.g. masculine/feminine forms with different source glosses).
    for row in conn.execute(
        "SELECT payload FROM source_observations WHERE lexeme_id=? ORDER BY list_id,row_key",
        (lexeme_id,),
    ):
        gloss = json.loads(row[0]).get("english")
        if gloss and gloss.strip():
            glosses.append(gloss)
    return list(dict.fromkeys(glosses))


def _context_lines(conn, lexeme_id: str) -> list[str]:
    out = []
    for r in conn.execute(
        "SELECT context FROM list_items WHERE lexeme_id = ? AND context IS NOT NULL ORDER BY list_id, rank",
        (lexeme_id,),
    ):
        ex = json.loads(r[0]).get("example")
        if ex:
            out.append(ex)
    for row in conn.execute(
        "SELECT payload FROM source_observations WHERE lexeme_id=? ORDER BY list_id,row_key",
        (lexeme_id,),
    ):
        example = json.loads(row[0]).get("context", {}).get("example")
        if example:
            out.append(example)
    return list(dict.fromkeys(out))[:8]


def _kaikki_entry(lx: sqlite3.Row) -> dict | None:
    if not lx["in_kaikki"]:
        return None
    from .lexicon import _KAIKKI_FOR

    want = _KAIKKI_FOR.get(lx["pos"])
    found = (kaikki.entries(lx["lemma"], want) if want else []) or (
        kaikki.entries(lx["lemma"], "det") if want == "article" else []
    )
    return found[0] if found else None


def _lexeme_payload(conn, lx: sqlite3.Row) -> dict:
    from . import semantics

    entry = _kaikki_entry(lx)
    meaning_evidence = semantics.evidence(conn, lx)
    chain = lx["etymology_chain"]
    draft = conn.execute(
        "SELECT fact, kind FROM word_facts WHERE word = ? AND has_fact = 1", (fact_key(lx["lemma"]),)
    ).fetchone()
    return {
        k: v
        for k, v in {
            "id": lx["id"],
            "italian": lx["display"],
            "lemma": lx["lemma"],
            "pos": lx["pos"],
            "gender": lx["gender"],
            "dictionary_senses": [g for c in meaning_evidence["candidates"] for g in c["glosses"]],
            "meaning_evidence": meaning_evidence,
            "existing_senses": [
                dict(r)
                for r in conn.execute(
                    "SELECT idx,prompt,source_id FROM senses WHERE lexeme_id=? AND active=1 ORDER BY idx",
                    (lx["id"],),
                )
            ],
            "plural": lx["plural"],
            "forms": json.loads(lx["forms_json"] or "{}"),
            "source_entry": entry.get("word") if entry else None,
            "list_glosses": _list_glosses(conn, lx["id"]),
            "contexts": _context_lines(conn, lx["id"]),
            "etymology": lx["etymology"],
            "etymology_source": chain,
            "draft_fact": draft["fact"] if draft else None,
        }.items()
        if v
    }


def _add_etymology_chains(conn, jobs: list[sqlite3.Row], lock=None) -> list[sqlite3.Row]:
    """One hop down each root's etymology (French *robinet* for *rubinetto*),
    fetched from kaikki.org only when the root is about to be enriched and
    kept on the lexeme. Returns the jobs with refreshed payloads."""
    lock = lock or threading.Lock()
    refreshed = []
    for job in jobs:
        if STOP.is_set():
            raise KeyboardInterrupt
        with lock:
            lx = conn.execute("SELECT * FROM lexemes WHERE id = ?", (job["subject"],)).fetchone()
        if lx and lx["in_kaikki"] and not lx["etymology_chain"]:
            # Network requests must not hold the shared SQLite lock or delay
            # the other batches' provider calls and completed checkpoints.
            entry = _kaikki_entry(lx)
            source = kaikki.etymology_source(entry) if entry else None
            chain = (kaikki.source_etymology(*source) or "")[:500] if source else None
            if chain:
                with lock, transaction(conn):
                    conn.execute("UPDATE lexemes SET etymology_chain = ? WHERE id = ?", (chain, lx["id"]))
                    lx = conn.execute("SELECT * FROM lexemes WHERE id = ?", (lx["id"],)).fetchone()
                    payload = {**_lexeme_payload(conn, lx), "dictionary_version": kaikki.version()}
                    conn.execute(
                        "UPDATE ai_jobs SET payload = ?, fingerprint = ? WHERE id = ?",
                        (
                            json.dumps(payload, ensure_ascii=False, sort_keys=True),
                            _fingerprint("lexeme_enrich", payload),
                            job["id"],
                        ),
                    )
        with lock:
            refreshed.append(conn.execute("SELECT * FROM ai_jobs WHERE id = ?", (job["id"],)).fetchone())
    return refreshed


def _verb_payload(conn, lx, selected_keys=None):
    from . import semantics

    sense = semantics.primary(conn, lx["id"])
    forms = {t: {p: d for p, (d, _a) in persons.items()} for t, persons in verb_forms(lx, sense).items()}
    if selected_keys is not None:
        suffix = semantics.form_suffix(sense)
        forms = {
            t: {
                p: value
                for p, value in persons.items()
                if f"form:{lx['id']}:{t}:{p}{suffix}" in selected_keys
            }
            for t, persons in forms.items()
        }
        forms = {t: persons for t, persons in forms.items() if persons}
    return {
        "id": lx["id"],
        "infinitive": lx["lemma"],
        "meaning": sense["prompt"] if sense else (_list_glosses(conn, lx["id"]) or [""])[0],
        "source_id": sense["source_id"] if sense else None,
        "usage": semantics.usage(lx, sense),
        "forms": forms,
        "existing_prompts": [
            dict(r)
            for r in conn.execute(
                "SELECT tense,person,english FROM form_prompts WHERE lexeme_id=? AND source_id=?",
                (lx["id"], sense["source_id"] if sense else None),
            )
        ],
    }


# ── Draining ───────────────────────────────────────────────────────────────


def _apply_lexeme(conn, word, model):
    lx = conn.execute("SELECT * FROM lexemes WHERE id=?", (word["id"],)).fetchone()
    if not lx or not word.get("senses"):
        raise ValueError("Missing lexeme or senses")
    prov = json.loads(lx["provenance"] or "{}")
    if not overrides.is_human(conn, lx["id"], "senses"):
        overrides.save_senses(conn, lx["id"], word["senses"], f"claude:{model}")
        prov["senses"] = f"claude:{model}"
    issues = [x for x in word.get("issues", []) if x.strip()]
    status = "ready" if word.get("verified") and not issues else "needs_review"
    plural = (
        lx["english_plural"]
        if overrides.is_human(conn, lx["id"], "english_plural")
        else word.get("english_plural") or None
    )
    conn.execute(
        "UPDATE lexemes SET english_plural=?,status=?,issues=?,provenance=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (plural, status, json.dumps(issues), json.dumps(prov, sort_keys=True), lx["id"]),
    )
    f = word.get("fact") or {}
    if not overrides.is_human(conn, lx["id"], "fact"):
        has = bool(f.get("has_fact") and (f.get("text") or "").strip())
        conn.execute(
            "INSERT OR REPLACE INTO word_facts(word,has_fact,kind,fact,confidence,model) VALUES(?,?,?,?,?,?)",
            (
                fact_key(lx["lemma"]),
                has,
                f.get("kind") if has else None,
                f.get("text") if has else None,
                f.get("confidence", 0),
                f"claude:{model}",
            ),
        )
    overrides.apply(conn, lx["id"])
    return True


def _apply_phrase(conn, p, model):
    lx = conn.execute("SELECT * FROM lexemes WHERE id=?", (p["id"],)).fetchone()
    if not lx or not (p.get("prompt") or "").strip():
        raise ValueError("Missing phrase or prompt")
    if not overrides.is_human(conn, p["id"], "senses"):
        overrides.save_senses(conn, p["id"], [p], f"claude:{model}")
    issues = [x for x in p.get("issues", []) if x.strip()]
    conn.execute(
        "UPDATE lexemes SET status=?,issues=? WHERE id=?",
        ("ready" if p.get("verified") and not issues else "needs_review", json.dumps(issues), p["id"]),
    )
    overrides.apply(conn, p["id"])
    return True


def _validate_text_result(kind, payloads, jobs, result):
    """The same semantic checks apply to fresh answers and cached answers."""
    validate(result, SPECS[kind].schema)
    returned = result["words" if kind == "disambiguate" else TEXT_ROOTS[kind]]
    expected = (
        {i for p in payloads for i in p["ids"]} if kind == "disambiguate" else {j["subject"] for j in jobs}
    )
    ids = [item["id"] for item in returned]
    if set(ids) != expected or len(ids) != len(set(ids)):
        raise ValueError("Provider must return exactly the requested IDs once each")
    for item in returned:
        if kind == "lexeme_enrich" and (
            not item["senses"] or any(not s["prompt"].strip() for s in item["senses"])
        ):
            raise ValueError("Empty sense response")
        if kind == "lexeme_enrich":
            payload = next(p for p in payloads if p["id"] == item["id"])
            evidence = payload.get("meaning_evidence", {})
            allowed = {s["source_id"] for s in evidence.get("candidates", [])}
            for sense in item["senses"]:
                if allowed and sense["source_id"] not in allowed:
                    raise ValueError("Sense must cite supplied dictionary/source evidence")
        if kind == "phrase_enrich" and not item["prompt"].strip():
            raise ValueError("Empty phrase prompt")
        if kind == "verb_prompts":
            payload = next(p for p in payloads if p["id"] == item["id"])
            requested = {(t, p) for t, ps in payload["forms"].items() for p in ps}
            accepted = [(p["tense"], p["person"]) for p in item["prompts"]]
            excluded = [(p["tense"], p["person"]) for p in item["exclusions"]]
            if set(accepted + excluded) != requested or len(accepted + excluded) != len(requested):
                raise ValueError("Every requested verb form needs exactly one prompt or reasoned exclusion")
            if any(not p["english"].strip() for p in item["prompts"]) or any(
                not p["reason"].strip() for p in item["exclusions"]
            ):
                raise ValueError("Empty verb prompt or exclusion reason")
            rules = payload.get("usage", {})
            for prompt in item["prompts"]:
                if rules.get("persons") and prompt["person"] not in rules["persons"]:
                    raise ValueError("Verb prompt uses a person excluded by its meaning")
                if prompt["tense"] == "imperativo" and (
                    rules.get("no_imperative") or payload.get("infinitive") in {"dovere", "potere"}
                ):
                    raise ValueError("Unsuitable imperative must be explicitly excluded")


def _run_text_batch(ai, kind, jobs, conn, lock):
    payloads = []
    for job in jobs:
        if not job["payload"]:
            raise ValueError(f"{kind} job {job['id']} has no input; re-plan its dependencies")
        payload = json.loads(job["payload"])
        if not isinstance(payload, dict):
            raise ValueError(f"{kind} job {job['id']} has an invalid input object")
        payloads.append(payload)
    task = SPECS[kind].task({TEXT_ROOTS[kind]: payloads})

    def check(result):
        _validate_text_result(kind, payloads, jobs, result)

    result = ai.run(task, refresh=any(j["force_refresh"] for j in jobs), validate_result=check)
    check(result)
    returned = result["words" if kind == "disambiguate" else TEXT_ROOTS[kind]]
    by_id = {r["id"]: r for r in returned}
    with lock, transaction(conn):
        for item in returned:
            if kind == "lexeme_enrich":
                evidence = next(p.get("meaning_evidence", {}) for p in payloads if p["id"] == item["id"])
                for sense in item["senses"]:
                    sense["context"] = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
                _apply_lexeme(conn, item, task.model)
            elif kind == "phrase_enrich":
                _apply_phrase(conn, item, task.model)
            elif kind == "verb_prompts":
                source_id = next(p.get("source_id") for p in payloads if p["id"] == item["id"])
                requested = next(p["forms"] for p in payloads if p["id"] == item["id"])
                pairs = [(item["id"], t, p) for t, persons in requested.items() for p in persons]
                conn.executemany("DELETE FROM form_prompts WHERE lexeme_id=? AND tense=? AND person=?", pairs)
                conn.executemany(
                    "DELETE FROM form_exclusions WHERE lexeme_id=? AND tense=? AND person=?", pairs
                )
                conn.executemany(
                    "INSERT INTO form_exclusions(lexeme_id,tense,person,source_id,reason) VALUES(?,?,?,?,?)",
                    [
                        (item["id"], p["tense"], p["person"], source_id, p["reason"])
                        for p in item["exclusions"]
                    ],
                )
                conn.executemany(
                    "INSERT INTO form_prompts(lexeme_id,tense,person,english,source_id) VALUES(?,?,?,?,?)",
                    [
                        (item["id"], pr["tense"], pr["person"], pr["english"].strip(), source_id)
                        for pr in item["prompts"]
                    ],
                )
            elif not overrides.is_human(conn, item["id"], "senses"):
                conn.execute(
                    "UPDATE senses SET hint=?,also=? WHERE lexeme_id=? AND idx=0 AND active=1",
                    (item["hint"] or None, item["also"] or None, item["id"]),
                )
        for job in jobs:
            response = result if kind == "disambiguate" else by_id[job["subject"]]
            conn.execute(
                "UPDATE ai_jobs SET status='done',attempts=attempts+1,result=?,error=NULL,force_refresh=0,owner=NULL,next_retry_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (json.dumps(response, ensure_ascii=False), job["id"]),
            )
        conn.execute("DELETE FROM metadata WHERE key='notes_generation'")
    return len(jobs)


KIND_NAME = {
    "lexeme_enrich": "words",
    "phrase_enrich": "phrases",
    "verb_prompts": "verbs",
    "disambiguate": "hints",
    "image": "images",
}


def _wave_text(wave) -> list[str]:
    """'Claude: 3 word batches (45) · Codex: 1 image'"""
    by_kind: dict[str, int] = defaultdict(int)
    batches: dict[str, int] = defaultdict(int)
    for kind, jobs, _n, _l in wave:
        by_kind[kind] += len(jobs)
        batches[kind] += 1
    parts = [f"{batches[k]}× {KIND_NAME[k]} ({by_kind[k]})" for k in by_kind if k != "image"]
    out = [f"Claude: {', '.join(parts)}"] if parts else []
    if "image" in by_kind:
        out.append(f"Codex: {report.plural(by_kind['image'], 'image')}")
    return out


def _label(conn, kind, jobs) -> str:
    return (
        _describe(conn, jobs)
        if kind != "disambiguate"
        else f"group of {len(json.loads(jobs[0]['payload'] or '{}').get('ids', []))}"
    )


def pending_counts(conn) -> dict[str, int]:
    """kind → jobs waiting to run (pending or interrupted)."""
    return {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT kind, count(*) FROM ai_jobs WHERE status IN ('pending','running') GROUP BY kind"
        )
    }


def _describe(conn, jobs) -> str:
    """'casa, cane, gatto +12' for a progress line."""
    names = []
    for j in jobs[:3]:
        row = conn.execute("SELECT lemma FROM lexemes WHERE id = ?", (j["subject"],)).fetchone()
        names.append(row[0] if row else j["subject"][:12])
    return ", ".join(names) + (f" +{len(jobs) - 3}" if len(jobs) > 3 else "")


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
        sense = conn.execute(
            "SELECT prompt FROM senses WHERE lexeme_id = ? AND active=1 ORDER BY idx", (job["subject"],)
        ).fetchone()
        hint = conn.execute(
            "SELECT hint FROM list_items WHERE lexeme_id = ? AND hint IS NOT NULL", (job["subject"],)
        ).fetchone()
    if not lx:
        return False
    meaning = (sense[0] if sense else None) or (hint[0] if hint else None) or lx["lemma"]
    dest = codex.generate(lx["image_key"] or lx["lemma"], lx["display"] or lx["lemma"], meaning, lx["pos"])
    with lock:
        record(
            conn,
            dest,
            subject=lx["id"],
            kind="image",
            spec={"prompt": codex.image_prompt(lx["display"] or lx["lemma"], meaning, lx["pos"])},
        )
        conn.commit()
    return True


def drain(conn, *, kinds=None, max_batches=None, image_limit=None):
    """Run bounded waves of provider calls; serialize every database write.

    Plan between waves so newly ready dependencies can run in study order.
    ``max_batches`` counts calls, not waves. Images keep their per-run cap.
    """
    if report.VERBOSE:
        print_banner("work — AI queue")
    kinds = kinds or ORDER
    if max_batches is not None and max_batches < 0:
        raise ValueError("batches must be nonnegative")
    database = Path(
        conn.execute("PRAGMA database_list").fetchone()[2]
        or Path(tempfile.gettempdir()) / "flashcards-memory"
    )
    summary = {}
    batch_no = 0
    lock = threading.Lock()
    client = AI(conn, lock)
    owner = uuid4().hex
    workers = max(1, settings.ai.concurrency)

    def work(kind, jobs, number, label):
        started = time.monotonic()
        if STOP.is_set():
            raise KeyboardInterrupt
        if kind == "lexeme_enrich":
            report.detail(f"  [{number}] Preparing {len(jobs)} word inputs …")
            jobs = _add_etymology_chains(conn, jobs, lock)
        if kind == "image":
            if not _run_image(jobs[0], conn, lock):
                raise ValueError("Image subject no longer exists")
            with lock, transaction(conn):
                conn.execute(
                    "UPDATE ai_jobs SET status='done',attempts=attempts+1,owner=NULL,"
                    "error=NULL,next_retry_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (jobs[0]["id"],),
                )
            done = 1
        else:
            done = _run_text_batch(client, kind, jobs, conn, lock)
        return done, time.monotonic() - started

    def release(jobs):
        with lock, transaction(conn):
            conn.executemany(
                "UPDATE ai_jobs SET status='pending',owner=NULL WHERE id=? AND owner=?",
                [(j["id"], owner) for j in jobs],
            )

    with writer_lock(database):
        # No previous worker can publish after this lock is acquired.
        conn.execute("UPDATE ai_jobs SET status='pending',owner=NULL WHERE status='running'")
        conn.commit()
        planning_started = time.monotonic()
        plan(conn)  # repair legacy inputs and cancel premature/obsolete jobs before claiming
        report.detail(
            f"  Planning: {time.monotonic() - planning_started:.1f}s · up to {workers} concurrent batches"
        )
        available_kinds = list(kinds)
        images_started = 0
        if "image" in available_kinds and image_limit == 0:
            available_kinds.remove("image")
            report.detail("  Images skipped ([run] image_limit = 0; pass --images N to make some).")
        elif "image" in available_kinds and not codex.logged_in():
            available_kinds.remove("image")
            report.line("images skipped: Codex is not logged in (run `codex login` once)")
        try:
            while available_kinds and (max_batches is None or batch_no < max_batches):
                wave = []
                wave_kinds = list(available_kinds)
                while wave_kinds and len(wave) < workers and (max_batches is None or batch_no < max_batches):
                    marks = ",".join("?" for _ in wave_kinds)
                    first = conn.execute(
                        f"SELECT * FROM ai_jobs WHERE status='pending' AND kind IN ({marks}) "
                        "AND (next_retry_at IS NULL OR next_retry_at<=datetime('now')) "
                        "ORDER BY priority, CASE kind WHEN 'lexeme_enrich' THEN 0 "
                        "WHEN 'phrase_enrich' THEN 0 WHEN 'image' THEN 2 ELSE 1 END,id LIMIT 1",
                        wave_kinds,
                    ).fetchone()
                    if not first:
                        break
                    kind = first["kind"]
                    band_end = (first["priority"] // 100_000 + 1) * 100_000
                    with transaction(conn):
                        # Isolate a previously failed text job on its next attempt;
                        # one bad member must not keep poisoning the same whole batch.
                        size = 1 if first["attempts"] else BATCH[kind]
                        jobs = conn.execute(
                            "SELECT * FROM ai_jobs WHERE status='pending' AND kind=? AND priority<? "
                            "AND (attempts>0)=? "
                            "AND (next_retry_at IS NULL OR next_retry_at<=datetime('now')) "
                            "ORDER BY priority,id LIMIT ?",
                            (kind, band_end, bool(first["attempts"]), size),
                        ).fetchall()
                        conn.executemany(
                            "UPDATE ai_jobs SET status='running',owner=?,started_at=CURRENT_TIMESTAMP WHERE id=?",
                            [(owner, j["id"]) for j in jobs],
                        )
                    batch_no += 1
                    label = _label(conn, kind, jobs)
                    wave.append((kind, jobs, batch_no, label))
                    if kind == "image":
                        images_started += 1
                        # Media subjects can share a destination. Publish one image
                        # per wave, alongside independent text work.
                        wave_kinds.remove("image")
                        if image_limit is not None and images_started >= image_limit:
                            available_kinds.remove("image")
                if not wave:
                    break
                report.line("asking " + " · ".join(_wave_text(wave)) + " …")
                # Planning is finished before threads start. Input updates, cache
                # writes, result transactions and failure accounting share one lock.
                pool = ThreadPoolExecutor(max_workers=workers)
                try:
                    futures = {pool.submit(work, *item): item for item in wave}
                    for future in as_completed(futures):
                        kind, jobs, number, label = futures[future]
                        counts = summary.setdefault(kind, {"done": 0, "failed": 0})
                        try:
                            done, seconds = future.result()
                        except AIUnavailable as exc:
                            release(jobs)
                            report.line(f"{KIND_NAME[kind]:<8}… put back for later — {exc}")
                            if kind == "image":
                                available_kinds = [k for k in available_kinds if k != "image"]
                            else:
                                available_kinds = [k for k in available_kinds if k == "image"]
                        except Exception as exc:
                            with lock, transaction(conn):
                                conn.executemany(
                                    "UPDATE ai_jobs SET status=CASE WHEN attempts>=2 THEN 'failed' ELSE 'pending' END,"
                                    "attempts=attempts+1,error=?,owner=NULL,next_retry_at=datetime('now','+5 minutes'),"
                                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                    [(str(exc)[:500], j["id"]) for j in jobs],
                                )
                            counts["failed"] += len(jobs)
                            report.line(
                                f"{KIND_NAME[kind]:<8}✗ {len(jobs):<4}{label[:34]:<34} failed: "
                                f"{str(exc)[:70]} (tried again later)"
                            )
                        else:
                            counts["done"] += done
                            with lock:  # workers share this connection
                                left = conn.execute(
                                    "SELECT count(*) FROM ai_jobs WHERE kind=? AND status IN ('pending','running')",
                                    (kind,),
                                ).fetchone()[0]
                            report.line(
                                f"{KIND_NAME[kind]:<8}✓ {done:<4}{label[:34]:<34} "
                                f"{report.duration(seconds):>6} · {left:,} left"
                            )
                except BaseException:
                    STOP.set()
                    raise
                finally:
                    # Join before releasing ownership; interrupted subprocesses use
                    # STOP to terminate their process groups. No late DB publisher.
                    pool.shutdown(wait=True, cancel_futures=True)
                planning_started = time.monotonic()
                plan(conn)
                report.detail(f"  Queue checkpoint · planning {time.monotonic() - planning_started:.1f}s")
        finally:
            # Preserve completed transactions; only unfinished claims are released.
            conn.rollback()
            with transaction(conn):
                conn.execute(
                    "UPDATE ai_jobs SET status='pending',owner=NULL WHERE status='running' AND owner=?",
                    (owner,),
                )
            from .lexicon import export_jsonl

            export_jsonl(conn)
        pending = conn.execute("SELECT count(*) FROM ai_jobs WHERE status='pending'").fetchone()[0]
        failed = conn.execute("SELECT count(*) FROM ai_jobs WHERE status='failed'").fetchone()[0]
        report.detail(
            f"  Queue remaining: {pending} pending (includes skipped images/deferred retries), {failed} failed"
        )
    return summary


def status(conn: sqlite3.Connection) -> list[tuple]:
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT kind, status, COUNT(*) FROM ai_jobs GROUP BY kind, status ORDER BY kind, status"
        )
    ]


def retry_failed(conn: sqlite3.Connection, kind: str | None = None) -> int:
    cur = conn.execute(
        "UPDATE ai_jobs SET status = 'pending', attempts = 0, next_retry_at=NULL WHERE status = 'failed'"
        + (" AND kind = ?" if kind else ""),
        (kind,) if kind else (),
    )
    conn.commit()
    return cur.rowcount


def refresh(conn: sqlite3.Connection, list_id: str) -> int:
    """Re-ask Claude for every root in a list (keeps human edits)."""
    ids = [r[0] for r in conn.execute("SELECT lexeme_id FROM list_items WHERE list_id = ?", (list_id,))]
    for lid in ids:
        conn.execute(
            "UPDATE ai_jobs SET status = 'pending', attempts = 0, force_refresh=1, next_retry_at=NULL WHERE subject = ? AND kind IN ('lexeme_enrich', 'phrase_enrich')",
            (lid,),
        )
        conn.execute("UPDATE lexemes SET status = 'new' WHERE id = ?", (lid,))
        # Keep the last complete senses while replacement content is pending.
    if ids:
        conn.execute("DELETE FROM metadata WHERE key='notes_generation'")
    conn.commit()
    return len(ids)


__all__ = [
    "plan",
    "drain",
    "status",
    "study_order",
    "full_form_verbs",
    "verb_forms",
    "plan_tenses",
    "MODEL_VERBS",
    "conjugation_class",
    "retry_failed",
    "refresh",
]
