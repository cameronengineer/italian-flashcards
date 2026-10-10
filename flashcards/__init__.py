"""Italian flashcards pipeline (v4: word-first).

What to learn is declared in ``lists.toml`` (CILS levels, core vocabulary,
frequency lists, movie subtitles …), priorities in ``plan.toml``, tunables
in ``settings.toml``. ``flashcards run`` (or ``./run.sh``):

    lists ─▶ lexicon (one record per root word; Kaikki/Wiktionary + rules)
                │
                ├─▶ AI job queue: Claude verifies + writes prompts and fun facts,
                │                 Codex draws one image per root
                ▼
    notes (vocab, phrases, verb forms, noun phrases, cognates, mistakes)
                ▼
    Anki ◀── reconcile: add · update · adopt studied notes · retire · reorder
                │
                └─▶ card_knowledge (what you know) ─▶ study order, movie coverage

Package map:
  settings.py  settings.toml loader         lists.py     lists.toml / plan.toml readers
  kaikki.py    Wiktionary dictionary index  srt.py       subtitles → lemmas
  italian.py   rules (articles, numbers …)  lexicon.py   root words + JSONL export
  planning.py  study order, verb budget     overrides.py human edits, sense identity
  ai.py        Claude client + wait gate    codex.py     images via Codex
  tasks.py     every AI task                queue.py     durable AI job queue
  notes.py     desired notes + notetype     quality.py   one publication rule
  reconcile.py Anki plan / apply            ownership.py what the pipeline owns in Anki
  db.py        schema + migrations          backup.py    DB snapshots; recovery.py bundles
  assets.py    media lookup + manifest      runtime.py   atomic writes, writer lock
  commands/    audio, movie, audit, review, practice, learnt, leech, share, doctor
"""

__version__ = "4.0.0"
