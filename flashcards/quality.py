"""One publication policy for generated notes, sync, audits, and exports."""

import html
import re
from .util import md5_hex
from .settings import settings

AUDITED = (
    "Italian",
    "English",
    "Hint",
    "Labels",
    "Details",
    "Forms",
    "IPA",
    "Example",
    "Note",
    "Fact",
    "Recognition",
    "Production",
)


def text(value):
    return html.unescape(re.sub(r"<[^>]+>", " ", value or "")).strip()


def note_hash(fields):
    return md5_hex("\x1f".join(text(fields.get(f, "")) for f in AUDITED) + "\x1f" + fields.get("Image", ""))


def reasons(conn, key, lexeme_id, fields):
    from .semantics import note_hold

    result = []
    if note_hold(conn, key):
        result.append("grammar review")
    if not text(fields.get("Italian")) or not text(fields.get("English")):
        result.append("missing content")
    if lexeme_id:
        lx = conn.execute("SELECT status,pos FROM lexemes WHERE id=?", (lexeme_id,)).fetchone()
        if not lx or lx[0] == "needs_review":
            result.append("needs review")
        elif lx[0] != "ready" and lx[1] not in {"num", "letter"}:
            result.append("awaiting verification")
    if key.startswith("form:") and lexeme_id:
        job = conn.execute(
            "SELECT status FROM ai_jobs WHERE kind='verb_prompts' AND subject=?", (lexeme_id,)
        ).fetchone()
        if job and job[0] != "done":
            result.append("awaiting verb prompts")
    if settings.audit.block_failures:
        a = conn.execute(
            "SELECT card_hash,verdict FROM audits WHERE natural_key=? AND direction='note'", (key,)
        ).fetchone()
        if a and a[0] == note_hash(fields) and a[1] == "fail":
            decision = conn.execute(
                "SELECT decision FROM review_decisions WHERE subject=? AND content_hash=?", (key, a[0])
            ).fetchone()
            if not decision or decision[0] != "approve":
                result.append("failed audit")
    return result


def require_current_notes(conn):
    """Refuse to publish a materialized view invalidated by upstream writes."""
    checkpoints = dict(conn.execute("SELECT key,value FROM metadata"))
    source = checkpoints.get("source_generation")
    if source and checkpoints.get("notes_generation") != source:
        raise ValueError(
            "Notes are stale; rebuild with `flashcards notes` before publishing or generating audio"
        )
