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
* An unstudied legacy note is retired once its replacement is confirmed in
  Anki: the v4 note's matching card, or a studied legacy note adopted for
  that same card (an unstudied duplicate). Notes without a replacement stay.
* Anki is read once per sync. The ~60k legacy notes are indexed weekly
  (``legacy_notes``); each sync only reads the studied ones, and every note
  is re-read immediately before it is deleted.
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
from uuid import uuid4

from . import report, semantics
from .db import transaction
from .ownership import OWNER_TAG, tags_for, deck_owned

from .anki import chunks, invoke, pipeline_models, quote
from .notes import CSS, FIELDS, MODEL_NAME, TEMPLATES
from .assets import locate
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
    reviews: int = 0
    templates: dict[int, str] = field(default_factory=dict)  # card id → template name
    #: card id → ord / type / queue / due / interval / lapses / reps
    cards_info: dict[int, dict] = field(default_factory=dict)

    @property
    def studied(self) -> set[int]:
        """Cards with study history: not new, or reviewed at least once."""
        return {c for c, i in self.cards_info.items() if i.get("type", 0) != 0 or i.get("reps", 0) > 0}


@dataclass
class Plan:
    model: str = "ok"
    add: list[dict] = field(default_factory=list)
    update: list[tuple[AnkiNote, dict]] = field(default_factory=list)
    move: list[tuple[list[int], str]] = field(default_factory=list)
    adopt: list[tuple[AnkiNote, dict, str]] = field(
        default_factory=list
    )  # legacy note, desired row, template
    retire_v4: list[AnkiNote] = field(default_factory=list)
    keep_v4_studied: list[AnkiNote] = field(default_factory=list)
    retire_legacy: list[AnkiNote] = field(default_factory=list)
    keep_legacy_studied: int = 0
    waiting: int = 0
    media: set[str] = field(default_factory=set)
    present: set[str] = field(default_factory=set)  # v4 keys already in Anki

    retire_dependencies: dict[int, tuple[str, str] | None] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    suspend: list[int] = field(default_factory=list)
    owned_count: int = 0
    profile: str = ""
    adopted: set[int] = field(default_factory=set)  # studied legacy notes that are a v4 note's home
    served: set[tuple[str, str]] = field(default_factory=set)  # (v4 key, template) an adopted note covers
    replacements: dict[tuple[str, str], tuple[int, dict, str]] = field(default_factory=dict)
    v4: dict[str, AnkiNote] = field(default_factory=dict)  # owned v4 notes as read at planning
    studied_legacy: list[AnkiNote] = field(default_factory=list)
    # results of apply
    added: int = 0
    retired: int = 0
    reordered: int = 0
    uploaded: int = 0

    def result(self) -> str:
        """``+231 new · 6 updated · 357 old cards replaced`` (what apply changed)."""
        parts = [
            f"+{self.added:,} new" if self.added else "",
            f"{len(self.update):,} updated" if self.update else "",
            f"{len(self.adopt):,} studied old cards refreshed" if self.adopt else "",
            f"{self.retired:,} unstudied old cards removed" if self.retired else "",
            f"{len(self.rejected):,} rejected by Anki" if self.rejected else "",
            f"{len(set(self.suspend)):,} unsuitable/disabled cards suspended (history kept)"
            if self.suspend
            else "",
        ]
        return " · ".join(p for p in parts if p) or "up to date"

    def summary(self) -> str:
        return (
            f"model: {self.model} · add {len(self.add)} · update {len(self.update)} · "
            f"move {sum(len(c) for c, _ in self.move)} cards · adopt {len(self.adopt)} studied legacy notes "
            f"({len(self.adopted)} adopted in all) · "
            f"retire {len(self.retire_v4)} v4 + {len(self.retire_legacy)} legacy (unstudied) · "
            f"keep {len(self.keep_v4_studied) + self.keep_legacy_studied} studied without a v4 home · "
            f"{self.waiting} notes still waiting (AI/image) · upload {len(self.media)} media files"
        )


# ── Anki state ─────────────────────────────────────────────────────────────


def _notes(query: str, key_field: str) -> list[AnkiNote]:
    return _load(invoke("findNotes", query=query), key_field)


_CARD_INFO = ("ord", "type", "queue", "due", "interval", "lapses", "reps")


