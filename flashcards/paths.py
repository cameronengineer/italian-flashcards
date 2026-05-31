"""Centralised filesystem paths for the flashcards pipeline."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

INPUTS_DIR = PROJECT_ROOT / "inputs"
DECKS_DIR = PROJECT_ROOT / "decks"
BACKUPS_DIR = PROJECT_ROOT / "backups"

MEDIA_DIR = PROJECT_ROOT / "media"
AUDIO_DIR = MEDIA_DIR / "audio"
AUDIO_DIR_COMPRESSED = MEDIA_DIR / "audio_compressed"
IMAGE_DIR = MEDIA_DIR / "images"
IMAGE_DIR_COMPRESSED = MEDIA_DIR / "images_compressed"

DB_PATH = PROJECT_ROOT / "database.sqlite"

OPENROUTER_KEY_FILE = PROJECT_ROOT / ".openrouter"
ELEVENLABS_KEY_FILE = PROJECT_ROOT / ".elevenlabs"


def ensure_dirs() -> None:
    """Create all output directories. Safe to call repeatedly."""
    for d in (
        DECKS_DIR,
        BACKUPS_DIR,
        MEDIA_DIR,
        AUDIO_DIR,
        AUDIO_DIR_COMPRESSED,
        IMAGE_DIR,
        IMAGE_DIR_COMPRESSED,
    ):
        d.mkdir(parents=True, exist_ok=True)
