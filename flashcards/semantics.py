"""Meaning evidence and conservative, sense-specific grammar constraints.

Legacy identities remain usable but are explicitly unverified. New dictionary
identities include the entry and gloss: a different homonym cannot inherit them.
"""

import json
import re

from .util import md5_hex


def normal(value):
    return " ".join(value.casefold().split())


def candidate_entries(lemma, pos):
    from . import kaikki
    from .lexicon import _KAIKKI_FOR

    want = _KAIKKI_FOR.get(pos)
    return (kaikki.entries(lemma, want) if want else []) or (
        kaikki.entries(lemma, "det") if want == "article" else []
    )


def dictionary_candidates(entries):
    from . import kaikki

    result = {}
    for entry in entries:
        anchor = [entry.get("word"), entry.get("pos"), entry.get("etymology_text", "")]
        for sense in entry.get("senses", []):
            glosses = sense.get("glosses", [])
            if not glosses:
                continue
            identity = "dictionary:" + md5_hex(
                json.dumps([anchor, glosses], sort_keys=True, ensure_ascii=False)
            )
            result[identity] = {
                "source_id": identity,
                "glosses": glosses,
                "tags": sense.get("tags", []),
                "word": entry.get("word"),
                "pos": entry.get("pos"),
                "noun_forms": kaikki.noun_forms(entry) if entry.get("pos") == "noun" else [],
            }
    return list(result.values())


def evidence(conn, lx):
    candidates = dictionary_candidates(candidate_entries(lx["lemma"], lx["pos"])) if lx["in_kaikki"] else []
    observations = []
    for row in conn.execute(
        "SELECT list_id,row_key,payload FROM source_observations WHERE lexeme_id=? ORDER BY list_id,row_key",
        (lx["id"],),
    ):
        item = json.loads(row["payload"])
        observations.append({"list": row["list_id"], "row": row["row_key"]})
        if item.get("english"):
            identity = "source:" + md5_hex(
                json.dumps([lx["id"], item["english"], item.get("context", {})], sort_keys=True)
            )
            candidates.append(
                {
                    "source_id": identity,
                    "glosses": [item["english"]],
                    "tags": [],
                    "context": item.get("context", {}),
                }
            )
    # An unchanged input phrase is evidence even without a dictionary entry.
    if not candidates:
        candidates = [
            {"source_id": "input:" + md5_hex(lx["id"] + ":" + lx["lemma"]), "glosses": [], "tags": []}
        ]
    return {"candidates": candidates, "observations": observations}


def primary(conn, lid):
    return conn.execute(
        "SELECT * FROM senses WHERE lexeme_id=? AND active=1 ORDER BY idx LIMIT 1", (lid,)
    ).fetchone()


def features(sense):
    return json.loads(sense["features"] or "{}") if sense and "features" in sense.keys() else {}


def usage(lx, sense=None):
    """Constraints attach to meaning, never to every meaning of a homonymous verb."""
    result = dict(features(sense))
    meaning = normal(sense["prompt"]) if sense else ""
    # An event can happen in singular or plural; first/second person drills are
    # inappropriate for this meaning (succedere also has succession meanings).
    alternatives = {m.strip() for m in re.split(r"[/;,]", meaning)}
    if (
        lx["lemma"] in {"succedere", "accadere", "avvenire", "capitare"}
        and alternatives
        and alternatives <= {"to happen", "happen", "to occur", "occur", "to come about", "come about"}
    ):
        result.update(
            persons=["lui_lei", "loro"],
            auxiliary="essere",
            construction="event subject; it happens / they happen",
            no_imperative=True,
        )
    return result


def exclusion(lx, sense, tense, person):
    rules = usage(lx, sense)
    if rules.get("persons") and person not in rules["persons"]:
        return "Person is not used with the selected meaning"
    if tense == "imperativo" and (rules.get("no_imperative") or lx["lemma"] in {"dovere", "potere"}):
        return "No ordinary imperative exercise for this meaning"
    return None


def form_suffix(sense):
    # Keep historical keys for the original meaning only. New meanings must not
    # rewrite an old conjugation/noun drill's study history.
    return f":sense-{sense['idx']}" if sense and sense["idx"] else ""


def noun_variant(lx, sense, plural=False):
    f = features(sense)
    info = json.loads(lx["forms_json"] or "{}")
    variants = info.get("noun_forms", [])
    if not plural:
        return (lx["lemma"], f.get("singular_gender") or lx["gender"], sense["prompt"])
    word = f.get("plural") or lx["plural"]
    english = f.get("english_plural") or lx["english_plural"]
    gender = f.get("plural_gender")
    if not gender:
        matches = [v for v in variants if v.get("number") == "plural" and v.get("form") == word]
        genders = {v.get("gender") for v in matches if v.get("gender")}
        if len(genders) == 1:
            gender = genders.pop()
        elif len(genders) > 1 or len({v.get("form") for v in variants if v.get("number") == "plural"}) > 1:
            return None  # a sense must select among distinct plurals
        else:
            gender = lx["gender"]
    # Common gender-changing plurals cannot inherit the singular masculine.
    if word in {"braccia", "uova", "dita", "ginocchia", "labbra", "ossa", "mura", "lenzuola"}:
        gender = "feminine"
    if not word or not english or gender not in {"masculine", "feminine"}:
        return None
    if len({v.get("form") for v in variants if v.get("number") == "plural"}) > 1 and not f.get("plural"):
        return None
    return (word, gender, english)


def note_hold(conn, key):
    """A known unsuitable published form is suspended, preserving its review log."""
    if key.startswith("nphrase:"):
        parts = key.split(":")
        if len(parts) >= 4 and parts[3] == "plural":
            sense = primary(conn, parts[1])
            lx = conn.execute("SELECT * FROM lexemes WHERE id=?", (parts[1],)).fetchone()
            if (
                sense
                and lx
                and ":".join(parts[4:]) == form_suffix(sense).lstrip(":")
                and not noun_variant(lx, sense, plural=True)
            ):
                return "Choose a meaning-specific plural and gender before publishing this drill"
        return None
    if not key.startswith("form:"):
        return None
    parts = key.split(":")
    if len(parts) < 4:
        return None
    _, lid, tense, person, *suffix = parts
    sense = primary(conn, lid)
    if not sense or ":".join(suffix) != form_suffix(sense).lstrip(":"):
        return None
    lx = conn.execute("SELECT * FROM lexemes WHERE id=?", (lid,)).fetchone()
    reason = exclusion(lx, sense, tense, person)
    if reason:
        return reason
    row = conn.execute(
        "SELECT reason FROM form_exclusions WHERE lexeme_id=? AND tense=? AND person=? AND source_id=?",
        (lid, tense, person, sense["source_id"]),
    ).fetchone()
    return row[0] if row else None