def _load(note_ids: list[int], key_field: str) -> list[AnkiNote]:
    """Notes with their decks and per-card state, in bulk calls."""
    out: list[AnkiNote] = []
    for chunk in chunks(list(note_ids), 500):
        for n in invoke("notesInfo", notes=chunk):
            if not n:
                continue
            fields = {k: v["value"] for k, v in n.get("fields", {}).items()}
            out.append(
                AnkiNote(
                    n["noteId"],
                    fields.get(key_field, "").strip(),
                    fields,
                    n.get("tags", []),
                    n.get("cards", []),
                )
            )
    cids = [c for n in out for c in n.cards]
    deck_of: dict[int, str] = {}
    for chunk in chunks(cids, 5000):
        for deck, cs in invoke("getDecks", cards=chunk).items():
            for c in cs:
                deck_of[c] = deck
    info = {}
    for chunk in chunks(cids, 500):
        info.update({c["cardId"]: c for c in invoke("cardsInfo", cards=chunk) if c})
    for n in out:
        n.decks = {deck_of.get(c, "") for c in n.cards}
        n.cards_info = {c: {k: info[c].get(k) for k in _CARD_INFO} for c in n.cards if c in info}
        n.templates = {
            c: ("Recognition" if i.get("ord", 0) == 0 else "Production") for c, i in n.cards_info.items()
        }
        n.reviews = sum(i.get("reps") or 0 for i in n.cards_info.values())
    return out


def _studied_in(notes) -> set[int]:
    return {c for n in notes for c in n.studied}


def _studied(card_ids) -> set[int]:
    """Those of ``card_ids`` with any study history: not new, or with review-log entries."""
    studied: set[int] = set()
    for chunk in chunks(list(card_ids), 500):
        studied |= set(invoke("findCards", query="cid:" + ",".join(map(str, chunk)) + " -is:new"))
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
    return (
        "("
        + " OR ".join(quote(f"note:{m}") for m in models)
        + ") ("
        + " OR ".join(quote(f"deck:{d}") for d in decks)
        + ")"
    )


# ── Legacy mapping ─────────────────────────────────────────────────────────


def legacy_targets(conn):
    """A fast ``sort_key → (v4 key, template) | None`` for many legacy notes at once."""
    lexeme_of = {
        r[0]: (r[1], r[2])
        for r in conn.execute(
            "SELECT el.entry_id, l.id, l.pos FROM entry_lexeme el JOIN lexemes l ON l.id = el.lexeme_id"
        )
    }
    forms = {
        r[0]: (r[1], r[2], r[3])
        for r in conn.execute("SELECT card_key, entry_id, tense, person FROM verb_forms")
    }

    def target(sort_key: str) -> tuple[str, str] | None:
        nk, _, direction = sort_key.rpartition("|")
        if not nk or direction not in ("it_to_en", "en_to_it"):
            return None
        template = "Recognition" if direction == "it_to_en" else "Production"
        kind, _, rest = nk.partition(":")
        if kind in ("gloss", "verb_infinitive", "avere"):
            hit = lexeme_of.get(rest.split(":")[0])
            if not hit:
                return None
            return f"{'phrase' if hit[1] == 'phrase' else 'vocab'}:{hit[0]}:0", template
        if kind == "verb_form" and template == "Production" and nk in forms:
            entry_id, tense, person = forms[nk]
            hit = lexeme_of.get(entry_id)
            if hit:
                return f"form:{hit[0]}:{tense}:{person}", template
        return None

    return target


def legacy_target(conn, sort_key: str) -> tuple[str, str] | None:
    """(v4 key, template) a legacy note (SortKey ``natural_key|direction``) maps to."""
    return legacy_targets(conn)(sort_key)


def _legacy_fields(desired: dict, template: str) -> dict:
    """Map v4 fields onto the legacy notetype for an adopted note."""
    f = desired
    back = "".join(
        x
        for x in (
            f'<div class="details">{f["Details"]}</div>' if f.get("Details") else "",
            f'<div class="details">{f["Forms"]}</div>' if f.get("Forms") else "",
            f'<div class="details">{f["Note"]}</div>' if f.get("Note") else "",
            f'<div class="ipa">{f["IPA"]}</div>' if f.get("IPA") else "",
            f'<div class="example">{f["Example"]}</div>' if f.get("Example") else "",
            (
                f'<div class="fact"><span class="fact-title">{f["FactTitle"]}</span>{f["Fact"]}</div>'
                if f.get("Fact")
                else ""
            ),
        )
    )
    if template == "Recognition":
        return {
            "FrontText": f["Italian"],
            "BackHighlight": f["English"],
            "FrontAudio": f["Audio"],
            "Audio": "",
            "FrontLabels": f["Labels"],
            "BackText": back,
            "Image": f["Image"],
        }
    return {
        "FrontText": f["English"] + (f' <span class="hint">({f["Hint"]})</span>' if f.get("Hint") else ""),
        "BackHighlight": f["Italian"],
        "FrontAudio": "",
        "Audio": f["Audio"],
        "FrontLabels": f["Labels"],
        "BackText": back,
        "Image": f["Image"],
    }


