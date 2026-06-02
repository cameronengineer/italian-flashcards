#!/usr/bin/env python3
"""One-shot cleanup for the verb-mode regression where _fill_forms briefly
asked the AI to conjugate every non-verb entry that shared a source_path with
a verb entry (the SUBTLEX source mixes both). The bug is fixed in
flashcards/modes/verb.py:_fill_forms (now filters mode='verb'); this script
removes the data damage from the aborted run.

Idempotent: re-running it after a clean DB is a no-op.

Cleanup steps, in a single transaction:
  1. For each non-verb entry that has any verb_forms rows attached, derive
     the cache key that would have been used by _forms_prompt(entry, TENSES)
     and delete that row from ai_cache. This stops the cache from replaying
     bogus noun-as-verb conjugations on the next run.
  2. Delete every verb_forms row whose entry_id resolves to a non-verb entry.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running as `./.venv/bin/python scripts/cleanup_bogus_verb_forms.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flashcards.db import connect, transaction  # noqa: E402
from flashcards.grammar import TENSES  # noqa: E402
from flashcards.modes.verb import VerbMode  # noqa: E402
from flashcards.openrouter import DEFAULT_MODEL, _cache_key  # noqa: E402


def main() -> int:
    conn = connect()
    vm = VerbMode()

    bad = conn.execute(
        """
        SELECT DISTINCT e.id, e.italian, e.english, e.infinitive,
               e.auxiliary, e.past_participle, e.is_reflexive
        FROM entries e
        WHERE e.mode != 'verb'
          AND EXISTS (SELECT 1 FROM verb_forms vf WHERE vf.entry_id = e.id)
        """
    ).fetchall()
    print(f"non-verb entries with bogus verb_forms: {len(bad)}")

    # Reconstruct cache keys for the bogus AI calls. At the time the bogus
    # backfill fired, each non-verb entry had zero verb_forms rows, so the
    # `missing` list passed to _forms_prompt was exactly list(TENSES). The
    # prompt is a pure function of (entry, missing), so the md5 we derive
    # here matches what _cache_key wrote.
    keys_to_delete: list[str] = []
    for row in bad:
        prompt = vm._forms_prompt(row, list(TENSES))
        keys_to_delete.append(_cache_key(prompt, "verb_forms", DEFAULT_MODEL))

    with transaction(conn):
        cache_deleted = 0
        # executemany on DELETE doesn't expose total rowcount reliably across
        # versions; loop and accumulate.
        for k in keys_to_delete:
            cur = conn.execute("DELETE FROM ai_cache WHERE cache_key = ?", (k,))
            cache_deleted += cur.rowcount if cur.rowcount > 0 else 0

        cur = conn.execute(
            """
            DELETE FROM verb_forms
            WHERE entry_id IN (
                SELECT id FROM entries WHERE mode != 'verb'
            )
            """
        )
        forms_deleted = cur.rowcount

    print(f"ai_cache rows removed:      {cache_deleted}")
    print(f"verb_forms rows removed:    {forms_deleted}")

    remaining = conn.execute(
        """
        SELECT COUNT(*) AS n FROM verb_forms vf
        JOIN entries e ON e.id = vf.entry_id
        WHERE e.mode != 'verb'
        """
    ).fetchone()["n"]
    print(f"non-verb verb_forms remaining (should be 0): {remaining}")
    return 0 if remaining == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
