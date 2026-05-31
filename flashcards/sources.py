"""Source registry. Reads the single ``sources.json`` at the repo root.

There used to be a sidecar JSON per CSV; that was replaced with one
authoritative manifest so it's obvious at a glance what gets built.

To add a new source: open ``sources.json`` and append an entry. To disable
one temporarily without deleting it: set ``"disabled": true``.

Every field except ``path`` and ``mode`` is optional.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .grammar import AVERE_PERSONS as _AVERE_PERSONS
from .paths import INPUTS_DIR, PROJECT_ROOT
from .util import humanize

# ─────────────────────────────────────────────────────────────────────────────
# Config object
# ─────────────────────────────────────────────────────────────────────────────

VALID_MODES = {"gloss", "avere", "verb", "noun", "subtlex"}

DEFAULT_MANIFEST = PROJECT_ROOT / "sources.json"


@dataclass(frozen=True)
class Source:
    """A single processable source. See ``sources.json`` for the live config."""

    path: Path
    mode: str
    deck: str

    enrich: bool = True
    image: bool = True
    audio: bool = True

    label_pill: str | None = None
    front_pill: str | None = None
    shuffle_window: int = 50
    prompt_hint: str = ""

    infinitive_deck: str | None = None
    phrases_deck: str | None = None

    limit: int | None = None

    extras: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        """Human-readable id (path relative to inputs/)."""
        try:
            return str(self.path.relative_to(INPUTS_DIR))
        except ValueError:
            return self.path.name


# ─────────────────────────────────────────────────────────────────────────────
# Loading
# ─────────────────────────────────────────────────────────────────────────────

#: Manifest keys mapped directly to ``Source`` dataclass attributes. Anything
#: not in this set OR ``EXTRA_FIELDS`` below triggers a typo warning in
#: ``_source_from_entry``.
KNOWN_FIELDS = {
    "path", "mode", "deck", "enrich", "image", "audio",
    "label_pill", "front_pill", "shuffle_window", "prompt_hint",
    "infinitive_deck", "phrases_deck", "limit",
    "disabled", "$comment",
}

#: Manifest keys that legitimately live in ``Source.extras`` (i.e. they are
#: mode-specific config without their own ``Source`` attribute). Anything
#: NOT in ``KNOWN_FIELDS`` and NOT here is treated as a typo by the manifest
#: validator — that catches ``nuon_limit: 1000`` before it silently falls
#: back to the default.
EXTRA_FIELDS = {
    "noun_deck",            # subtlex: definite-phrase noun deck
    "verb_limit",           # subtlex: top-N verbs to extract
    "noun_limit",           # subtlex: top-N nouns to extract
    "cards_per_expression", # avere: how many persons to fan each expression into
}

ALLOWED_FIELDS = KNOWN_FIELDS | EXTRA_FIELDS


def _default_deck(stem: str) -> str:
    return f"Italian - {humanize(stem)}"


def _source_from_entry(
    entry: dict,
    manifest_path: Path,
    parse_errors: list[str],
) -> Source | None:
    """Convert one JSON entry into a Source. ``None`` if disabled.

    Unknown keys (not in ``ALLOWED_FIELDS``) are appended to ``parse_errors``
    rather than silently falling into ``extras`` — that catches manifest
    typos like ``nuon_limit`` before they cost an AI run.
    """
    if entry.get("disabled"):
        return None
    path_str = entry.get("path")
    if not path_str:
        raise ValueError(f"{manifest_path}: every source must have a 'path'")
    mode = entry.get("mode", "gloss")
    if mode not in VALID_MODES:
        raise ValueError(
            f"{manifest_path}: unknown mode {mode!r} for {path_str!r} "
            f"(valid: {sorted(VALID_MODES)})"
        )
    unknown = [k for k in entry.keys() if k not in ALLOWED_FIELDS]
    if unknown:
        parse_errors.append(
            f"{path_str}: unknown manifest key(s): {sorted(unknown)} "
            f"(allowed: {sorted(ALLOWED_FIELDS)})"
        )
    csv_path = (INPUTS_DIR / path_str).resolve()
    deck = entry.get("deck") or _default_deck(Path(path_str).stem)
    # Only legitimately-extra fields go into ``extras``; unknown keys are
    # reported above and intentionally dropped so they never silently
    # influence behaviour downstream.
    extras = {k: v for k, v in entry.items() if k in EXTRA_FIELDS}
    return Source(
        path=csv_path,
        mode=mode,
        deck=deck,
        enrich=bool(entry.get("enrich", True)),
        image=bool(entry.get("image", True)),
        audio=bool(entry.get("audio", True)),
        label_pill=entry.get("label_pill"),
        front_pill=entry.get("front_pill"),
        shuffle_window=int(entry.get("shuffle_window", 50)),
        prompt_hint=entry.get("prompt_hint", ""),
        infinitive_deck=entry.get("infinitive_deck"),
        phrases_deck=entry.get("phrases_deck"),
        limit=entry.get("limit"),
        extras=extras,
    )


def load(manifest_path: Path = DEFAULT_MANIFEST) -> tuple[list[Source], list[str]]:
    """Read ``sources.json`` (or another manifest).

    Returns ``(sources, parse_errors)``. ``parse_errors`` contains manifest-level
    issues discovered while parsing (e.g. unknown keys) — callers should feed
    this into :func:`validate` so users see a single consolidated error list.
    """
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"Source manifest not found: {manifest_path}\n"
            f"Create it (see README.md) listing every CSV to ingest."
        )
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {manifest_path}: {exc}") from exc
    if not isinstance(data, dict) or "sources" not in data:
        raise ValueError(f"{manifest_path}: top level must be an object with a 'sources' list")
    parse_errors: list[str] = []
    sources: list[Source] = []
    for entry in data["sources"]:
        s = _source_from_entry(entry, manifest_path, parse_errors)
        if s is not None:
            sources.append(s)
    return sources, parse_errors


# Back-compat alias for cli/discover.
discover = load


# ─────────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────────

#: Bounds for ``avere.cards_per_expression``. Derived from
#: ``flashcards.grammar.AVERE_PERSONS`` so if the grammar definition ever
#: grows, the validator stays in sync.
_AVERE_CARDS_MIN = 1
_AVERE_CARDS_MAX = len(_AVERE_PERSONS)


def validate(sources: list[Source], parse_errors: list[str] | None = None) -> list[str]:
    """Return all configuration errors for a list of sources.

    ``parse_errors`` is the second element returned by :func:`load`; pass it
    through so any unknown-manifest-key issues are reported alongside the
    per-source checks below. Pure function — no module state mutated.
    """
    errors: list[str] = list(parse_errors) if parse_errors else []
    seen_paths: set[Path] = set()
    seen_decks: dict[str, str] = {}
    for s in sources:
        if not s.path.exists():
            errors.append(f"{s.id}: file missing at {s.path}")
        if s.path in seen_paths:
            errors.append(f"{s.id}: duplicate path entry")
        seen_paths.add(s.path)
        if s.mode == "verb" and not s.infinitive_deck:
            errors.append(f"{s.id}: mode='verb' requires 'infinitive_deck'")
        if s.mode == "noun" and not s.phrases_deck:
            errors.append(f"{s.id}: mode='noun' requires 'phrases_deck'")
        if s.mode == "avere" and "cards_per_expression" in s.extras:
            # Pre-flight check so a malformed value doesn't crash materialise
            # AFTER the (paid) AI ingest pass has already run.
            raw = s.extras["cards_per_expression"]
            try:
                n = int(raw)
            except (TypeError, ValueError):
                errors.append(
                    f"{s.id}: 'cards_per_expression' must be an integer, got {raw!r}"
                )
            else:
                if not (_AVERE_CARDS_MIN <= n <= _AVERE_CARDS_MAX):
                    errors.append(
                        f"{s.id}: 'cards_per_expression' must be in "
                        f"{_AVERE_CARDS_MIN}..{_AVERE_CARDS_MAX}, got {n}"
                    )
        if s.mode == "subtlex":
            # All four deck names are explicit — no fallbacks. This forces
            # sources.json to spell out exactly where SUBTLEX-derived cards
            # land, so renames or new freq-lists can't accidentally collide
            # with hand-curated decks.
            if not s.deck:
                errors.append(f"{s.id}: mode='subtlex' requires 'deck' (verb deck prefix)")
            if not s.infinitive_deck:
                errors.append(f"{s.id}: mode='subtlex' requires 'infinitive_deck'")
            if not s.extras.get("noun_deck"):
                errors.append(f"{s.id}: mode='subtlex' requires 'noun_deck'")
            if not s.phrases_deck:
                errors.append(f"{s.id}: mode='subtlex' requires 'phrases_deck'")
        existing = seen_decks.get(s.deck)
        if existing and existing != s.id:
            errors.append(
                f"{s.id}: deck name {s.deck!r} already claimed by {existing}"
            )
        seen_decks[s.deck] = s.id
    return errors


def summarise(sources: list[Source]) -> str:
    lines = [f"Discovered {len(sources)} source(s):"]
    for s in sources:
        lines.append(f"  [{s.mode:<7}] {s.id:<45} deck={s.deck!r}")
    return "\n".join(lines)