# ── Planning ───────────────────────────────────────────────────────────────


def _media_names(fields: dict) -> set[str]:
    text = " ".join(fields.values())
    return set(_IMG.findall(text)) | set(_SND.findall(text))


def owned_notes(conn, *, conflicts=None):
    """Return only notes proven owned; unknown v4 notes block model changes."""
    desired = {r[0] for r in conn.execute("SELECT key FROM v4_notes")}
    identities = {
        r["key"]: r["note_id"] for r in conn.execute("SELECT key,note_id FROM identity WHERE kind='v4'")
    }
    result = []
    seen = set()
    if MODEL_NAME not in invoke("modelNames"):
        return result
    for n in _notes(V4_QUERY, "Key"):
        if n.key in seen:
            if conflicts is not None:
                conflicts.append(f"Duplicate v4 Key {n.key}")
            continue
        seen.add(n.key)
        valid = bool(n.key) and bool(n.decks) and all(deck_owned(d) for d in n.decks)
        valid = valid and (identities.get(n.key) == n.note_id or (OWNER_TAG in n.tags and n.key in desired))
        if valid:
            result.append(n)
        elif conflicts is not None:
            conflicts.append(f"Unowned v4 note {n.note_id} ({n.key}); model changes would affect it")
    return result


def owned_legacy(conn, *, studied_only: bool = False):
    q = _legacy_query(conn)
    if not q:
        return []
    keys = {r[0] + "|" + r[1] for r in conn.execute("SELECT natural_key,direction FROM cards")}
    decks = _legacy_decks(conn)
    return [
        n
        for n in _notes(q + (" -is:new" if studied_only else ""), "SortKey")
        if n.key in keys and n.decks and all(deck_owned(d, decks) for d in n.decks)
    ]


LEGACY_INDEX_DAYS = 7


def legacy_index(conn, profile: str, *, refresh: bool = False) -> dict[int, tuple[str, list[int]]]:
    """note id → (SortKey, cards) for every owned legacy note, rebuilt weekly.

    Reading all ~60k legacy notes takes half a minute, and they only change
    when this pipeline adopts or retires them, so syncs use this index to
    find retirement candidates. Deletion always re-reads the note first.
    """
    from datetime import datetime, timedelta

    row = conn.execute("SELECT value FROM metadata WHERE key='legacy_index'").fetchone()
    meta = json.loads(row[0]) if row else {}
    stale = (
        refresh
        or meta.get("profile") != profile
        or datetime.fromisoformat(meta.get("scanned_at", "2000-01-01"))
        < datetime.now() - timedelta(days=LEGACY_INDEX_DAYS)
    )
    if stale:
        report.line("indexing your old v3 notes in Anki (once a week, ~30s) …")
        notes = owned_legacy(conn)
        with transaction(conn):
            conn.execute("DELETE FROM legacy_notes")
            conn.executemany(
                "INSERT INTO legacy_notes VALUES(?,?,?)",
                [(n.note_id, n.key, json.dumps(n.cards)) for n in notes],
            )
            conn.execute(
                "INSERT OR REPLACE INTO metadata VALUES('legacy_index',?)",
                (
                    json.dumps(
                        {"profile": profile, "scanned_at": datetime.now().isoformat(timespec="seconds")}
                    ),
                ),
            )
    return {r[0]: (r[1], json.loads(r[2])) for r in conn.execute("SELECT * FROM legacy_notes")}


