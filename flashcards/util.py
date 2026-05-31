"""Hashing, identifier, and small utility helpers."""

from __future__ import annotations

import hashlib
import re
from typing import Iterable, Iterator, TypeVar

T = TypeVar("T")


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def entry_id(source_path: str, natural_id: str) -> str:
    """Stable ID for an `entries` row, derived from the source + natural id."""
    return md5_hex(f"{source_path}::{natural_id}")


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
