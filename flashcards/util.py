"""Hashing, identifier, and small utility helpers."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Iterator, TypeVar

T = TypeVar("T")


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def entry_id(source_key: str, mode: str, natural_id: str) -> str:
    """Stable ID for a new `entries` row.

    ``source_key`` is the path relative to ``inputs/`` so the ID survives the
    repo moving. Rows created before that change keep their stored legacy IDs
    (derived from the absolute path) — IDs are never re-derived for existing
    rows, which is what keeps their Anki GUIDs stable.
    """
    return md5_hex(f"{source_key}::{mode}::{natural_id}")


def note_key(natural_key: str, direction: str) -> str:
    """Stable per-note identity written into the Anki ``SortKey`` field."""
    return f"{natural_key}|{direction}"


def has_control_chars(text: str | None) -> bool:
    return bool(text) and any(unicodedata.category(ch) == "Cc" for ch in text)


def clean_text(text: str | None, fallback: str = "") -> str:
    """Trimmed AI text, or ``fallback`` if it contains control characters.

    The model occasionally emits a NUL in place of an accented letter
    (``maestà`` → ``maest\x00``). Stripping the NUL would leave a wrong word,
    so corrupted values are replaced by the caller's fallback instead.
    """
    if has_control_chars(text):
        return fallback
    return (text or "").strip()


def media_hash(text: str) -> str:
    """Content-addressed media filename hash. Stable across DB rebuilds."""
    return md5_hex(text.strip())


def audio_filename(text: str) -> str:
    return f"{media_hash(text)}.mp3"


def image_filename(key: str, ext: str = "png") -> str:
    return f"{media_hash(key)}.{ext}"


def slugify(text: str) -> str:
    """Filesystem-safe slug from a deck name (e.g. 'Italian - Verbs' → 'italian_verbs')."""
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug or "deck"


def humanize(name: str) -> str:
    """Filename stem → human-readable deck suffix.

    >>> humanize('italian_interjections')
    'Interjections'
    >>> humanize('italki_verbs')
    'Italki Verbs'
    """
    stem = name
    # Strip a leading 'italian_' so the default deck prefix isn't doubled.
    for prefix in ("italian_", "italian-", "it_", "it-"):
        if stem.lower().startswith(prefix):
            stem = stem[len(prefix):]
    return " ".join(p.capitalize() for p in re.split(r"[\s_-]+", stem) if p)


def chunked(items: list[T], size: int) -> Iterator[list[T]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def print_banner(title: str) -> None:
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


def load_key_file(path) -> str:
    p = path
    if not p.exists():
        raise FileNotFoundError(
            f"API key file not found: {p}\n"
            f"Create it with: echo 'your-key-here' > {p}"
        )
    key = p.read_text(encoding="utf-8").strip()
    if not key:
        raise ValueError(f"API key file is empty: {p}")
    return key


def table(headers: list[str], rows: list[list], *, total: list | None = None) -> str:
    """A boxed plain-text table (used by every reporting command)."""
    cells = [[str(c) for c in r] for r in rows]
    foot = [str(c) for c in total] if total else None
    widths = [len(h) for h in headers]
    for r in cells + ([foot] if foot else []):
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))

    def line(char: str = "-") -> str:
        return "+" + "+".join(char * (w + 2) for w in widths) + "+"

    def fmt(r: list[str]) -> str:
        return "| " + " | ".join(c.ljust(w) for c, w in zip(r, widths)) + " |"

    out = [line(), fmt(headers), line("=")] + [fmt(r) for r in cells]
    if foot:
        out += [line(), fmt(foot)]
    out.append(line())
    return "\n".join(out)