def build_plan(conn: sqlite3.Connection, *, anki_media: set[str], profile: str = "") -> Plan:
    from .quality import reasons, require_current_notes

    require_current_notes(conn)
    plan = Plan()
    desired = {r["key"]: dict(r) for r in conn.execute("SELECT * FROM v4_notes")}
    for row in desired.values():
        if row["ready"] and reasons(conn, row["key"], row["lexeme_id"], json.loads(row["fields_json"])):
            row["ready"] = False
    plan.waiting = sum(not r["ready"] for r in desired.values())
    v4 = {n.key: n for n in owned_notes(conn, conflicts=plan.conflicts)}
    plan.v4 = v4
    for key, note in v4.items():
        if semantics.note_hold(conn, key):
            plan.suspend.extend(note.cards)
    plan.present = set(v4)
    plan.studied_legacy = owned_legacy(conn, studied_only=True)
    seen = {n.note_id for n in plan.studied_legacy}
    legacy = plan.studied_legacy + [
        AnkiNote(nid, key, {}, [], cards)
        for nid, (key, cards) in legacy_index(conn, profile).items()
        if nid not in seen
    ]
    plan.owned_count = len(v4) + len(legacy)
    studied = _studied_in(list(v4.values()) + plan.studied_legacy)
    saved = {(r["key"], r["direction"]): r["note_id"] for r in conn.execute("SELECT * FROM adoptions")}
    candidates = defaultdict(dict)
    targets = {}
    target_of = legacy_targets(conn)
    for n in legacy:
        tgt = target_of(n.key)
        targets[n.note_id] = tgt
        if tgt and semantics.note_hold(conn, tgt[0]):
            plan.suspend.extend(n.cards)
            continue
        if not tgt or tgt[0] not in desired or not any(c in studied for c in n.cards):
            continue
        if not desired[tgt[0]]["ready"]:
            continue
        current_v4 = v4.get(tgt[0])
        # A studied v4 direction stays the canonical home; don't create an empty studied card.
        if current_v4 and any(
            c in studied and current_v4.templates.get(c) == tgt[1] for c in current_v4.cards
        ):
            continue
        current = candidates[tgt[0]].get(tgt[1])

        # Several studied legacy notes may map to one direction: keep the one
        # adopted before, else the most reviewed, else the oldest.
        def score(note, tgt=tgt):
            return (saved.get(tgt) == note.note_id, note.reviews, -note.note_id)

        if current is None or score(n) > score(current):
            candidates[tgt[0]][tgt[1]] = n
    for key, row in desired.items():
        if not row["ready"]:
            continue
        fields = json.loads(row["fields_json"])
        tags = row["tags"].split() + [OWNER_TAG]
        taken = candidates.get(key, {})
        for template, ln in taken.items():
            if not fields[template]:
                continue
            plan.adopted.add(ln.note_id)
            plan.served.add((key, template))
            want = _legacy_fields(fields, template)
            plan.replacements[(key, template)] = (ln.note_id, {**want, "SortKey": ln.key}, row["deck"])
            current = all(ln.fields.get(k, "") == v for k, v in want.items())
            if not (current and ln.decks == {row["deck"]} and {*tags, "fc::adopted"} <= set(ln.tags)):
                plan.adopt.append((ln, {**fields, "_deck": row["deck"], "_tags": tags}, template))
            fields[template] = ""
        plan.media |= _media_names(fields) - anki_media
        existing = v4.get(key)
        if existing:
            # Preserve studied cards even when a manifest disables a direction.
            for cid, template in existing.templates.items():
                if cid in studied and not fields[template]:
                    fields[template] = existing.fields.get(template, "1")
                elif not fields[template]:
                    plan.suspend.append(cid)
            wanted_tags = tags_for(existing.tags, tags)
            if any(existing.fields.get(f, "") != fields.get(f, "") for f in FIELDS) or set(
                existing.tags
            ) != set(wanted_tags):
                plan.update.append((existing, {"fields": fields, "tags": wanted_tags}))
            if existing.decks != {row["deck"]}:
                plan.move.append((existing.cards, row["deck"]))
        elif fields["Recognition"] or fields["Production"]:
            plan.add.append(
                {
                    "deckName": row["deck"],
                    "modelName": MODEL_NAME,
                    "fields": fields,
                    "tags": tags,
                    "options": {"allowDuplicate": True},
                    "_sort": row["sort_order"],
                }
            )
    reasons = {r[0]: r[1] for r in conn.execute("SELECT key,reason FROM retirements")}
    for key, n in v4.items():
        if key not in desired:
            if any(c in studied for c in n.cards):
                plan.keep_v4_studied.append(n)
            elif key in reasons:
                plan.retire_v4.append(n)
                plan.retire_dependencies[n.note_id] = None
    adopted_ids = plan.adopted
    possible = set(v4) | {a["fields"]["Key"] for a in plan.add}
    for n in legacy:
        if n.note_id in adopted_ids:
            continue
        if any(c in studied for c in n.cards):
            plan.keep_legacy_studied += 1
            continue
        tgt = targets[n.note_id]
        # Only once the same card exists in v4 (or is served by an adopted note).
        replaced = (
            tgt
            and (tgt[0] in possible or tgt in plan.replacements)
            and tgt[0] in desired
            and desired[tgt[0]]["ready"]
            and json.loads(desired[tgt[0]]["fields_json"]).get(tgt[1])
        )
        if replaced or n.key in reasons:
            plan.retire_legacy.append(n)
            plan.retire_dependencies[n.note_id] = tgt if replaced else None
    return plan


