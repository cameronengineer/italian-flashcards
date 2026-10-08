# Lexicon (generated — reviewable, editable)

`flashcards lexicon` / `flashcards run` regenerate these files from the
database on every run:

| File | Contents |
|---|---|
| `lexemes.jsonl` | One root word per line: lemma, part of speech, display form, gender, plural, forms, IPA, etymology, card senses (prompt / hint / note / also), fun fact, status, and per-field `provenance` (`kaikki`, `rule`, `claude:<model>`, `human`). |
| `lists.jsonl` | Which list each root comes from, its rank and the source's own gloss. Movie example lines are deliberately excluded (subtitle text stays local). |
| `identity.csv` | Every note key ever sent to Anki — the drift guard uses it to protect review history. |

**Editing:** change a field in `lexemes.jsonl` and set its provenance to
`"human"` (e.g. `"provenance": {"senses": "human"}`); the next run applies it
and AI refreshes never overwrite it.

**Attribution / licence:** dictionary data (parts of speech, gender, forms,
IPA, etymology text) comes from [Wiktionary](https://en.wiktionary.org) via
[kaikki.org](https://kaikki.org) (wiktextract; Ylonen, LREC 2022) and is
licensed [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).
The files in this directory are shared under the same licence.
