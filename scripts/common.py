#!/usr/bin/env python3
"""Shared utilities for Italian flashcard pipeline scripts."""

from __future__ import annotations

import hashlib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "database.sqlite"
API_KEY_FILE = PROJECT_ROOT / ".openrouter"


def load_api_key(path: Path = API_KEY_FILE) -> str:
    """Load an API key from a file, raising on missing or empty file."""
    if not path.exists():
        raise FileNotFoundError(
            f"API key file not found: {path}\n"
            f"Create it with: echo 'your-key-here' > {path}"
        )
    key = path.read_text(encoding="utf-8").strip()
    if not key:
        raise ValueError(f"API key file is empty: {path}")
    return key


def lemma_id(lemma: str) -> str:
    """Return the MD5 hex digest of the normalised lemma, used as word_entries.id."""
    return hashlib.md5(lemma.strip().lower().encode("utf-8")).hexdigest()


def media_hash(text: str) -> str:
    """Return the MD5 hex digest of *text*, used as the base for media filenames."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def audio_filename(text: str) -> str:
    """Return the MP3 filename for *text* (MD5 hash + .mp3)."""
    return f"{media_hash(text)}.mp3"


def image_filename(key: str, ext: str = "png") -> str:
    """Return the image filename for *key* (MD5 hash + extension)."""
    return f"{media_hash(key)}.{ext}"