# ── Apply ──────────────────────────────────────────────────────────────────


def ensure_model() -> str:
    if MODEL_NAME not in invoke("modelNames"):
        invoke(
            "createModel",
            modelName=MODEL_NAME,
            inOrderFields=FIELDS,
            css=CSS,
            isCloze=False,
            cardTemplates=TEMPLATES,
        )
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


def _upload_media(names: set[str]) -> int:
    n = 0
    for name in sorted(names):
        p = locate(name)
        if not p:
            raise RuntimeError(f"Required media missing: {name}; run explicit compression/build first")
        result = invoke("storeMediaFile", filename=name, path=str(p.resolve()))
        if result != name:
            raise RuntimeError(f"Media upload did not confirm {name}")
        n += 1
    return n


def _multi(actions: list[dict]) -> None:
    for chunk in chunks(actions, 200):
        results = invoke("multi", actions=[{**a, "version": 6} for a in chunk], timeout=300)
        if not isinstance(results, list) or len(results) != len(chunk):
            raise RuntimeError("Incomplete Anki multi response")
        for action, result in zip(chunk, results, strict=True):
            if (
                not isinstance(result, dict)
                or "error" not in result
                or "result" not in result
                or result["error"] is not None
            ):
                raise RuntimeError(f"Anki {action['action']} failed: {result}")


def _drift_guard(conn, v4_keys_in_anki: set[str]) -> str | None:
    expected = {r[0] for r in conn.execute("SELECT key FROM identity WHERE kind = 'v4'")}
    still_wanted = {r[0] for r in conn.execute("SELECT key FROM v4_notes")}
    missing = (expected & still_wanted) - v4_keys_in_anki
    if expected and len(missing) > max(20, 0.05 * len(expected)):
        return (
            f"{len(missing)} of {len(expected)} v4 notes that were in Anki are gone — is a "
            f"different Anki profile open, or were notes deleted by hand? Refusing to sync."
        )
    return None


def _verify_notes(expected: dict) -> dict[int, dict[int, int]]:
    """Read back exact fields and required templates after acknowledged writes.

    Returns note id → {ord: card id} for the notes read.
    """
    cards_by_note: dict[int, dict[int, int]] = defaultdict(dict)
    for chunk in chunks(list(expected), 500):
        by_id = {n["noteId"]: n for n in invoke("notesInfo", notes=chunk) if n}
        cards = [c for nid in chunk if nid in by_id for c in by_id[nid].get("cards", [])]
        for part in chunks(cards, 500):
            for c in invoke("cardsInfo", cards=part):
                if c:
                    cards_by_note[c["note"]][c.get("ord")] = c["cardId"]
        for nid in chunk:
            n = by_id.get(nid)
            fields = expected[nid]
            if (
                not n
                or not n.get("cards")
                or any(n.get("fields", {}).get(k, {}).get("value") != v for k, v in fields.items())
            ):
                raise RuntimeError(f"Anki read-back failed for note {nid}; no retirement performed")
            if "Key" in fields:
                for ordinal, direction in enumerate(("Recognition", "Production")):
                    if fields.get(direction) and ordinal not in cards_by_note[nid]:
                        raise RuntimeError(f"Missing replacement {direction} card on note {nid}")
    return cards_by_note


