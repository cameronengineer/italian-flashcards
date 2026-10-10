# Frequency Dictionary Data

`subtlex-it.cleaned.csv` — a SUBTLEX-IT frequency list, post-processed with
an AI pass to correct mis-tagged parts of speech, malformed lemmas, and a
handful of bad translations in the original distribution.

The file is consumed by the `subtlex` list in `lists.toml`. The pipeline
walks the rows in `id` order, takes the top `verb_limit` lemmas tagged
`dom_pos=VER` and the top `noun_limit` lemmas tagged `dom_pos=NOM`, and
resolves them into the shared lexicon. Its wordform and dominant-lemma columns
also supply the subtitle lemmatizer. Those dominant labels are corpus-wide
guesses; they do not establish a token's meaning in an individual film line.
The current CSV does not record the earlier AI correction pass as a per-row
patch history, so retain the file and its hash when reproducing a run.

## Source

The underlying data comes from the SUBTLEX-IT project:

- Files: https://osf.io/zg7sc/files/osfstorage
- Project overview: https://osf.io/zg7sc/overview

See the original distribution for citation information and usage terms.
