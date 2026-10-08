"""Italian flashcards pipeline (v4: word-first).

What to learn is declared in ``lists.toml`` (CILS levels, italki notes,
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
                └─▶ knowledge (what you know) ─▶ study order, movie coverage

Package map:
  settings.py  settings.toml loader         lists.py     lists.toml readers
  kaikki.py    Wiktionary dictionary index  srt.py       subtitles → lemmas
  italian.py   rules (articles, numbers …)  lexicon.py   root words + JSONL export
  ai.py        Claude client + wait gate    codex.py     images via Codex
  tasks.py     every AI task                queue.py     durable AI job queue
  notes.py     desired notes + notetype     reconcile.py Anki plan / apply
  db.py        schema + migrations          backup.py    DB snapshots
  commands/    audio, movie, audit, review, practice, learnt, leech, share
"""

__version__ = "4.0.0"