def run(conn: sqlite3.Connection, *, dry_run: bool = False, allow_retire: bool = True) -> Plan:
    """Plan, then (unless ``dry_run``) apply: add, update, adopt, retire, reorder, read knowledge."""
    if report.VERBOSE:
        print_banner("anki — plan" if dry_run else "anki — apply")
    profile = invoke("getActiveProfile")
    stored = conn.execute("SELECT value FROM metadata WHERE key='anki_profile'").fetchone()
    expected = settings.anki.profile or (stored[0] if stored else "")
    plan = build_plan(conn, anki_media=set(invoke("getMediaFilesNames", pattern="*")), profile=profile)
    plan.profile = profile
    if not isinstance(profile, str) or not profile:
        plan.conflicts.append("Anki did not identify its active profile")
    if expected and expected != profile:
        plan.conflicts.append(f"Expected Anki profile {expected!r}, got {profile!r}")
    drift = _drift_guard(conn, plan.present)
    if drift:
        plan.conflicts.append(drift)
    report.detail("  " + plan.summary())
    if plan.conflicts:
        print("  BLOCKED: " + "; ".join(plan.conflicts), flush=True)
        if not dry_run:
            raise RuntimeError("Sync refused before any Anki writes: " + "; ".join(plan.conflicts))
    if dry_run:
        return plan
    run_id = uuid4().hex
    payload = {
        "add": [a["fields"]["Key"] for a in plan.add],
        "update": [n.key for n, _u in plan.update],
        "adopt": [(n.note_id, f["Key"], t) for n, f, t in plan.adopt],
        "retire": [
            (n.note_id, plan.retire_dependencies[n.note_id]) for n in plan.retire_v4 + plan.retire_legacy
        ],
    }
    conn.execute(
        "INSERT INTO sync_runs(id,profile,plan_json,status) VALUES(?,?,?,'running')",
        (run_id, profile, json.dumps(payload)),
    )
    conn.execute("INSERT OR REPLACE INTO metadata VALUES('anki_profile',?)", (profile,))
    conn.commit()
    try:
        plan.model = ensure_model()
        if plan.add or plan.media:
            report.detail(f"  … uploading {len(plan.media)} media files, adding {len(plan.add)} notes")
        plan.uploaded = _upload_media(plan.media)
        for deck in (
            {a["deckName"] for a in plan.add}
            | {d for _c, d in plan.move}
            | {f["_deck"] for _n, f, _t in plan.adopt}
        ):
            invoke("createDeck", deck=deck)
        final = {key: n.fields for key, n in plan.v4.items()}  # each v4 note's fields after this sync
        note_of = {key: n.note_id for key, n in plan.v4.items()}
        added_ids = []
        verify = {}
        for chunk in chunks(plan.add, 100):
            ids = invoke(
                "addNotes",
                notes=[{k: v for k, v in a.items() if not k.startswith("_")} for a in chunk],
                timeout=300,
            )
            if not isinstance(ids, list) or len(ids) != len(chunk):
                raise RuntimeError("Incomplete addNotes result; inspect/rerun, no retirement performed")
            for a, nid in zip(chunk, ids, strict=True):
                key = a["fields"]["Key"]
                if nid:
                    conn.execute(
                        "INSERT INTO identity(key,guid,kind,note_id,profile) VALUES(?,'','v4',?,?) ON CONFLICT(key) DO UPDATE SET note_id=excluded.note_id,profile=excluded.profile",
                        (key, nid, profile),
                    )
                    verify[nid] = a["fields"]
                    final[key], note_of[key] = a["fields"], nid
                    added_ids.append(nid)
                    plan.added += 1
                else:
                    plan.rejected.append(key)
            conn.commit()
        if plan.update or plan.adopt:
            report.detail(f"  … updating {len(plan.update)} notes, adopting {len(plan.adopt)}")
        _multi(
            [
                {
                    "action": "updateNoteFields",
                    "params": {"note": {"id": n.note_id, "fields": _legacy_fields(f, t)}},
                }
                for n, f, t in plan.adopt
            ]
        )
        adopt_actions = []
        for n, f, t in plan.adopt:
            verify[n.note_id] = {**_legacy_fields(f, t), "SortKey": n.key}
            if n.decks != {f["_deck"]}:
                adopt_actions.append(
                    {"action": "changeDeck", "params": {"cards": n.cards, "deck": f["_deck"]}}
                )
            adopt_actions.append(
                {
                    "action": "addTags",
                    "params": {"notes": [n.note_id], "tags": " ".join([*f["_tags"], "fc::adopted"])},
                }
            )
        _multi(adopt_actions)
        _verify_notes(verify)  # additions and adoptions must exist before switching homes
        _multi(
            [
                {"action": "updateNoteFields", "params": {"note": {"id": n.note_id, "fields": u["fields"]}}}
                for n, u in plan.update
            ]
        )
        for n, u in plan.update:
            verify[n.note_id] = u["fields"]
            final[n.key] = u["fields"]
        tag_actions = []
        for n, u in plan.update:
            remove = set(n.tags) - set(u["tags"])
            add = set(u["tags"]) - set(n.tags)
            if remove:
                tag_actions.append(
                    {
                        "action": "removeTags",
                        "params": {"notes": [n.note_id], "tags": " ".join(sorted(remove))},
                    }
                )
            if add:
                tag_actions.append(
                    {"action": "addTags", "params": {"notes": [n.note_id], "tags": " ".join(sorted(add))}}
                )
        _multi(tag_actions)
        _multi(
            [{"action": "changeDeck", "params": {"cards": cards, "deck": deck}} for cards, deck in plan.move]
        )
        written = _verify_notes(verify)
        for n, f, t in plan.adopt:
            conn.execute(
                "INSERT OR REPLACE INTO adoptions VALUES(?,?,?,?)", (f["Key"], t, n.note_id, profile)
            )
        conn.commit()
        if plan.suspend:
            invoke("suspend", cards=plan.suspend)
        plan.retired = _retire(conn, plan, final, note_of, allow_retire)
        plan.reordered = _reorder(conn, plan, {nid: written.get(nid, {}) for nid in added_ids}, note_of)
        _store_knowledge(conn, list(plan.v4.values()) + plan.studied_legacy)
        conn.execute(
            "UPDATE sync_runs SET status=?,error=? WHERE id=?",
            (
                "partial" if plan.rejected else "done",
                f"Anki rejected: {plan.rejected[:20]}" if plan.rejected else None,
                run_id,
            ),
        )
        conn.commit()
        report.detail(
            f"  done: added {plan.added}, retired {plan.retired}, reordered {plan.reordered}, uploaded {plan.uploaded}"
        )
        if plan.rejected:
            print(
                f"  Anki rejected {len(plan.rejected)} new note(s) (their predecessors stay): "
                f"{', '.join(plan.rejected[:5])}",
                flush=True,
            )
        return plan
    except BaseException as exc:
        conn.rollback()
        conn.execute("UPDATE sync_runs SET status='failed',error=? WHERE id=?", (str(exc)[:1000], run_id))
        conn.commit()
        raise


