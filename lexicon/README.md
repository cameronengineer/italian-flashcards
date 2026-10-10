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

Stop `./run.sh` before editing this generated file: a running run exports it
again and can overwrite edits that have not yet been imported. The next
`./run.sh` imports your edits first thing.
The SQLite database is authoritative for overrides, queue state, adoptions,
audit decisions and Anki identity. These JSONL files alone are not a recovery
bundle; use `./run.sh recovery create PATH --include-media` for a verified copy
of the database, inputs, configuration, exports and media. Keep that bundle
private. It currently does not include the dictionary index or the Anki
collection, which need separate backups for a fully offline restoration.

**Attribution / licence:** dictionary data (parts of speech, gender, forms,
IPA, etymology text) comes from [Wiktionary](https://en.wiktionary.org) via
[kaikki.org](https://kaikki.org) (wiktextract; Ylonen, LREC 2022) and is
licensed [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).
The files in this directory are shared under the same licence.
