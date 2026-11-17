"""Human overrides and stable sense identities, independent of generated values."""

from __future__ import annotations

import json
import unicodedata

SIMPLE = {"display", "gender", "plural", "english_plural", "ipa", "etymology"}
SUPPORTED = SIMPLE | {"forms", "senses", "fact"}


def is_human(conn, lexeme_id: str, field: str) -> bool:
    if conn.execute("SELECT 1 FROM overrides WHERE lexeme_id=? AND field=?", (lexeme_id, field)).fetchone():
        return True
    row = conn.execute("SELECT provenance FROM lexemes WHERE id=?", (lexeme_id,)).fetchone()
    return bool(row and json.loads(row[0] or "{}").get(field) == "human")


def set_override(conn, lexeme_id, field, value, *, reason="JSONL edit", author="human"):
    if field not in SUPPORTED:
        raise ValueError(f"Unsupported human field {field!r}; supported: {sorted(SUPPORTED)}")
    if field in SIMPLE and value is not None and not isinstance(value, str):
        raise ValueError(f"{field} must be text or null")
    if field in {"forms", "fact"} and not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    if field == "senses":
        if (
            not isinstance(value, list)
            or not value
            or any(not isinstance(s, dict) or not (s.get("prompt") or "").strip() for s in value)
        ):
            raise ValueError("Human senses require nonempty prompts")
    conn.execute(
        """INSERT INTO overrides(lexeme_id,field,value_json,author,reason) VALUES(?,?,?,?,?)
        ON CONFLICT(lexeme_id,field) DO UPDATE SET value_json=excluded.value_json,
        author=excluded.author,reason=excluded.reason,version=overrides.version+1,updated_at=CURRENT_TIMESTAMP
        WHERE overrides.value_json != excluded.value_json""",
        (lexeme_id, field, json.dumps(value, ensure_ascii=False), author, reason),
    )


def _normal(text):
    return " ".join(unicodedata.normalize("NFC", text).casefold().split())