def _retire(conn, plan: Plan, final: dict, note_of: dict, allow: bool) -> int:
    """Delete unstudied predecessors whose replacement is confirmed in Anki.

    A replacement is the v4 note's matching card (read back from Anki here) or
    a studied legacy note adopted for that same card. Every note is re-read
    immediately before deletion: same identity, no study history, owned deck.
    """
    candidates = plan.retire_v4 + plan.retire_legacy
    if not candidates or not allow:
        if candidates:
            report.line(f"kept {len(candidates):,} unstudied old cards (retirement switched off)")
        return 0
    needed, decks = {}, {}
    verified_dependencies = {}
    for dep in plan.retire_dependencies.values():
        if not dep:
            continue
        if dep in plan.replacements:
            nid, fields, deck = plan.replacements[dep]
            needed[nid], decks[nid] = fields, deck
            verified_dependencies[dep] = nid
        elif final.get(dep[0], {}).get(dep[1]) and note_of.get(dep[0]):
            nid = note_of[dep[0]]
            # Check content as well as identity/direction, including existing homes.
            needed[nid] = final[dep[0]]
            verified_dependencies[dep] = nid
    card_maps = _verify_notes(needed)
    for nid, fields in needed.items():
        if "SortKey" in fields and 0 not in card_maps.get(nid, {}):
            raise RuntimeError(f"Missing adopted replacement card on note {nid}; no retirement performed")
    if decks:
        cards = [cid for nid in decks for cid in card_maps[nid].values()]
        deck_of = {}
        for part in chunks(cards, 500):
            for deck, ids in invoke("getDecks", cards=part).items():
                deck_of.update(dict.fromkeys(ids, deck))
        for nid, deck in decks.items():
            if any(deck_of.get(cid) != deck for cid in card_maps[nid].values()):
                raise RuntimeError(f"Adopted replacement {nid} moved; no retirement performed")
    eligible = [
        n
        for n in candidates
        if (dep := plan.retire_dependencies[n.note_id]) is None or dep in verified_dependencies
    ]
    if eligible:
        report.detail(f"  … retiring up to {len(eligible)} unstudied predecessors")
    legacy_decks = _legacy_decks(conn)
    retire_v4 = {n.note_id for n in plan.retire_v4}
    retired, gone = 0, []
    for chunk in chunks(eligible, 200):
        fresh = {n["noteId"]: n for n in invoke("notesInfo", notes=[n.note_id for n in chunk]) if n}
        cards = [c for f in fresh.values() for c in f.get("cards", [])]
        studied = _studied(cards)
        deck_of = {c: d for d, cs in (invoke("getDecks", cards=cards) if cards else {}).items() for c in cs}
        doomed = []
        for n in chunk:
            current = fresh.get(n.note_id)
            if not current:
                gone.append(n.note_id)
                continue
            field_name = "Key" if n.note_id in retire_v4 else "SortKey"
            if current.get("fields", {}).get(field_name, {}).get("value") != n.key:
                raise RuntimeError("Retirement identity changed during sync")
            cs = current.get("cards", [])
            if studied & set(cs) or not all(deck_owned(deck_of.get(c, ""), legacy_decks) for c in cs):
                continue
            doomed.append(n.note_id)
        if doomed:
            invoke("deleteNotes", notes=doomed)
            retired += len(doomed)
            gone += doomed
    if gone:
        conn.executemany("DELETE FROM legacy_notes WHERE note_id=?", [(i,) for i in gone])
        conn.commit()
    return retired


