"""Shared card budgeting and deterministic verb derivation, without provider calls.

Materialization is imported lazily from notes so jobs and publication select the
same candidates instead of maintaining separate root and card budgets.
"""

import json
import sqlite3
from .lists import load_plan
from .italian import compound, progressive, strip_clitic

MODEL_VERBS = {"-are": "parlare", "-ere": "credere", "-ire": "dormire", "-ire (isc)": "finire"}


def study_order(conn: sqlite3.Connection) -> dict[str, int]:
    """lexeme id → priority (lower = sooner), from plan.toml + list rank."""
    plan = load_plan()
    priorities = plan.get("priority", [])
    priorities = sorted(enumerate(priorities), key=lambda ip: (str(ip[1].get("by") or "9999-12-31"), ip[0]))
    order = [p["list"] for _i, p in priorities]
    pos_of = {lid: i for i, lid in enumerate(order)}
    out: dict[str, int] = {}
    for r in conn.execute("SELECT list_id, lexeme_id, rank FROM list_items"):
        p = pos_of.get(r["list_id"], len(order)) * 100_000 + r["rank"]
        if p < out.get(r["lexeme_id"], 1 << 60):
            out[r["lexeme_id"]] = p
    known = known_roots(conn)
    return {lid: value + (100_000_000 if lid in known else 0) for lid, value in out.items()}


def known_roots(conn):
    # Recognition of a vocabulary/phrase sense is evidence of movie comprehension.
    # Form/noun-drill mastery and production are intentionally distinct skills.
    active = {
        f"{'phrase' if r['pos'] == 'phrase' else 'vocab'}:{r['lexeme_id']}:{r['idx']}": r["lexeme_id"]
        for r in conn.execute(
            "SELECT s.lexeme_id,s.idx,l.pos FROM senses s JOIN lexemes l ON l.id=s.lexeme_id "
            "WHERE s.active=1 AND s.idx=(SELECT min(s2.idx) FROM senses s2 "
            "WHERE s2.lexeme_id=s.lexeme_id AND s2.active=1)"
        )
    }
    # notes.vocab_and_phrases gives only the first active sense a Recognition
    # card, combining the answers there. Secondary senses are Production-only.
    # Numbers and letters have no senses table row but do have recognition notes.
    for row in conn.execute(
        "SELECT n.key,n.lexeme_id FROM v4_notes n JOIN lexemes l ON l.id=n.lexeme_id "
        "WHERE l.pos IN ('num','letter') AND json_extract(n.fields_json,'$.Recognition')='1'"
    ):
        active[row["key"]] = row["lexeme_id"]
    states = {
        r["key"]
        for r in conn.execute(
            "SELECT key FROM card_knowledge WHERE direction='Recognition' AND state='known' AND suspended=0"
        )
    }
    by_root = {}
    for key, lid in active.items():
        by_root.setdefault(lid, []).append(key in states)
    return {lid for lid, values in by_root.items() if all(values)}


def horizon_budget(plan: dict | None = None) -> int | None:
    """New-card slots ahead of what is already in Anki, or None (no horizon).

    Opt-in via ``horizon_days`` in plan.toml. Without it every root is worked
    on in study order and Anki's daily new-card limit does the pacing.
    """
    plan = load_plan() if plan is None else plan
    days = plan.get("horizon_days")
    return None if not days else plan.get("new_cards_per_day", 25) * days


def published_directions(conn):
    result = {(r["key"], r["direction"]) for r in conn.execute("SELECT key,direction FROM adoptions")}
    result |= {(r["key"], r["direction"]) for r in conn.execute("SELECT key,direction FROM card_knowledge")}
    # Old identity rows may predate card_knowledge; recover their stored directions.
    for row in conn.execute(
        "SELECT n.key,n.fields_json FROM identity i JOIN v4_notes n ON n.key=i.key WHERE i.kind='v4'"
    ):
        fields = json.loads(row["fields_json"])
        result |= {(row["key"], d) for d in ("Recognition", "Production") if fields.get(d)}
    return result


