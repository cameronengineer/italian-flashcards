"""Italian flashcards pipeline.

Drop CSVs (or folders of CSVs) into ``inputs/``. Optional sidecar JSON files
configure per-source behaviour. Run ``python -m flashcards run``.

The high-level flow:

    inputs/  ──[discover]──▶  sources  ──[ingest]──▶  entries
                                                     │
                          ┌──[verb mode]─▶ verb_forms (AI)
                          ├──[noun mode]─▶ noun_phrases (AI)
                          └─────────────────────────────▼
                                            [materialize] ──▶ cards
                                                        │
                                            [media] audio + images
                                                        │
                                            [export] decks/*.apkg
                                                        │
                                            [sync] AnkiConnect
"""

from .paths import PROJECT_ROOT, INPUTS_DIR, DECKS_DIR, MEDIA_DIR, DB_PATH  # noqa: F401

__version__ = "2.0.0"