def _reorder(conn, plan: Plan, added: dict[int, dict[int, int]], note_of: dict) -> int:
    """Put new v4 cards in study-plan order; only cards whose position differs are moved."""
    pos = {r["key"]: r["sort_order"] for r in conn.execute("SELECT key, sort_order FROM v4_notes")}
    key_of = {nid: key for key, nid in note_of.items()}
    want: dict[int, int] = {}
    current: dict[int, int | None] = {}
    for key, n in plan.v4.items():
        if key in pos:
            for cid, info in n.cards_info.items():
                if info.get("type") == 0:
                    want[cid] = pos[key] * 2 + (info.get("ord") or 0)
                    current[cid] = info.get("due")
    for nid, cards in added.items():
        key = key_of.get(nid)
        if key in pos:
            for ord_, cid in cards.items():
                want[cid] = pos[key] * 2 + (ord_ or 0)
                current[cid] = None
    actions = [
        {"action": "setSpecificValueOfCard", "params": {"card": cid, "keys": ["due"], "newValues": [due]}}
        for cid, due in want.items()
        if current.get(cid) != due
    ]
    _multi(actions)
    return len(actions)


def _store_knowledge(conn, notes: list[AnkiNote]) -> dict:
    """What you know, per card and direction, from the cards you've studied.

    Unstudied cards carry no knowledge, so only cards with history are kept.
    Suspended cards are never "known".
    """
    records = []
    target_of = legacy_targets(conn)
    for n in notes:
        legacy = "|" in n.key
        tgt = target_of(n.key) if legacy else None
        if legacy and not tgt:
            continue
        key = tgt[0] if legacy else n.key
        for cid in n.studied:
            c = n.cards_info[cid]
            direction = tgt[1] if legacy else ("Recognition" if c.get("ord") == 0 else "Production")
            suspended = c.get("queue") == -1
            interval = c.get("interval") or 0
            state = (
                "known"
                if not suspended and c.get("type") == 2 and interval >= 21
                else "learning"
                if not suspended
                else "unknown"
            )
            records.append(
                (
                    key,
                    direction,
                    cid,
                    state,
                    interval,
                    c.get("lapses") or 0,
                    c.get("reps") or 0,
                    int(suspended),
                )
            )
    with transaction(conn):
        conn.execute("DELETE FROM card_knowledge")
        conn.executemany(
            "INSERT OR REPLACE INTO card_knowledge(key,direction,card_id,state,interval,lapses,reviews,suspended) VALUES(?,?,?,?,?,?,?,?)",
            records,
        )
        conn.execute("INSERT OR REPLACE INTO metadata VALUES('knowledge_synced_at',datetime('now'))")
    counts = defaultdict(int)
    for r in records:
        counts[r[3]] += 1
    return dict(counts)


__all__ = ["run", "build_plan", "legacy_index", "legacy_target", "ensure_model"]
