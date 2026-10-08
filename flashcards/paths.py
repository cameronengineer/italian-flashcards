"""Centralised filesystem paths for the flashcards pipeline."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

INPUTS_DIR = PROJECT_ROOT / "inputs"
DECKS_DIR = PROJECT_ROOT / "decks"
DATA_DIR = PROJECT_ROOT / "data"          # reference data indexes (gitignored)
LEXICON_DIR = PROJECT_ROOT / "lexicon"    # versioned lexicon JSONL (in git)
DATASETS_DIR = PROJECT_ROOT / "datasets"  # per-movie word lists (gitignored)
BACKUPS_DIR = PROJECT_ROOT / "backups"

MEDIA_DIR = PROJECT_ROOT / "media"
AUDIO_DIR = MEDIA_DIR / "audio"
AUDIO_DIR_COMPRESSED = MEDIA_DIR / "audio_compressed"
IMAGE_DIR = MEDIA_DIR / "images"
IMAGE_DIR_COMPRESSED = MEDIA_DIR / "images_compressed"

# ``FLASHCARDS_DB`` points the pipeline at another database (e.g. a scratch copy).
DB_PATH = Path(os.environ.get("FLASHCARDS_DB") or PROJECT_ROOT / "database.sqlite")

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
