"""Italian flashcards pipeline.

Inputs are declared in ``sources.json``; tunables live in ``settings.toml``.
Run ``flashcards run`` (or ``./run.sh``). The flow:

    sources.json ─▶ modes (ingest) ─▶ entries / verb_forms / noun_phrases
                                          │
                     modes (cards) ─▶ Card objects ─▶ fun facts (word_facts)
                                          │
                                       cards table ─▶ audio + images
                                          │
                                       decks/*.apkg ─▶ Anki (AnkiConnect)

Package map:
  settings.py   settings.toml loader          tasks.py   every AI task (prompts + schemas)
  ai.py         the one AI client + cache      cards.py   card presentation + note model
  sources.py    sources.json loader/validator  facts.py   fun facts
  modes/        one Mode subclass per mode     db.py      schema + migrations
  commands/     one module per CLI command     anki.py    AnkiConnect client
"""

__version__ = "3.0.0"
