"""``subtlex`` mode — extract top-frequency verbs and nouns from a SUBTLEX-IT CSV.

This is what populates the big "Italian - Verbs *" and "Italian - Nouns" decks.
The SUBTLEX CSV uses ``;`` as separator and has columns
``wordform;freq_count;zipf;cd_count;dom_pos;dom_lemma;dom_lemma_freq;id``.

Config keys recognised on the source (all four deck fields are **required**;
``sources.validate()`` will reject a subtlex source missing any of them):

  ``limit``             — total verbs + nouns to keep. Default: 1400.
  ``verb_limit``        — verbs cap; default 400.
  ``noun_limit``        — nouns cap; default 1000.
  ``deck``              — verb deck prefix (REQUIRED).
  ``infinitive_deck``   — verb infinitive deck (REQUIRED).
  ``noun_deck``         — noun definite deck (REQUIRED, in ``extras``).
  ``phrases_deck``      — non-definite noun phrase deck (REQUIRED).

Internally, subtlex builds **two** views of the source (verb and noun, same
``source_path``) and delegates ingestion + cards to ``VerbMode`` / ``NounMode``.
"""

from __future__ import annotations

import csv
import unicodedata
from dataclasses import replace
from pathlib import Path
from typing import Iterable

from ..cards import Card
from ..csvio import CsvRow
from ..sources import Source
from .base import Mode
from .noun import NounMode
from .verb import VerbMode


def _read_subtlex(path: Path) -> list[dict]:
    out: list[dict] = []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter=";")
        for r in reader:
            out.append(r)
    return out


def _parse_zipf(v: str | None) -> float | None:
    if not v:
        return None
    try:
        return float(v.replace(",", "."))
    except ValueError:
        return None


def _is_garbage(lemma: str) -> bool:
    if not lemma:
        return True
    if len(lemma) == 1 and unicodedata.category(lemma[0]) in ("Lu", "Ll"):
        return True
    return any(
        not (unicodedata.category(ch).startswith("L") or unicodedata.category(ch).startswith("M"))
        for ch in lemma
    )


def _candidates(rows: list[dict], pos: str, limit: int) -> list[tuple[str, int, float | None]]:
    """Yield (lemma, frequency_rank, zipf) for top ``limit`` unique lemmas.

    ``frequency_rank`` is the 1-based position of the lemma's most frequent
    wordform in the (zipf-descending) SUBTLEX file. The file's own ``id``
    column is an arbitrary identifier, not a rank.
    """
    out: list[tuple[str, int, float | None]] = []
    seen: set[str] = set()
    for file_rank, r in enumerate(rows, start=1):
        if r.get("dom_pos") != pos:
            continue
        lemma_raw = (r.get("dom_lemma") or "").strip()
        if not lemma_raw or lemma_raw == "<unknown>":
            continue
        if _is_garbage(lemma_raw):
            continue
        lemma = lemma_raw.lower()
        if lemma in seen:
            continue
        seen.add(lemma)
        out.append((lemma_raw, file_rank, _parse_zipf(r.get("zipf"))))
        if len(out) >= limit:
            break
    return out


def _backfill_frequency(
    conn,
    sub_source: Source,
    candidates: list[tuple[str, int, float | None]],
    resolved: dict[str, str],
) -> None:
    """Update entries.frequency_rank/zipf, matching via the resolved lemma
    (the AI may normalise a candidate, e.g. ``centinaia`` → ``centinaio``)."""
    rows = [
        (freq_rank, zipf, sub_source.key, sub_source.mode, resolved[lemma])
        for lemma, freq_rank, zipf in candidates
        if lemma in resolved
    ]
    conn.executemany(
        "UPDATE entries SET frequency_rank = ?, zipf = ? "
        "WHERE source_path = ? AND mode = ? AND natural_id = ?",
        rows,
    )
    conn.commit()


def _sub_sources(source: Source) -> tuple[Source, Source]:
    """The verb and noun views of a subtlex source (same source_path)."""
    verb_deck, noun_deck, infinitive_deck, phrases_deck = _decks(source)
    verb_source = replace(
        source, mode="verb", deck=verb_deck, infinitive_deck=infinitive_deck,
        prompt_hint=source.prompt_hint or "Italian verb from a SUBTLEX-IT frequency-list extraction.",
    )
    noun_source = replace(
        source, mode="noun", deck=noun_deck, phrases_deck=phrases_deck,
        prompt_hint=source.prompt_hint or "Italian noun from a SUBTLEX-IT frequency-list extraction.",
    )
    return verb_source, noun_source


def _rows(candidates: list[tuple[str, int, float | None]]) -> list[CsvRow]:
    return [CsvRow(italian=lemma, english="", index=i) for i, (lemma, _r, _z) in enumerate(candidates, 1)]


class SubtlexMode(Mode):
    """Composite mode: top-N SUBTLEX verbs and nouns, run through the verb
    and noun modes as two views of the same source."""

    name = "subtlex"

    def ingest(self, source: Source, ctx) -> int:
        verb_limit = int(source.extras.get("verb_limit", 400))
        noun_limit = int(source.extras.get("noun_limit", 1000))
        if source.limit is not None:
            # A single combined limit splits 1:2 verbs:nouns.
            verb_limit = max(1, source.limit // 3)
            noun_limit = source.limit - verb_limit
        verb_source, noun_source = _sub_sources(source)
        rows = _read_subtlex(source.path)
        inserted = 0
        # Always run both, even with no candidates, so the live sets get
        # recorded (an empty list retires everything that dropped out).
        for mode, sub, cands in (
            (VerbMode(), verb_source, _candidates(rows, "VER", verb_limit)),
            (NounMode(), noun_source, _candidates(rows, "NOM", noun_limit)),
        ):
            n, resolved = mode.ingest_rows(sub, _rows(cands), ctx)
            inserted += n
            _backfill_frequency(ctx.conn, sub, cands, resolved)
        return inserted

    def cards(self, source: Source, conn) -> Iterable[Card]:
        verb_source, noun_source = _sub_sources(source)
        yield from VerbMode().cards(verb_source, conn)
        yield from NounMode().cards(noun_source, conn)


def _decks(source: Source) -> tuple[str, str, str, str]:
    """Pull the four required deck names off a subtlex source.

    All four fields are required by ``sources.validate()``; this helper just
    asserts the invariant at use time so a hand-built ``Source`` (e.g. in
    tests) trips a clear error rather than silently falling back to a default.
    """
    verb_deck = source.deck
    infinitive_deck = source.infinitive_deck
    noun_deck = source.extras.get("noun_deck")
    phrases_deck = source.phrases_deck
    missing = [
        name for name, val in (
            ("deck", verb_deck),
            ("infinitive_deck", infinitive_deck),
            ("noun_deck (extras)", noun_deck),
            ("phrases_deck", phrases_deck),
        ) if not val
    ]
    if missing:
        raise ValueError(
            f"subtlex source {source.id!r} missing required deck field(s): "
            f"{', '.join(missing)}"
        )
    return verb_deck, noun_deck, infinitive_deck, phrases_deck