def card_plan(conn, candidates=None):
    """Actual note directions, shared by AI selection and publication.

    Missing enrichment is represented by its current placeholder; after enrichment
    the expanded candidates are budgeted again. No estimated drill counts are used.
    """
    if candidates is None:
        from .notes import candidates as materialize

        candidates = materialize(conn).notes
    budget = horizon_budget()
    remaining = budget
    published = published_directions(conn)
    keys, roots, verbs, costs = set(), set(), set(), {}
    total = 0
    for note in sorted(candidates, key=lambda n: (n["sort"], n["key"])):
        directions = {(note["key"], d) for d in ("Recognition", "Production") if note["fields"].get(d)}
        cost = len(directions - published)
        costs[note["key"]] = cost
        maintained = bool(directions & published)
        if remaining is not None and cost > remaining and not maintained:
            continue
        # Published notes continue to be maintained, but newly enabled directions
        # consume slots too. No new directions are enabled on a maintenance exception.
        if remaining is not None and cost > remaining:
            for _key, d in directions - published:
                note["fields"][d] = ""
            cost = 0
        if remaining is not None:
            remaining -= cost
        total += cost
        keys.add(note["key"])
        if note["lexeme_id"]:
            roots.add(note["lexeme_id"])
            if note["card_type"] == "form":
                verbs.add(note["lexeme_id"])
    return {
        "keys": keys,
        "roots": roots,
        "verbs": verbs,
        "new_directions": total,
        "budget": budget,
        "remaining": remaining,
        "costs": costs,
    }


def selected_roots(conn):
    return card_plan(conn)["roots"]


# ── Verb budget (also used by notes.py) ───────────────────────────────────


def full_form_verbs(conn: sqlite3.Connection) -> set[str]:
    """Verbs that get full conjugation cards: model verbs, irregular verbs,
    and the most frequent N (plan.toml [conjugation] full_forms_top)."""
    plan = load_plan()
    top = int(plan.get("conjugation", {}).get("full_forms_top", 120))
    rows = conn.execute(
        """SELECT id, lemma, irregular, freq_rank FROM lexemes
           WHERE pos = 'verb' AND forms_json IS NOT NULL
             AND (id IN (SELECT lexeme_id FROM list_items) OR lemma IN (?, ?, ?, ?))""",
        tuple(MODEL_VERBS.values()),
    ).fetchall()
    ranked = sorted((r for r in rows if r["freq_rank"]), key=lambda r: r["freq_rank"])[:top]
    keep = {r["id"] for r in ranked}
    keep |= {r["id"] for r in rows if r["irregular"]}
    models = (
        {r["id"] for r in rows if r["lemma"] in MODEL_VERBS.values()}
        if plan.get("cards", {}).get("patterns", True)
        else set()
    )
    order = study_order(conn)
    cap = plan.get("conjugation", {}).get("max_full_verbs", 120)
    published = {
        key.split(":")[1] for key, _direction in published_directions(conn) if key.startswith("form:")
    }
    published |= {
        r[0].split(":")[1]
        for r in conn.execute("SELECT key FROM identity WHERE kind='v4' AND key LIKE 'form:%'")
    }
    maintained = published & {r["id"] for r in rows}
    fixed = models | maintained
    return fixed | set(
        sorted(keep - fixed, key=lambda lid: (order.get(lid, 1 << 30), lid))[: max(0, cap - len(fixed))]
    )


def plan_tenses() -> list[str]:
    return list(
        load_plan()
        .get("conjugation", {})
        .get(
            "tenses",
            [
                "presente",
                "passato_prossimo",
                "imperfetto",
                "futuro_semplice",
                "presente_progressivo",
                "imperativo",
                "condizionale_presente",
                "condizionale_passato",
            ],
        )
    )


def verb_forms(lexeme: sqlite3.Row, sense=None) -> dict[str, dict[str, tuple[str, str]]]:
    """{tense: {person: (display, audio)}} for the plan's tenses — simple
    tenses from Wiktionary, compound tenses built by rule."""
    from .semantics import usage

    info = json.loads(lexeme["forms_json"] or "{}")
    forms = info.get("forms", {})
    reflexive = bool(info.get("reflexive"))
    aux = usage(lexeme, sense).get("auxiliary") or info.get(
        "auxiliary"
    )  # unknown/dual auxiliaries need a sense-specific human choice
    pp = strip_clitic(info.get("past_participle") or "", reflexive)
    ger = strip_clitic(info.get("gerund") or "", reflexive)
    out: dict[str, dict[str, tuple[str, str]]] = {}
    for tense in plan_tenses():
        if tense in forms:
            out[tense] = {p: (f, f) for p, f in forms[tense].items()}
        elif tense == "passato_prossimo" and pp and aux in ("avere", "essere"):
            out[tense] = {
                p: compound(aux, "presente", pp, p, reflexive)
                for p in ("io", "tu", "lui_lei", "noi", "voi", "loro")
            }
        elif tense == "condizionale_passato" and pp and aux in ("avere", "essere"):
            out[tense] = {
                p: compound(aux, "condizionale", pp, p, reflexive)
                for p in ("io", "tu", "lui_lei", "noi", "voi", "loro")
            }
        elif tense == "presente_progressivo" and ger:
            out[tense] = {
                p: (progressive(ger, p, reflexive),) * 2
                for p in ("io", "tu", "lui_lei", "noi", "voi", "loro")
            }
    return out
