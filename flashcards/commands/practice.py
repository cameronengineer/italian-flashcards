"""``practice`` command — interactive translation practice from learnt words.

Each round samples learnt cards from Anki, asks the AI for an English
sentence using some of them (``tasks.PRACTICE_SENTENCE``), lets you type
the Italian, and gives focused feedback (``tasks.PRACTICE_FEEDBACK``).
Models and defaults come from ``settings.toml`` (``[practice]`` and
``[ai.task_models]``). Nothing is cached — every sentence is fresh.
"""

from __future__ import annotations

import random

try:
    import readline  # noqa: F401 — enables arrow-key line editing in input()
except ImportError:
    pass

from ..ai import AI, AIError
from ..anki import learnt_pairs
from ..settings import settings
from ..tasks import PRACTICE_FEEDBACK, PRACTICE_SENTENCE
from ..util import print_banner

LENGTH_GUIDANCE = {
    "short": "The sentence should be short and simple (about 5-10 words).",
    "medium": "The sentence should be of moderate length (about 12-20 words), with at "
    "least one subordinate or relative clause.",
    "long": "The sentence should be long and complex (about 20-35 words), with multiple "
    "clauses (e.g. subordinate, relative, or conditional). It should still feel "
    "natural and conversational, not run-on.",
}

STYLE_GUIDANCE = {
    "direct-pronouns": "The Italian translation MUST use at least one direct object pronoun "
    "(mi, ti, lo, la, ci, vi, li, le) — ideally more than one.",
    "indirect-pronouns": "The Italian translation MUST use at least one indirect object "
    "pronoun (mi, ti, gli, le, ci, vi, gli/loro).",
    "combined-pronouns": "The Italian translation MUST use at least one combined pronoun "
    "(e.g. me lo, te la, glielo, gliela, ce ne, ve li).",
    "reflexive": "The Italian translation MUST use at least one reflexive verb "
    "(e.g. svegliarsi, lavarsi, divertirsi, accorgersi).",
    "passato-prossimo": "The Italian translation MUST be in the passato prossimo, with the "
    "correct auxiliary (essere/avere) and past participle agreement.",
    "imperfetto": "The Italian translation MUST use the imperfetto, ideally for a habitual "
    "past action or a setting/description.",
    "imperfetto-vs-passato": "The Italian translation MUST contrast the imperfetto and "
    "passato prossimo in the same sentence (background vs. completed action).",
    "future": "The Italian translation MUST use the futuro semplice.",
    "conditional": "The Italian translation MUST use the condizionale (present or past).",
    "subjunctive": "The Italian translation MUST use the congiuntivo, triggered by an "
    "appropriate expression (e.g. penso che, è importante che, benché).",
    "imperative": "The Italian translation MUST use at least one imperativo form "
    "(tu, noi, voi, or formal Lei).",
    "ci-ne": "The Italian translation MUST use the particle 'ci' and/or 'ne' "
    "(e.g. ci vado, ne ho due, ce ne sono).",
    "relative": "The Italian translation MUST contain at least one relative clause "
    "(introduced by che, cui, il quale, etc.).",
    "conditional-if": "The Italian translation MUST be a 'periodo ipotetico' (if/then) "
    "sentence — first, second, or third type — with the right tense/mood combination.",
    "question": "The English sentence MUST be phrased as a question, and the Italian "
    "translation should reflect natural question word order.",
}

NO_SUBJUNCTIVE_SENTENCE = (
    "The Italian translation MUST NOT use the subjunctive mood (congiuntivo) anywhere. "
    "Avoid structures that would normally trigger it (e.g. penso che, benché, affinché); "
    "use indicative or infinitive constructions instead."
)
NO_SUBJUNCTIVE_FEEDBACK = (
    "The learner is not yet studying the subjunctive: do NOT flag or correct anything "
    "related to the congiuntivo, even if the correct Italian would normally require it."
)


def list_styles() -> int:
    print("\nAvailable --style options:\n")
    width = max(len(s) for s in STYLE_GUIDANCE)
    for name in sorted(STYLE_GUIDANCE):
        print(f"  {name.ljust(width)}  {STYLE_GUIDANCE[name]}")
    print()
    return 0


def _learnt(deck: str | None) -> list[tuple[str, str]]:
    seen: dict[str, str] = {}
    for pairs in learnt_pairs(deck).values():
        for italian, english in pairs:
            seen.setdefault(italian, english)
    return list(seen.items())


