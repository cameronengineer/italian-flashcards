"""Anki as a reconciled projection: ``plan`` (dry run) and ``apply``.

Compares the desired notes (``v4_notes``) with what Anki holds and makes
targeted changes through AnkiConnect — no ``.apkg`` import, so the
pipeline owns its notetype and never gets surprise notetype copies.

Rules (safety first):

* Only notes on the pipeline's notetypes, in the pipeline's decks
  (``Italian::*`` and the legacy v3 decks), are ever touched.
* **Studied notes are never deleted.** A legacy note you've reviewed is
  *adopted*: its content is updated in place, it moves into the v4 deck,
  and the matching direction of the new v4 note is switched off so you
  never study the same thing twice.
* An unstudied legacy note is retired only once its v4 replacement is in
  Anki — or when nothing in v4 replaces it — so new cards never run dry.
* Large retirements need ``--allow-retire`` (limits in settings.toml [sync]).
* Media is uploaded once per file and never deleted.
* A drift guard refuses to run if many notes Anki should have are missing
  (e.g. a different Anki profile is open).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .anki import chunks, invoke, pipeline_models, quote
from .notes import CSS, FIELDS, MODEL_NAME, TEMPLATES
from .paths import AUDIO_DIR, AUDIO_DIR_COMPRESSED, IMAGE_DIR, IMAGE_DIR_COMPRESSED
from .settings import settings
from .util import print_banner

V4_QUERY = quote(f"note:{MODEL_NAME}")
_IMG = re.compile(r'<img src="([^"]+)"')
_SND = re.compile(r"\[sound:([^\]]+)\]")


@dataclass
class AnkiNote:
    note_id: int
    key: str
    fields: dict
    tags: list[str]
    cards: list[int]
    decks: set[str] = field(default_factory=set)
    templates: dict[int, str] = field(default_factory=dict)  # card id → template name


@dataclass
class Plan:
    model: str = "ok"
    add: list[dict] = field(default_factory=list)
    update: list[tuple[AnkiNote, dict]] = field(default_factory=list)
    move: list[tuple[list[int], str]] = field(default_factory=list)
    adopt: list[tuple[AnkiNote, dict, str]] = field(default_factory=list)  # legacy note, desired row, template
    retire_v4: list[AnkiNote] = field(default_factory=list)
    keep_v4_studied: list[AnkiNote] = field(default_factory=list)
    retire_legacy: list[AnkiNote] = field(default_factory=list)
    keep_legacy_studied: int = 0
    waiting: int = 0
    media: set[str] = field(default_factory=set)
    present: set[str] = field(default_factory=set)  # v4 keys already in Anki

    def summary(self) -> str:
        return (f"model: {self.model} · add {len(self.add)} · update {len(self.update)} · "
                f"move {sum(len(c) for c, _ in self.move)} cards · adopt {len(self.adopt)} studied legacy notes · "
                f"retire {len(self.retire_v4)} v4 + {len(self.retire_legacy)} legacy (unstudied) · "
                f"keep {len(self.keep_v4_studied) + self.keep_legacy_studied} studied without a v4 home · "
                f"{self.waiting} notes still waiting (AI/image) · upload {len(self.media)} media files")


# ── Anki state ─────────────────────────────────────────────────────────────


def _actions() -> set[str]:
    try:
        return set(invoke("apiReflect", scopes=["actions"], actions=None)["actions"])
    except RuntimeError:
        return set()


def _notes(query: str, key_field: str) -> list[AnkiNote]:
    ids = invoke("findNotes", query=query)
    out: list[AnkiNote] = []
    for chunk in chunks(ids, 500):
        for n in invoke("notesInfo", notes=chunk):
            if not n:
                continue
            fields = {k: v["value"] for k, v in n.get("fields", {}).items()}
            out.append(AnkiNote(n["noteId"], fields.get(key_field, "").strip(), fields, n.get("tags", []), n.get("cards", [])))
    cids = [c for n in out for c in n.cards]
    deck_of: dict[int, str] = {}
    for chunk in chunks(cids, 5000):
        for deck, cs in invoke("getDecks", cards=chunk).items():
            for c in cs:
                deck_of[c] = deck
    for n in out:
        n.decks = {deck_of.get(c, "") for c in n.cards}
    return out


def _studied(card_ids: list[int], query: str) -> set[int]:
    """Cards with any study history: not new, or with review-log entries."""
    studied = set(invoke("findCards", query=f"{query} -is:new"))
    for chunk in chunks(card_ids, 2000):
        for cid, revs in invoke("getReviewsOfCards", cards=chunk).items():
            if revs:
                studied.add(int(cid))
    return studied


def _legacy_decks(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT deck FROM cards")]


def _legacy_query(conn) -> str | None:
    models = [m for m in pipeline_models()]
    if not models:
        return None
    decks = _legacy_decks(conn) + ["Italian::*"]
    return ("(" + " OR ".join(quote(f"note:{m}") for m in models) + ") ("
            + " OR ".join(quote(f"deck:{d}") for d in decks) + ")")


# ── Legacy mapping ─────────────────────────────────────────────────────────


def legacy_target(conn, sort_key: str) -> tuple[str, str] | None:
    """(v4 key, template) a legacy note (SortKey ``natural_key|direction``) maps to."""
    nk, _, direction = sort_key.rpartition("|")
    if not nk:
        return None
    template = "Recognition" if direction == "it_to_en" else "Production"

    def lexeme_of(entry_id: str) -> tuple[str, str] | None:
        r = conn.execute("SELECT l.id, l.pos FROM entry_lexeme el JOIN lexemes l ON l.id = el.lexeme_id WHERE el.entry_id = ?",
                         (entry_id,)).fetchone()
        return (r["id"], r["pos"]) if r else None

    kind, _, rest = nk.partition(":")
    if kind in ("gloss", "verb_infinitive", "avere"):
        hit = lexeme_of(rest.split(":")[0])
        if not hit:
            return None
        ct = "phrase" if hit[1] == "phrase" else "vocab"
        return f"{ct}:{hit[0]}:0", template
    if kind == "verb_form" and template == "Production":
        r = conn.execute("SELECT entry_id, tense, person FROM verb_forms WHERE card_key = ?", (nk,)).fetchone()
        if r:
            hit = lexeme_of(r["entry_id"])
            if hit:
                return f"form:{hit[0]}:{r['tense']}:{r['person']}", template
    return None


def _legacy_fields(desired: dict, template: str) -> dict:
    """Map v4 fields onto the legacy notetype for an adopted note."""
    f = desired
    back = "".join(x for x in (
        f'<div class="details">{f["Details"]}</div>' if f.get("Details") else "",
        f'<div class="details">{f["Forms"]}</div>' if f.get("Forms") else "",
        f'<div class="details">{f["Note"]}</div>' if f.get("Note") else "",
        (f'<div class="fact"><span class="fact-title">{f["FactTitle"]}</span>{f["Fact"]}</div>' if f.get("Fact") else ""),
    ))
    if template == "Recognition":
        return {"FrontText": f["Italian"], "BackHighlight": f["English"], "FrontAudio": f["Audio"],
                "Audio": "", "FrontLabels": f["Labels"], "BackText": back, "Image": f["Image"]}
    return {"FrontText": f["English"] + (f' <span class="hint">({f["Hint"]})</span>' if f.get("Hint") else ""),
            "BackHighlight": f["Italian"], "FrontAudio": "", "Audio": f["Audio"],
            "FrontLabels": f["Labels"], "BackText": back, "Image": f["Image"]}


# ── Planning ───────────────────────────────────────────────────────────────


def _media_names(fields: dict) -> set[str]:
    text = " ".join(fields.values())
    return set(_IMG.findall(text)) | set(_SND.findall(text))


def build_plan(conn: sqlite3.Connection, *, anki_media: set[str]) -> Plan:
    plan = Plan()
    desired = {r["key"]: r for r in conn.execute("SELECT * FROM v4_notes")}
    plan.waiting = sum(1 for r in desired.values() if not r["ready"])

    v4 = {n.key: n for n in _notes(V4_QUERY, "Key")} if MODEL_NAME in invoke("modelNames") else {}
    plan.present = set(v4)
    legacy_q = _legacy_query(conn)
    legacy = _notes(legacy_q, "SortKey") if legacy_q else []
    all_cards = [c for n in list(v4.values()) + legacy for c in n.cards]
    studied = _studied(all_cards, "(" + V4_QUERY + (" OR " + legacy_q if legacy_q else "") + ")") if all_cards else set()

    # studied legacy notes → adoption per (v4 key, template)
    adopt: dict[str, dict[str, AnkiNote]] = defaultdict(dict)
    legacy_targets: dict[int, tuple[str, str] | None] = {}
    for n in legacy:
        tgt = legacy_target(conn, n.key) if "|" in n.key else None
        legacy_targets[n.note_id] = tgt
        if tgt and any(c in studied for c in n.cards) and tgt[0] in desired:
            current = adopt[tgt[0]].get(tgt[1])
            if current is None or len(n.cards) > len(current.cards):
                adopt[tgt[0]][tgt[1]] = n

    for key, row in desired.items():
        fields = json.loads(row["fields_json"])
        tags = row["tags"].split()
        taken = adopt.get(key, {})
        for template in taken:
            fields[template] = ""  # that direction is the adopted legacy note
        for template, ln in taken.items():
            plan.adopt.append((ln, {**fields, "_deck": row["deck"], "_tags": tags}, template))
        if not row["ready"]:
            continue
        if not fields["Recognition"] and not fields["Production"]:
            continue  # both directions covered by studied legacy notes
        plan.media |= _media_names(fields) - anki_media
        existing = v4.get(key)
        if existing is None:
            plan.add.append({"deckName": row["deck"], "modelName": MODEL_NAME, "fields": fields,
                             "tags": tags, "options": {"allowDuplicate": True}, "_sort": row["sort_order"]})
            continue
        if any(existing.fields.get(f, "") != fields.get(f, "") for f in FIELDS) or set(existing.tags) != set(tags):
            plan.update.append((existing, {"fields": fields, "tags": tags}))
        if existing.decks != {row["deck"]}:
            plan.move.append((existing.cards, row["deck"]))

    for key, n in v4.items():
        if key not in desired:
            (plan.keep_v4_studied if any(c in studied for c in n.cards) else plan.retire_v4).append(n)

    present_after = set(v4) | {a["fields"]["Key"] for a in plan.add}
    adopted_ids = {ln.note_id for ln, _f, _t in plan.adopt}
    for n in legacy:
        if n.note_id in adopted_ids:
            continue
        if any(c in studied for c in n.cards):
            plan.keep_legacy_studied += 1
            continue
        tgt = legacy_targets.get(n.note_id)
        if tgt is None or tgt[0] in present_after:
            plan.retire_legacy.append(n)
    return plan


# ── Apply ──────────────────────────────────────────────────────────────────


def ensure_model() -> str:
    if MODEL_NAME not in invoke("modelNames"):
        invoke("createModel", modelName=MODEL_NAME, inOrderFields=FIELDS, css=CSS,
               isCloze=False, cardTemplates=TEMPLATES)
        return "created"
    have = invoke("modelFieldNames", modelName=MODEL_NAME)
    for i, f in enumerate(FIELDS):
        if f not in have:
            invoke("modelFieldAdd", modelName=MODEL_NAME, fieldName=f, index=i)
    want = {t["Name"]: {"Front": t["Front"], "Back": t["Back"]} for t in TEMPLATES}
    changed = []
    if invoke("modelTemplates", modelName=MODEL_NAME) != want:
        invoke("updateModelTemplates", model={"name": MODEL_NAME, "templates": want})
        changed.append("templates")
    if invoke("modelStyling", modelName=MODEL_NAME).get("css") != CSS:
        invoke("updateModelStyling", model={"name": MODEL_NAME, "css": CSS})
        changed.append("css")
    return "updated " + "+".join(changed) if changed else "ok"


def _media_path(name: str) -> Path | None:
    for d in (IMAGE_DIR_COMPRESSED, AUDIO_DIR_COMPRESSED, AUDIO_DIR, IMAGE_DIR):
        p = d / name
        if p.exists() and p.stat().st_size > 0:
            return p
    if name.endswith(".jpg"):  # compress a new PNG on the fly (originals kept)
        png = IMAGE_DIR / (name[:-4] + ".png")
        if png.exists():
            from .commands.media import _compress_image

            _compress_image(png)
            jpg = IMAGE_DIR_COMPRESSED / name
            return jpg if jpg.exists() else None
    return None


def _upload_media(names: set[str]) -> int:
    n = 0
    for name in sorted(names):
        p = _media_path(name)
        if p:
            invoke("storeMediaFile", filename=name, path=str(p.resolve()))
            n += 1
    return n


def _multi(actions: list[dict]) -> None:
    for chunk in chunks(actions, 200):
        invoke("multi", actions=chunk, timeout=300)


def _drift_guard(conn, v4_keys_in_anki: set[str]) -> str | None:
    expected = {r[0] for r in conn.execute("SELECT key FROM identity WHERE kind = 'v4'")}
    still_wanted = {r[0] for r in conn.execute("SELECT key FROM v4_notes")}
    missing = (expected & still_wanted) - v4_keys_in_anki
    if expected and len(missing) > max(20, 0.05 * len(expected)):
        return (f"{len(missing)} of {len(expected)} v4 notes that were in Anki are gone — is a "
                f"different Anki profile open, or were notes deleted by hand? Refusing to sync.")
    return None


def run(conn: sqlite3.Connection, *, dry_run: bool = False, allow_retire: bool = False) -> Plan:
    print_banner("anki — plan" if dry_run else "anki — apply")
    model = "ok" if MODEL_NAME in invoke("modelNames") else ("would create" if dry_run else "")
    if not dry_run:
        model = ensure_model()
    anki_media = set(invoke("getMediaFilesNames", pattern="*"))
    plan = build_plan(conn, anki_media=anki_media)
    plan.model = model or plan.model
    print("  " + plan.summary())
    drift = _drift_guard(conn, plan.present)
    if dry_run:
        for title, items in (("add", [a["fields"]["Key"] for a in plan.add[:5]]),
                             ("retire legacy", [n.key for n in plan.retire_legacy[:5]])):
            if items:
                print(f"  e.g. {title}: {items}")
        return plan
    limit = settings.sync.orphan_absolute_limit
    total_retire = len(plan.retire_v4) + len(plan.retire_legacy)
    if total_retire > limit and not allow_retire:
        print(f"  NOT retiring {total_retire} unstudied notes (> {limit}); re-run with --allow-retire. "
              "Adds, updates and adoption go ahead.")
        plan.retire_v4, plan.retire_legacy = [], []
    if drift:
        print("  " + drift)
        return plan

    uploaded = _upload_media(plan.media)
    for deck in {a["deckName"] for a in plan.add} | {d for _c, d in plan.move} | {f["_deck"] for _n, f, _t in plan.adopt}:
        invoke("createDeck", deck=deck)
    added = 0
    for chunk in chunks(plan.add, 100):
        ids = invoke("addNotes", notes=[{k: v for k, v in a.items() if not k.startswith("_")} for a in chunk], timeout=300)
        added += sum(1 for i in ids if i)
        conn.executemany("INSERT OR IGNORE INTO identity (key, guid, kind) VALUES (?, ?, 'v4')",
                         [(a["fields"]["Key"], str(i)) for a, i in zip(chunk, ids) if i])
        conn.commit()
    _multi([{"action": "updateNoteFields", "params": {"note": {"id": n.note_id, "fields": u["fields"]}}} for n, u in plan.update])
    for n, u in plan.update:
        if set(n.tags) != set(u["tags"]):
            if n.tags:
                invoke("removeTags", notes=[n.note_id], tags=" ".join(n.tags))
            invoke("addTags", notes=[n.note_id], tags=" ".join(u["tags"]))
    for cards, deck in plan.move:
        invoke("changeDeck", cards=cards, deck=deck)
    # adoption: update studied legacy notes in place, move them, tag them
    _multi([{"action": "updateNoteFields", "params": {"note": {"id": ln.note_id, "fields": _legacy_fields(f, t)}}}
            for ln, f, t in plan.adopt])
    for ln, f, _t in plan.adopt:
        if ln.decks != {f["_deck"]}:
            invoke("changeDeck", cards=ln.cards, deck=f["_deck"])
        invoke("addTags", notes=[ln.note_id], tags=" ".join(f["_tags"] + ["adopted"]))
    retired = 0
    for chunk in chunks([n.note_id for n in plan.retire_v4 + plan.retire_legacy], 500):
        invoke("deleteNotes", notes=chunk)
        retired += len(chunk)
    reordered = reorder(conn)
    print(f"  done: added {added}, updated {len(plan.update)}, adopted {len(plan.adopt)}, "
          f"retired {retired}, reordered {reordered} new cards, uploaded {uploaded} media files")
    sync_knowledge(conn)
    return plan


def reorder(conn: sqlite3.Connection) -> int:
    """New v4 cards get due positions from the study plan (sort_order)."""
    pos = {r["key"]: r["sort_order"] for r in conn.execute("SELECT key, sort_order FROM v4_notes")}
    new_cards = set(invoke("findCards", query=f"{V4_QUERY} is:new"))
    notes = _notes(V4_QUERY, "Key")
    actions = []
    for n in notes:
        base = pos.get(n.key)
        if base is None:
            continue
        for i, cid in enumerate(sorted(n.cards)):
            if cid in new_cards:
                actions.append({"action": "setSpecificValueOfCard",
                                "params": {"card": cid, "keys": ["due"], "newValues": [base * 2 + i]}})
    _multi(actions)
    return len(actions)


def sync_knowledge(conn: sqlite3.Connection) -> dict:
    """Read review state back: unknown / learning / known per lexeme + card type."""
    legacy_q = _legacy_query(conn)
    scope = "(" + V4_QUERY + (" OR " + legacy_q if legacy_q else "") + ")"
    known = set(invoke("findCards", query=f"{scope} prop:ivl>=21"))
    learning = set(invoke("findCards", query=f"{scope} -is:new"))
    rows = []
    for n in _notes(V4_QUERY, "Key") + (_notes(legacy_q, "SortKey") if legacy_q else []):
        key = n.key
        if "|" in key:
            tgt = legacy_target(conn, key)
            if not tgt:
                continue
            key = tgt[0]
        parts = key.split(":")
        if len(parts) < 2:
            continue
        card_type, lexeme = parts[0], parts[1]
        state = "known" if any(c in known for c in n.cards) else "learning" if any(c in learning for c in n.cards) else "unknown"
        rows.append((lexeme, card_type, state))
    best: dict[tuple[str, str], str] = {}
    rank = {"unknown": 0, "learning": 1, "known": 2}
    for lexeme, ct, st in rows:
        if rank[st] > rank.get(best.get((lexeme, ct), "unknown"), 0) or (lexeme, ct) not in best:
            best[(lexeme, ct)] = st
    conn.execute("DELETE FROM knowledge")
    conn.executemany("INSERT INTO knowledge (lexeme_id, card_type, state) VALUES (?, ?, ?)",
                     [(l, c, s) for (l, c), s in best.items()])
    conn.commit()
    counts = defaultdict(int)
    for s in best.values():
        counts[s] += 1
    print(f"  knowledge: {dict(counts)}")
    return dict(counts)


__all__ = ["run", "build_plan", "reorder", "sync_knowledge", "legacy_target", "ensure_model"]
