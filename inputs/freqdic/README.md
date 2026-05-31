# Frequency Dictionary Data

`subtlex-it.cleaned.csv` — a SUBTLEX-IT frequency list, post-processed with
an AI pass to correct mis-tagged parts of speech, malformed lemmas, and a
handful of bad translations in the original distribution.

The file is consumed by the `subtlex` mode in `sources.json`. The pipeline
walks the rows in `id` order, takes the top `verb_limit` lemmas tagged
`dom_pos=VER` and the top `noun_limit` lemmas tagged `dom_pos=NOM`, and
feeds them into the verb / noun pipelines.

## Source

The underlying data comes from the SUBTLEX-IT project:

- Files: https://osf.io/zg7sc/files/osfstorage
- Project overview: https://osf.io/zg7sc/overview

See the original distribution for citation information and usage terms.