def run(
    *,
    words: int | None = None,
    sentences: int | None = None,
    deck: str | None = None,
    seed: int | None = None,
    length: str | None = None,
    styles: list[str] | None = None,
    no_subjunctive: bool | None = None,
) -> int:
    print_banner("practice — translate sentences built from your learnt words")
    cfg = settings.practice
    words = words or cfg.words
    sentences = sentences or cfg.sentences
    length = length or cfg.length
    styles = styles or []
    no_subjunctive = cfg.no_subjunctive if no_subjunctive is None else no_subjunctive

    print("\nFetching learnt cards from Anki...", flush=True)
    try:
        pool = _learnt(deck)
    except RuntimeError as exc:
        print(f"  ERROR: {exc}")
        return 1
    if not pool:
        print("No learnt cards found.")
        return 1
    print(f"  Found {len(pool)} learnt words.", flush=True)

    rules = [LENGTH_GUIDANCE.get(length, LENGTH_GUIDANCE["medium"])]
    rules += [STYLE_GUIDANCE[s] for s in styles if s in STYLE_GUIDANCE]
    if no_subjunctive:
        rules.append(NO_SUBJUNCTIVE_SENTENCE)
    feedback_rules = (NO_SUBJUNCTIVE_FEEDBACK,) if no_subjunctive else ()

    ai = AI()
    rng = random.Random(seed)
    print("\n" + "=" * 60 + "\n  ITALIAN TRANSLATION PRACTICE\n" + "=" * 60)
    print(f"  Length: {length}")
    print(f"  Styles: {', '.join(styles) if styles else '(none — free-form)'}")
    if no_subjunctive:
        print("  Subjunctive: disabled")
    print("  Type your Italian translation, or press Enter to skip.\n" + "=" * 60)

    mistakes: list[dict] = []
    for i in range(1, sentences + 1):
        bank = rng.sample(pool, min(words, len(pool)))
        print(f"\n  Generating sentence {i}/{sentences}...", flush=True)
        try:
            item = ai.run(
                PRACTICE_SENTENCE.task(
                    {"word_bank": [{"italian": it, "english": en} for it, en in bank]},
                    rules=tuple(rules),
                )
            )
        except AIError as exc:
            print(f"ERROR: {exc}")
            break
        print(f"\n  {i}/{sentences}  {item['english']}\n")
        try:
            attempt = input("  Your Italian: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if attempt:
            print("\n  Getting feedback...", flush=True)
            try:
                feedback = ai.run(
                    PRACTICE_FEEDBACK.task(
                        {"english": item["english"], "correct_italian": item["italian"], "attempt": attempt},
                        rules=feedback_rules,
                    )
                )
            except AIError:
                feedback = "(Could not retrieve feedback)"
            print(f"\n  {'EN:'.ljust(10)} {item['english']}")
            print(f"  {'IT:'.ljust(10)} {item['italian']}")
            print(f"  You wrote: {attempt}\n")
            for line in feedback.splitlines():
                print(f"  {line}")
            mistakes.append(
                {
                    "english": item["english"],
                    "correct_italian": item["italian"],
                    "attempt": attempt,
                    "feedback": feedback,
                }
            )
        print("\n  " + "-" * 56)
    print("\n" + "=" * 60 + "\n  Done!\n" + "=" * 60)
    if mistakes:
        _mine_mistakes(ai, mistakes)
    return 0


def _mine_mistakes(ai: AI, mistakes: list[dict]) -> None:
    """Turn this session's errors into flashcards (Italian::Mistakes)."""
    from contextlib import closing

    from ..db import connect, init_schema
    from ..tasks import MISTAKE_CARDS
    from ..util import md5_hex

    try:
        res = ai.run(MISTAKE_CARDS.task({"mistakes": mistakes}))
    except AIError as exc:
        print(f"  (couldn't turn mistakes into cards: {exc})")
        return
    cards = [c for c in res.get("cards", []) if c.get("italian") and c.get("english")]
    with closing(connect()) as conn:
        init_schema(conn)
        for c in cards:
            conn.execute(
                "INSERT OR IGNORE INTO mistakes (id, italian, english, note) VALUES (?, ?, ?, ?)",
                (
                    md5_hex(c["italian"].strip().lower()),
                    c["italian"].strip(),
                    c["english"].strip(),
                    c.get("note") or None,
                ),
            )
        conn.commit()
    if cards:
        print(
            f"\n  {len(cards)} mistake card(s) saved — they reach Anki (deck Italian::Mistakes) on the next run."
        )