def save_senses(conn, lexeme_id, values, provenance):
    """Store a word's meanings. The first is the main meaning and always idx 0;
    further meanings keep the idx of the old meaning they match (by dictionary
    source or wording), else get a new one.

    Human edits may explicitly retain idx with same_meaning=true.
    """
    from .util import md5_hex

    old = [dict(r) for r in conn.execute("SELECT * FROM senses WHERE lexeme_id=? ORDER BY idx", (lexeme_id,))]
    by_idx = {s["idx"]: s for s in old}
    by_prompt = {_normal(s["prompt"]): s for s in old}
    by_source = {
        s["source_id"]: s
        for s in old
        if s["source_id"] and not s["source_id"].startswith(("legacy:", "unverified:", "input:"))
    }
    next_idx = max(by_idx, default=-1) + 1
    wanted, claimed, prompts = [], set(), set()
    for value in values:
        s = dict(value)
        prompt = s["prompt"].strip()
        source = s.get("source_id")
        signature = (_normal(prompt), source)
        if signature in prompts:
            continue
        prompts.add(signature)
        match = by_source.get(source) or by_prompt.get(_normal(prompt))
        if provenance == "human" and "idx" in s:
            previous = by_idx.get(int(s["idx"]))
            if previous and _normal(previous["prompt"]) != _normal(prompt) and not s.get("same_meaning"):
                raise ValueError(
                    "Rewording an explicit sense idx requires same_meaning=true after review; omit idx for a new meaning"
                )
            match = previous
        # The same words can still refer to different dictionary senses.
        if (
            match
            and source
            and match["source_id"]
            and source != match["source_id"]
            and not match["source_id"].startswith(("legacy:", "unverified:", "input:"))
        ):
            match = None
        idx = match["idx"] if match else next_idx
        if not match:
            next_idx += 1
        if idx in claimed:
            raise ValueError("Repeated sense identity in response")
        claimed.add(idx)
        source = (
            source
            or (match["source_id"] if match else None)
            or "unverified:" + md5_hex(lexeme_id + ":" + _normal(prompt))
        )
        # Legacy IDs cannot be used to smuggle a new meaning onto an old idx.
        if not match and source.startswith("legacy:"):
            source = "unverified:" + md5_hex(lexeme_id + ":" + _normal(prompt))
        context = s.get("context") or (match["context"] if match else None)
        if isinstance(context, (dict, list)):
            context = json.dumps(context, ensure_ascii=False, sort_keys=True)
        feats = s.get("features", json.loads(match["features"] or "{}") if match else {})
        from .tasks import SENSE_FEATURES
        from .validation import validate

        validate(feats, {**SENSE_FEATURES, "required": []}, "sense.features")
        wanted.append(
            [idx, prompt, s, source, context, json.dumps(feats, ensure_ascii=False, sort_keys=True)]
        )
    # The first meaning is the card's main meaning and always idx 0, so its note
    # keys (vocab:<id>:0, verb forms, noun phrases) never change when a new answer
    # picks another dictionary sense: the existing Anki notes are updated in place.
    if wanted and wanted[0][0] != 0:
        # Swap places with whatever held idx 0, keeping the old meaning (archived
        # or not) under the new main meaning's former idx.
        moved = wanted[0][0]
        conn.execute("UPDATE senses SET idx=-1 WHERE lexeme_id=? AND idx=0", (lexeme_id,))
        conn.execute("UPDATE senses SET idx=0 WHERE lexeme_id=? AND idx=?", (lexeme_id, moved))
        conn.execute("UPDATE senses SET idx=? WHERE lexeme_id=? AND idx=-1", (moved, lexeme_id))
        for other in wanted[1:]:
            if other[0] == 0:
                other[0] = moved
        wanted[0][0] = 0
    conn.execute("UPDATE senses SET active=0 WHERE lexeme_id=?", (lexeme_id,))
    for idx, prompt, s, source, context, feats in wanted:
        conn.execute(
            """INSERT INTO senses(lexeme_id,idx,prompt,hint,register,note,also,provenance,active,source_id,context,features)
            VALUES(?,?,?,?,?,?,?,?,1,?,?,?) ON CONFLICT(lexeme_id,idx) DO UPDATE SET
            prompt=excluded.prompt,hint=excluded.hint,register=excluded.register,note=excluded.note,
            also=excluded.also,provenance=excluded.provenance,active=1,source_id=excluded.source_id,
            context=excluded.context,features=excluded.features""",
            (
                lexeme_id,
                idx,
                prompt,
                s.get("hint"),
                s.get("register"),
                s.get("note"),
                s.get("also"),
                provenance,
                source,
                context,
                feats,
            ),
        )


def apply(conn, lexeme_id: str | None = None):
    sql = "SELECT * FROM overrides" + (" WHERE lexeme_id=?" if lexeme_id else "")
    for row in conn.execute(sql, (lexeme_id,) if lexeme_id else ()).fetchall():
        lid, field, value = row["lexeme_id"], row["field"], json.loads(row["value_json"])
        if field in SIMPLE:
            conn.execute(f"UPDATE lexemes SET {field}=? WHERE id=?", (value, lid))
        elif field == "forms":
            conn.execute(
                "UPDATE lexemes SET forms_json=? WHERE id=?", (json.dumps(value, ensure_ascii=False), lid)
            )
        elif field == "senses":
            save_senses(conn, lid, value, "human")
        elif field == "fact":
            from .facts import normalise

            lemma = conn.execute("SELECT lemma FROM lexemes WHERE id=?", (lid,)).fetchone()[0]
            conn.execute(
                "INSERT OR REPLACE INTO word_facts(word,has_fact,kind,fact,confidence,model) VALUES(?,?,?,?,?,?)",
                (
                    normalise(lemma),
                    bool(value.get("text")),
                    value.get("kind"),
                    value.get("text"),
                    value.get("confidence", 1),
                    "human",
                ),
            )
        prov = json.loads(
            conn.execute("SELECT provenance FROM lexemes WHERE id=?", (lid,)).fetchone()[0] or "{}"
        )
        prov[field] = "human"
        conn.execute("UPDATE lexemes SET provenance=? WHERE id=?", (json.dumps(prov, sort_keys=True), lid))
