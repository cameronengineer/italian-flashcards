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

Internally, subtlex builds **two** virtual sub-sources, one per kind, and
delegates ingestion + materialisation to ``VerbMode`` / ``NounMode``.
"""

from __future__ import annotations

import csv
import unicodedata
from dataclasses import replace
from pathlib import Path

from ..csvio import CsvRow
from ..sources import Source
from .verb import VerbMode
from .noun import NounMode


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
    """Yield (lemma, frequency_rank, zipf) for top ``limit`` unique lemmas."""
    out: list[tuple[str, int, float | None]] = []
    seen: set[str] = set()
    rank = 0
    for r in rows:
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
        rank += 1
        try:
            freq_rank = int(r.get("id", "") or 0)
        except ValueError:
            freq_rank = 0
        out.append((lemma_raw, freq_rank, _parse_zipf(r.get("zipf"))))
        if len(out) >= limit:
            break
    return out


def _backfill_frequency(conn, sub_source: Source, candidates: list[tuple[str, int, float | None]]):
    """Update entries.frequency_rank/zipf for entries we just created."""
    sp = str(sub_source.path.resolve())
    rows = [(freq_rank, zipf, sp, lemma.lower()) for lemma, freq_rank, zipf in candidates]
    conn.executemany(
        "UPDATE entries SET frequency_rank = ?, zipf = ? "
        "WHERE source_path = ? AND natural_id = ?",
        rows,
    )
    conn.commit()


class _RowsAdapter:
    """Lightweight bridge: subtlex pre-computes CSV-like rows so it can reuse
    VerbMode._ingest_rows / NounMode._ingest_rows without reading from disk.
    """

    @staticmethod
    def for_lemmas(candidates: list[tuple[str, int, float | None]]) -> list[CsvRow]:
        return [
            CsvRow(italian=lemma, english="", index=i)
            for i, (lemma, _r, _z) in enumerate(candidates, start=1)
        ]


class SubtlexMode:
    name = "subtlex"

    def ingest(self, source: Source, ctx) -> int:
        verb_limit = int(source.extras.get("verb_limit", 400))
        noun_limit = int(source.extras.get("noun_limit", 1000))
        if source.limit is not None:
            # Split limit roughly 2:5 verb:noun if a single limit is given
            verb_limit = max(1, source.limit // 3)
            noun_limit = source.limit - verb_limit

        verb_deck, noun_deck, infinitive_deck, phrases_deck = _decks(source)

        verb_source = replace(
            source,
            mode="verb",
            deck=verb_deck,
            infinitive_deck=infinitive_deck,
            prompt_hint=(
                source.prompt_hint
                or "Italian verb from a SUBTLEX-IT frequency-list extraction."
            ),
        )
        noun_source = replace(
            source,
            mode="noun",
            deck=noun_deck,
            phrases_deck=phrases_deck,
            prompt_hint=(
                source.prompt_hint
                or "Italian noun from a SUBTLEX-IT frequency-list extraction."
            ),
        )

        rows = _read_subtlex(source.path)
        verb_cands = _candidates(rows, "VER", verb_limit)
        noun_cands = _candidates(rows, "NOM", noun_limit)

        inserted = 0
        if verb_cands:
            inserted += VerbMode()._ingest_rows(
                verb_source, _RowsAdapter.for_lemmas(verb_cands), ctx
            )
            _backfill_frequency(ctx.conn, verb_source, verb_cands)
        if noun_cands:
            inserted += NounMode()._ingest_rows(
                noun_source, _RowsAdapter.for_lemmas(noun_cands), ctx
            )
            _backfill_frequency(ctx.conn, noun_source, noun_cands)
        return inserted

    def materialise(self, source: Source, ctx) -> int:
        verb_deck, noun_deck, infinitive_deck, phrases_deck = _decks(source)
        verb_source = replace(source, mode="verb", deck=verb_deck, infinitive_deck=infinitive_deck)
        noun_source = replace(source, mode="noun", deck=noun_deck, phrases_deck=phrases_deck)
        n = 0
        n += VerbMode().materialise(verb_source, ctx)
        n += NounMode().materialise(noun_source, ctx)
        return n


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
