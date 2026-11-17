# Lexicon (generated — reviewable, editable)

`flashcards lexicon` / `flashcards run` regenerate these files from the
database on every run:

| File | Contents |
|---|---|
| `lexemes.jsonl` | One root word per line: lemma, part of speech, display form, gender, plural, forms, IPA, etymology, card senses (prompt / hint / note / also), fun fact, status, and per-field `provenance` (`kaikki`, `rule`, `claude:<model>`, `human`). |
| `lists.jsonl` | Which list each root comes from, its rank and the source's own gloss (only the lists in `lists.toml`; film example lines are in each film's `words.csv`). |

**Editing:** change a field in `lexemes.jsonl` and set its provenance to
`"human"` (e.g. `"provenance": {"senses": "human"}`); the next run applies it
and AI refreshes never overwrite it.

You can edit it while `./run.sh` is going: before the run writes the file
again (after each round), it notices your changes and takes in the fields you
marked `human` first. They reach Anki at the next sync, or the next run.
The SQLite database is authoritative for overrides, queue state,
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
