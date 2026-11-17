"""Anki as a reconciled projection: ``plan`` (dry run) and ``apply``.

Compares the desired notes (``v4_notes``) with what Anki holds and makes
targeted changes through AnkiConnect — no ``.apkg`` import, so the
pipeline owns its notetype and never gets surprise notetype copies.

Rules (safety first):

* Only notes on the pipeline's notetype, in the pipeline's decks
  (``Italian::*``), are ever touched.
* Study history never overrides the desired content, enabled directions, or
  retirement decisions.
* Obsolete notes are retired once their replacement is confirmed in Anki,
  or when explicitly removed/disabled/ignored, whether studied or not.
  Unmapped notes stay until there is a replacement or a retirement reason.
* Anki is read once per sync, and every note is re-read immediately before
  it is deleted.
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

from .anki import chunks, invoke, quote
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
    retire: list[AnkiNote] = field(default_factory=list)
    keep_unmapped: int = 0
    waiting: int = 0
    media: set[str] = field(default_factory=set)
    present: set[str] = field(default_factory=set)  # v4 keys already in Anki

    retire_dependencies: dict[int, tuple[str, str] | None] = field(default_factory=dict)
    retirement_reasons: dict[int, str] = field(default_factory=dict)
    removed_ids: set[int] = field(default_factory=set)
    conflicts: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    suspend: list[int] = field(default_factory=list)
    profile: str = ""
    v4: dict[str, AnkiNote] = field(default_factory=dict)  # owned notes as read at planning
    # results of apply
    added: int = 0
    retired: int = 0
    uploaded: int = 0
    suspended: int = 0
    resumed: int = 0

    def result(self) -> str:
        """``+231 new · 6 updated · 357 old cards replaced`` (what apply changed)."""
        parts = [
            f"+{self.added:,} new notes" if self.added else "",
            f"{len(self.update):,} note updates" if self.update else "",
            f"{self.retired:,} obsolete notes removed" if self.retired else "",
            f"{sum(len(c) for c, _ in self.move):,} cards moved" if self.move else "",
            f"{len(self.rejected):,} rejected by Anki" if self.rejected else "",
            f"{self.suspended:,} newly suspended cards (history kept)" if self.suspended else "",
            f"{self.resumed:,} dormant cards activated" if self.resumed else "",
        ]
        return " · ".join(p for p in parts if p) or "up to date"

    def summary(self) -> str:
        return (
            f"model: {self.model} · add {len(self.add)} · update {len(self.update)} · "
            f"move {sum(len(c) for c, _ in self.move)} cards · "
            f"retire {len(self.retire)} (including studied) · "
            f"keep {self.keep_unmapped} without a replacement/removal reason · "
            f"{self.waiting} notes still waiting (AI/image) · upload {len(self.media)} media files"
        )


# ── Anki state ─────────────────────────────────────────────────────────────


def _notes(query: str, key_field: str) -> list[AnkiNote]:
    return _load(invoke("findNotes", query=query), key_field)


_CARD_INFO = ("ord", "type", "queue", "due", "interval", "lapses", "reps")


def _suspend_active(plan):
    """Suspend with a fresh state and deck check; report only actual changes."""
    active = []
    for group in chunks(sorted(set(plan.suspend)), 500):
        decks = invoke("getDecks", cards=group)
        owned = {c for deck, cs in decks.items() if deck_owned(deck) for c in cs}
        active.extend(
            c["cardId"]
            for c in invoke("cardsInfo", cards=group)
            if c and c["cardId"] in owned and c.get("queue") is not None and c["queue"] != -1
        )
    if active:
        if not invoke("suspend", cards=active):
            raise RuntimeError("Anki did not confirm card suspension")
        plan.suspended = len(active)
        changed = set(active)
        for note in plan.v4.values():
            for cid in changed.intersection(note.cards_info):
                note.cards_info[cid]["queue"] = -1


def _sync_dormancy(conn, plan, note_of):
    """Suspend Dormant cards; reactivate only suspensions made by this feature.

    Intent is checkpointed before the Anki call so interruption is resumable.
    Existing manual/leech suspensions are not claimed by the pipeline.
    """
    modes = {
        r["key"]: "fc::dormant" in r["tags"].split()
        for r in conn.execute("SELECT key,tags FROM v4_notes WHERE ready=1 AND card_type='form'")
    }
    wanted = {nid: modes[key] for key, nid in note_of.items() if key in modes and nid not in plan.removed_ids}
    if not wanted:
        return
    checkpoint = f"dormant_suspensions:{plan.profile}"
    row = conn.execute("SELECT value FROM metadata WHERE key=?", (checkpoint,)).fetchone()
    managed = {
        int(cid): nid
        for cid, nid in json.loads(row[0] if row else "{}").items()
        if nid not in plan.removed_ids
    }
    notes = {}
    for group in chunks(sorted(wanted), 500):
        notes.update({n["noteId"]: n for n in invoke("notesInfo", notes=group) if n})
    card_modes, protected = {}, set()
    for nid, note in notes.items():
        for cid in note.get("cards", []):
            card_modes[cid] = (nid, wanted[nid])
            if "leech" in note.get("tags", []):
                protected.add(cid)
    before = {}
    for group in chunks(sorted(card_modes), 500):
        before.update({c["cardId"]: c for c in invoke("cardsInfo", cards=group) if c})
    suspend, resume = [], []
    for cid, (nid, dormant) in card_modes.items():
        state = before.get(cid, {})
        if state.get("queue") is None:
            continue
        if dormant and state["queue"] != -1:
            managed[cid] = nid
            suspend.append(cid)
        elif not dormant and managed.get(cid) == nid and cid not in protected and cid not in plan.suspend:
            if state["queue"] == -1:
                resume.append(cid)
            else:
                managed.pop(cid)
    conn.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", (checkpoint, json.dumps(managed)))
    conn.commit()
    for action, ids in (("suspend", suspend), ("unsuspend", resume)):
        for group in chunks(ids, 500):
            if not invoke(action, cards=group):
                raise RuntimeError(f"Anki did not confirm dormant-card {action}")
    changed = {}
    for group in chunks(suspend + resume, 500):
        changed.update({c["cardId"]: c for c in invoke("cardsInfo", cards=group) if c})
    if any(changed.get(cid, {}).get("queue") != -1 for cid in suspend) or any(
        changed.get(cid, {}).get("queue", -1) == -1 for cid in resume
    ):
        raise RuntimeError("Anki dormant-card state did not match the requested state")
    for cid in resume:
        managed.pop(cid, None)
    conn.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", (checkpoint, json.dumps(managed)))
    conn.commit()
    plan.suspended += len(suspend)
    plan.resumed += len(resume)
    for note in plan.v4.values():
        for cid in set(changed).intersection(note.cards_info):
            note.cards_info[cid].update(changed[cid])


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


def build_plan(conn: sqlite3.Connection, *, anki_media: set[str]) -> Plan:
    """What one sync changes in Anki: notes to add, update and move, cards to
    suspend, and notes to retire (each with its reason)."""
    from .quality import reasons, require_current_notes

    require_current_notes(conn)
    plan = Plan()
    drills = _Drills(conn)
    desired = {r["key"]: dict(r) for r in conn.execute("SELECT * FROM v4_notes")}
    for row in desired.values():
        if row["ready"] and reasons(conn, row["key"], row["lexeme_id"], json.loads(row["fields_json"])):
            row["ready"] = False
    plan.waiting = sum(not r["ready"] for r in desired.values())
    plan.v4 = v4 = {n.key: n for n in owned_notes(conn, conflicts=plan.conflicts)}
    plan.present = set(v4)
    for key, note in v4.items():
        if drills.held(key) and not drills.removed(key):
            plan.suspend.extend(c for c in note.cards if note.cards_info.get(c, {}).get("queue") != -1)
    for key, row in desired.items():
        if row["ready"] and not drills.removed(key):
            _plan_note(plan, row, v4.get(key), anki_media)
    _plan_retirements(conn, plan, desired, drills)
    return plan


class _Drills:
    """Drills the meaning rules leave out (no human review: they are removed)."""

    def __init__(self, conn):
        from .planning import form_publication

        self.conn = conn
        self.publication = form_publication()
        self.holds: dict[str, str | None] = {}

    def held(self, key) -> str | None:
        if key not in self.holds:
            self.holds[key] = semantics.note_hold(self.conn, key)
        return self.holds[key]

    def removed(self, key) -> str | None:
        from .planning import form_output_reason

        return form_output_reason(key, self.publication) or (
            self.held(key) if key.startswith(("form:", "nphrase:")) else None
        )


def _plan_note(plan: Plan, row: dict, existing: AnkiNote | None, anki_media: set[str]) -> None:
    """What one ready note needs in Anki: added, or updated, moved and suspended."""
    fields = json.loads(row["fields_json"])
    tags = row["tags"].split() + [OWNER_TAG]
    plan.media |= _media_names(fields) - anki_media
    if existing is None:
        if fields["Recognition"] or fields["Production"]:
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
        return
    # AnkiConnect cannot delete one template's card without deleting
    # its whole note. Disable/suspend that direction, regardless of history.
    for cid, template in existing.templates.items():
        if not fields[template] and existing.cards_info.get(cid, {}).get("queue") != -1:
            plan.suspend.append(cid)
    wanted_tags = tags_for(existing.tags, tags)
    if any(existing.fields.get(f, "") != fields.get(f, "") for f in FIELDS) or set(existing.tags) != set(
        wanted_tags
    ):
        plan.update.append((existing, {"fields": fields, "tags": wanted_tags}))
    if existing.decks != {row["deck"]}:
        plan.move.append((existing.cards, row["deck"]))


def _plan_retirements(conn, plan: Plan, desired: dict, drills: _Drills) -> None:
    """Notes in Anki that should leave it, and why; the rest stay."""
    stale = _StaleReasons(conn)
    for key, n in plan.v4.items():
        reason = drills.removed(key) or (stale.reason(key) if key not in desired else None)
        dependency = None
        if not reason and key not in desired:
            # An older key for a card that now has its stable key (the main
            # meaning is always :0, verb forms/noun phrases have no :sense-N):
            # removed once that card is confirmed in Anki.
            current = _current_key(key)
            if current in desired and desired[current]["ready"]:
                fields = json.loads(desired[current]["fields_json"])
                template = "Recognition" if fields.get("Recognition") else "Production"
                if fields.get(template):
                    reason, dependency = f"Replaced by {current}", (current, template)
        if reason:
            plan.retire.append(n)
            plan.retire_dependencies[n.note_id] = dependency
            plan.retirement_reasons[n.note_id] = reason
        elif key not in desired:
            plan.keep_unmapped += 1


class _StaleReasons:
    """Why a note the pipeline no longer wants should leave Anki, if it should."""

    def __init__(self, conn):
        self.retirements = dict(conn.execute("SELECT key,reason FROM retirements").fetchall())
        self.ignored = dict(
            conn.execute("SELECT id, ignored FROM lexemes WHERE ignored IS NOT NULL").fetchall()
        )
        # A word has a copy of its card in every list it is in; a copy whose list the
        # word has left goes (the plain key belongs to ``lexemes.key_list``).
        self.memberships = defaultdict(set)
        for r in conn.execute("SELECT lexeme_id, list_id FROM list_items"):
            self.memberships[r[0]].add(r[1])
        self.key_lists = dict(
            conn.execute("SELECT id, key_list FROM lexemes WHERE key_list IS NOT NULL").fetchall()
        )
        self.numbers = dict(
            conn.execute("SELECT id, card_number FROM lexemes WHERE card_number IN ('singular','plural')")
        )

    def reason(self, key) -> str | None:
        parts = key.split(":")
        return (
            self.retirements.get(key)
            or self._ignored(parts)
            or self._left_list(parts)
            or ("No longer a cognate card" if parts[0] == "cognate" else None)  # one card per word now
            or self._unused_number(parts)
        )

    def _ignored(self, parts):
        if (
            parts[0] in ("vocab", "phrase", "form", "nphrase", "cognate")
            and len(parts) > 1
            and parts[1] in self.ignored
        ):
            return f"Claude: not a useful card — {self.ignored[parts[1]]}"
        return None

    def _left_list(self, parts):
        if parts[0] not in ("vocab", "phrase") or len(parts) != 3 or parts[1] not in self.memberships:
            return None
        listed = parts[2].partition("@")[2] or self.key_lists.get(parts[1])
        return f"No longer in list {listed}" if listed and listed not in self.memberships[parts[1]] else None

    def _unused_number(self, parts):
        # A noun-phrase drill in a number the word isn't used in (un occhiale, i latti).
        if (
            parts[0] == "nphrase"
            and len(parts) >= 4
            and parts[1] in self.numbers
            and parts[3] != self.numbers[parts[1]]
        ):
            return f"{parts[3].capitalize()} not used for this noun"
        return None


def _current_key(key: str) -> str | None:
    """The stable key an older v4 key's card now has, or None.

    ``vocab:<id>:2`` → ``vocab:<id>:0`` (the main meaning is always :0);
    ``form:<id>:presente:io:sense-1`` → ``form:<id>:presente:io``.
    """
    if ":sense-" in key:
        return key.split(":sense-")[0]
    if "@" in key:  # a list's copy of a word (``vocab:<id>:0@<list>``) has no older key
        return None
    parts = key.split(":")
    if parts[0] in ("vocab", "phrase") and len(parts) == 3 and parts[2] != "0":
        return f"{parts[0]}:{parts[1]}:0"
    return None


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


#: Deck options preset for the pipeline's decks (a copy of what they used before).
PRESET = "Italian flashcards"
_spaced: set[str] = set()  # profiles whose deck options were checked by this process


def space_siblings(profile: str) -> int:
    """Never show a word's Italian → English and English → Italian cards on the
    same day: Anki's "bury siblings" (new, review and interday learning) on the
    deck options of the ``Italian`` decks.

    A preset shared with any other deck (e.g. Default, used by ZCam's Decks - Old)
    is never changed: those Italian decks move to their own copy of it instead.
    Other settings (new cards per day …) are left as they are. Returns how many
    decks or presets changed.
    """
    if profile in _spaced:
        return 0
    configs = {d: invoke("getDeckConfig", deck=d) for d in invoke("deckNames")}
    configs = {d: c for d, c in configs.items() if isinstance(c, dict) and "id" in c}
    ours = {d for d in configs if d == "Italian" or d.startswith("Italian::")}
    shared = {c["id"] for d, c in configs.items() if d not in ours}
    changed = 0
    move = sorted(d for d in ours if configs[d]["id"] in shared)
    if move:
        preset = next((c["id"] for c in configs.values() if c.get("name") == PRESET), None)
        if preset is None or preset in shared:
            preset = invoke("cloneDeckConfigId", name=PRESET, cloneFrom=str(configs[move[0]]["id"]))
            if not preset:
                raise RuntimeError("Anki did not create the Italian flashcards deck options preset")
        if not invoke("setDeckConfigId", decks=move, configId=preset):
            raise RuntimeError("Anki did not assign the Italian flashcards deck options")
        changed += len(move)
    presets = {c["id"]: c for c in (invoke("getDeckConfig", deck=d) for d in sorted(ours))}
    for config in presets.values():
        if config["id"] in shared:
            continue
        if config["new"].get("bury") and config["rev"].get("bury") and config.get("buryInterdayLearning"):
            continue
        config["new"]["bury"] = config["rev"]["bury"] = True
        config["buryInterdayLearning"] = True
        if not invoke("saveDeckConfig", config=config):
            raise RuntimeError("Anki did not save the Italian flashcards deck options")
        changed += 1
    _spaced.add(profile)
    return changed


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
    expected = {
        r[0] for r in conn.execute("SELECT key FROM identity WHERE kind = 'v4' AND note_id IS NOT NULL")
    }
    still_wanted = {r[0] for r in conn.execute("SELECT key FROM v4_notes")}
    missing = (expected & still_wanted) - v4_keys_in_anki
    if expected and len(missing) > max(20, settings.anki.drift_guard_ratio * len(expected)):
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
    """Plan, then (unless ``dry_run``) apply: add, update, retire, read knowledge."""
    if report.VERBOSE:
        print_banner("anki — plan" if dry_run else "anki — apply")
    profile = invoke("getActiveProfile")
    stored = conn.execute("SELECT value FROM metadata WHERE key='anki_profile'").fetchone()
    expected = settings.anki.profile or (stored[0] if stored else "")
    plan = build_plan(conn, anki_media=set(invoke("getMediaFilesNames", pattern="*")))
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
        "retire": [(n.note_id, plan.retire_dependencies[n.note_id]) for n in plan.retire],
        "retirement_reasons": plan.retirement_reasons,
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
        for deck in {a["deckName"] for a in plan.add} | {d for _c, d in plan.move}:
            invoke("createDeck", deck=deck)
        if space_siblings(profile):
            report.line("Anki: a word's two directions are now never shown on the same day")
        final = {key: n.fields for key, n in plan.v4.items()}  # each v4 note's fields after this sync
        note_of = {key: n.note_id for key, n in plan.v4.items()}
        verify = {}
        # In study-plan order: Anki numbers new cards in the order they are added.
        # AnkiConnect (since October 2026) can't move a card afterwards.
        for chunk in chunks(sorted(plan.add, key=lambda a: a.get("_sort", 0)), 100):
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
                    plan.added += 1
                else:
                    plan.rejected.append(key)
            conn.commit()
        if plan.update:
            report.detail(f"  … updating {len(plan.update)} notes")
        _verify_notes(verify)  # additions must exist before anything is retired
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
        _verify_notes(verify)
        _suspend_active(plan)
        plan.retired = _retire(conn, plan, final, note_of, allow_retire)
        _sync_dormancy(conn, plan, note_of)
        _store_knowledge(
            conn,
            [n for n in plan.v4.values() if n.note_id not in plan.removed_ids],
        )
        conn.execute(
            "UPDATE sync_runs SET status=?,error=? WHERE id=?",
            (
                "partial" if plan.rejected else "done",
                f"Anki rejected: {plan.rejected[:20]}" if plan.rejected else None,
                run_id,
            ),
        )
        # Each plan is ~0.5 MB; the last 20 are plenty to inspect a recent sync.
        conn.execute(
            "DELETE FROM sync_runs WHERE id NOT IN (SELECT id FROM sync_runs ORDER BY created_at DESC LIMIT 20)"
        )
        conn.commit()
        report.detail(f"  done: added {plan.added}, retired {plan.retired}, uploaded {plan.uploaded}")
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
    """Delete obsolete notes, including studied ones, after verifying the plan.

    A replacement is the current note's matching card (read back from Anki
    here). Every note is re-read immediately before deletion: same identity
    and owned deck. No media is deleted.
    """
    candidates = plan.retire
    if not candidates or not allow:
        if candidates:
            report.line(f"kept {len(candidates):,} obsolete notes (retirement switched off)")
        return 0
    needed = {}
    verified_dependencies = {}
    for dep in plan.retire_dependencies.values():
        if not dep:
            continue
        if final.get(dep[0], {}).get(dep[1]) and note_of.get(dep[0]):
            nid = note_of[dep[0]]
            # Check content as well as identity/direction, including existing homes.
            needed[nid] = final[dep[0]]
            verified_dependencies[dep] = nid
    _verify_notes(needed)
    eligible = [
        n
        for n in candidates
        if (dep := plan.retire_dependencies[n.note_id]) is None or dep in verified_dependencies
    ]
    if eligible:
        report.detail(f"  … retiring up to {len(eligible)} obsolete notes (including studied)")
    retired, gone = 0, []
    for chunk in chunks(eligible, 200):
        fresh = {n["noteId"]: n for n in invoke("notesInfo", notes=[n.note_id for n in chunk]) if n}
        cards = [c for f in fresh.values() for c in f.get("cards", [])]
        deck_of = {c: d for d, cs in (invoke("getDecks", cards=cards) if cards else {}).items() for c in cs}
        doomed = []
        for n in chunk:
            current = fresh.get(n.note_id)
            if not current:
                gone.append(n.note_id)
                continue
            if current.get("fields", {}).get("Key", {}).get("value") != n.key:
                raise RuntimeError("Retirement identity changed during sync")
            cs = current.get("cards", [])
            if not all(deck_owned(deck_of.get(c, "")) for c in cs):
                continue  # moved by you out of the pipeline's decks: never touched
            doomed.append(n.note_id)
        if doomed:
            invoke("deleteNotes", notes=doomed)
            retired += len(doomed)
            gone += doomed
    if gone:
        plan.removed_ids.update(gone)
        # Keep historical identities and all generated content, but stop counting
        # deleted notes as currently published (including when re-enabled later).
        conn.executemany("UPDATE identity SET note_id=NULL WHERE note_id=?", [(i,) for i in gone])
        removed_cards = [cid for n in candidates if n.note_id in plan.removed_ids for cid in n.cards]
        conn.executemany("DELETE FROM card_knowledge WHERE card_id=?", [(i,) for i in removed_cards])
        conn.commit()
    return retired


def _store_knowledge(conn, notes: list[AnkiNote]) -> dict:
    """What you know, per card and direction, from the cards you've studied.

    Unstudied cards carry no knowledge, so only cards with history are kept.
    Suspended cards are never "known".
    """
    from .lists import load_plan

    known_at = load_plan().known_interval_days
    records = []
    for n in notes:
        key = n.key
        for cid in n.studied:
            c = n.cards_info[cid]
            direction = "Recognition" if c.get("ord") == 0 else "Production"
            suspended = c.get("queue") == -1
            interval = c.get("interval") or 0
            state = (
                "known"
                if not suspended and c.get("type") == 2 and interval >= known_at
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


__all__ = ["run", "build_plan", "ensure_model"]
