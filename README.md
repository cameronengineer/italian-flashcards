# Italian Flashcards

Drop CSVs into `inputs/`, register them in `sources.json`, run `./run.sh`, get
Anki decks with audio, AI images, and stable Anki note GUIDs.

The pipeline is data-driven:

- Every input is one entry in `sources.json` at the repo root.
- Each entry points to a CSV with an `italian,english` header.
- Optional fields configure the mode (gloss / verb / noun / avere / subtlex),
  the deck name, AI enrichment, audio / image generation, and shuffle window.

To add a new source: add the CSV under `inputs/`, append a block to
`sources.json`, run `./run.sh`. No code edits anywhere.

---

## Quick start

```sh
echo "sk-or-..." > .openrouter
echo "sk_..."   > .elevenlabs

./run.sh
```

`run.sh` and every `scripts/*.sh` wrapper auto-create `.venv` and install
`requirements.txt` on first run (and re-install whenever `requirements.txt`
changes). No manual venv setup is required.

`run.sh` also validates `sources.json` (via `python -m flashcards discover`)
before doing any AI work — typos in the manifest fail fast.

The script is idempotent **with respect to the build database and media files**
(re-runs only do new work). It is *not* idempotent with respect to your Anki
collection — the `sync` step at the end deletes Anki notes that are no longer
in the local DB. See the "Sync safety" section below.

Disk artefacts the pipeline produces:

- `database.sqlite` — pipeline state.
- `media/audio/<md5>.mp3`, `media/images/<md5>.png` — content-addressed.
- `media/audio_compressed/`, `media/images_compressed/` — packaged into decks.
- `decks/<slug>.apkg` — final Anki decks.

---

## `sources.json`

Single source of truth. Live example:

```jsonc
{
  "sources": [
    {
      "path": "italian_interjections.csv",
      "mode": "gloss",
      "deck": "Italian - Interjections",
      "front_pill": "type: interjection",
      "shuffle_window": 0,
      "prompt_hint": "Italian exclamations, greetings..."
    },
    {
      "path": "italki/italki_verbs.csv",
      "mode": "verb",
      "deck": "Italian - Italki Verbs",
      "infinitive_deck": "Italian - Italki Verbs Infinitive",
      "label_pill": "source: italki",
      "shuffle_window": 0
    },
    {
      "path": "freqdic/subtlex-it.cleaned.csv",
      "mode": "subtlex",
      "deck": "Italian - Verbs",
      "infinitive_deck": "Italian - Verbs Infinitive",
      "noun_deck": "Italian - Nouns",
      "phrases_deck": "Italian - Noun Phrases",
      "verb_limit": 400,
      "noun_limit": 1000,
      "shuffle_window": 50
    }
  ]
}
```

Required: `path` and (implicitly) a `mode`. Every other field is optional.

| Field             | Default | Purpose                                                                 |
|-------------------|---------|-------------------------------------------------------------------------|
| `path`            | —       | CSV path relative to `inputs/`.                                          |
| `mode`            | `gloss` | `gloss` \| `avere` \| `verb` \| `noun` \| `subtlex`.                     |
| `deck`            | derived | Primary deck name. Default: `"Italian - <humanized CSV stem>"`.          |
| `enrich`          | `true`  | For `gloss`/`avere`: call the AI for a richer English gloss + notes.     |
| `image`           | `true`  | Generate AI images.                                                      |
| `audio`           | `true`  | Generate ElevenLabs audio.                                               |
| `label_pill`      | —       | Pipe-suffix on every card's labels (e.g. `"source: italki"`).            |
| `front_pill`      | —       | Default front label pill, overrides the auto `"type: …"` pill.           |
| `shuffle_window`  | `50`    | Sliding-window shuffle width. `0` preserves CSV order.                   |
| `prompt_hint`     | —       | Free-text guidance for the AI prompt.                                    |
| `infinitive_deck` | —       | Required for `mode='verb'` and `mode='subtlex'`.                         |
| `phrases_deck`    | —       | Required for `mode='noun'` and `mode='subtlex'`.                         |
| `noun_deck`       | —       | Required for `mode='subtlex'`. Definite-phrase noun deck.                |
| `verb_limit`      | `400`   | For `mode='subtlex'`: top-N verbs to extract.                            |
| `noun_limit`      | `1000`  | For `mode='subtlex'`: top-N nouns.                                       |
| `cards_per_expression` | `2` | For `mode='avere'`: how many persons to fan each expression out into.    |
| `disabled`        | `false` | Set `true` to temporarily skip a source.                                  |

For `mode='subtlex'`, all four deck fields (`deck`, `infinitive_deck`,
`noun_deck`, `phrases_deck`) are required — there are no hidden defaults.
This forces the manifest to spell out exactly where SUBTLEX-extracted cards
land, so renames or adding a second frequency source cannot accidentally
collide with hand-curated decks.

### Modes

| mode      | What it does                                                                          |
|-----------|---------------------------------------------------------------------------------------|
| `gloss`   | One card per CSV row. English gloss on the back. Default.                              |
| `avere`   | Two persons per row, conjugated for the `avere`-expression family.                     |
| `verb`    | Full verb pipeline: 22 conjugated forms + 1 infinitive card per row.                   |
| `noun`    | Definite phrases → main deck; other variants → `phrases_deck`.                         |
| `subtlex` | Read a SUBTLEX-IT frequency CSV; auto-extract top-N verbs + nouns.                     |

---

## Adding sources

### Add a CSV

```sh
cat > inputs/italian_proverbs.csv <<EOF
italian,english
Chi dorme non piglia pesci,The early bird catches the worm
EOF
```

Then append to `sources.json`:

```jsonc
  {
    "path": "italian_proverbs.csv",
    "mode": "gloss",
    "deck": "Italian - Proverbs",
    "front_pill": "type: proverb"
  }
```

`./run.sh` produces `decks/italian_proverbs.apkg`.

### Add a folder

Folders are just paths — each CSV needs its own `sources.json` entry. Put
the CSVs in `inputs/<folder>/` and register them by path:

```jsonc
  { "path": "tutor/week_01.csv", "mode": "gloss", "deck": "Italian - Tutor Week 01" },
  { "path": "tutor/week_02.csv", "mode": "gloss", "deck": "Italian - Tutor Week 02" },
  { "path": "tutor/verbs.csv",   "mode": "verb",
    "deck": "Italian - Tutor Verbs",
    "infinitive_deck": "Italian - Tutor Verbs Inf",
    "label_pill": "source: tutor" }
```

### Inspect what is registered (no DB writes):

```sh
python -m flashcards discover
```

---

## Pipeline

```
sources.json  ──[load]──▶  sources
                  │
                  ▼
            [ingest]  →  entries
                         verb_forms     (mode=verb)
                         noun_phrases   (mode=noun)
                  │
                  ▼
            [materialise]  →  cards (en↔it, stable GUIDs, frequency-aware order)
                  │
                  ▼
            [media]  →  media/audio/<md5>.mp3       (ElevenLabs)
                        media/images/<md5>.png      (riverflow-v2-fast)
                        media/audio_compressed/     (48kbps mono)
                        media/images_compressed/    (JPEG 512px q75)
                  │
                  ▼
            [export]  →  decks/<slug>.apkg          (genanki)
                  │
                  ▼
            [sync]    →  AnkiConnect importPackage
                      →  delete orphan notes (safeguarded)
                      →  reorder new-card queue
```

Each step is also a CLI subcommand:

```sh
python -m flashcards build               # ingest + materialise + sort
python -m flashcards audio               # only ElevenLabs
python -m flashcards images              # only AI images
python -m flashcards compress            # media compression
python -m flashcards export              # only .apkg writing
python -m flashcards sync                # AnkiConnect push
python -m flashcards run                 # all of the above
python -m flashcards discover            # dry-run discovery
```

Useful flags:

- `--workers N` — parallelism (default 10).
- `--source <id>` — restrict `build` to one CSV (id == `path` from `sources.json`).
- `--skip-ai` — for `build`, re-materialise existing entries without calling the AI.
- `--limit N` — per-stage media generation limit.
- `--no-sync` — `run` without touching AnkiConnect.
- `--dry-run` — `sync` only.
- `--allow-orphan-delete` — `sync` only; opt-in to deleting > 200 Anki notes
  or > 10% of any deck. Without this, sync refuses to delete when those
  thresholds are exceeded.

If any source's ingest fails during `run`, the AnkiConnect sync is
automatically skipped — partial builds never reach destructive Anki state.

---

## Sync safety

The `sync` step deletes Anki notes whose `SortKey` is no longer in the local
DB. That's how the deck stays in sync when you remove a CSV row. It is also
how a bad input set can wipe your review history.

Safeguards:

1. `cmd_run` runs `sync` only if every source's ingest succeeded.
2. `_delete_orphans` refuses to delete more than **200 notes absolute** or
   more than **10% of any deck** unless `--allow-orphan-delete` is passed.
3. If the local `cards` table is empty, `_delete_orphans` aborts entirely.
4. The SQLite database is backed up under `backups/` before every `run`.

Recommended workflow after a non-trivial change to `sources.json`:

```sh
./run.sh --no-sync                            # build + export, skip sync
python -m flashcards sync --dry-run           # preview every action
python -m flashcards sync                     # commit
```

---

## Database

Single SQLite file at `database.sqlite`. Five tables:

- **`entries`** — one row per logical "thing we know about" (a CSV row, a
  SUBTLEX-derived lemma). Holds gloss, optional verb metadata, optional
  noun metadata, optional frequency info.
- **`verb_forms`** — 22 conjugated forms per `mode='verb'` entry.
- **`noun_phrases`** — definite + chosen-variant × singular/plural per
  `mode='noun'` entry.
- **`cards`** — final Anki rows, both directions, with stable GUIDs and
  sort order. The single source of truth for the deck list.
- **`ai_cache`** — content-addressed cache of OpenRouter responses.
  Re-runs reuse cached responses for free.

Media files are content-addressed by `md5(text)` so they're shared across
cards and survive DB rebuilds.

---

## Auxiliary tools

Pure AnkiConnect utilities — they do not depend on the DB schema. Outputs
go to the system temp dir; the script prints the path when done.

| Wrapper                | Script                            | What it does                                  |
|------------------------|-----------------------------------|-----------------------------------------------|
| `./scripts/leech.sh`   | `scripts/01_leech_cards.py`       | Suspend cards failed ≥N times in a row.       |
| `./scripts/learnt.sh`  | `scripts/02_learnt_words.py`      | Export learnt-Italian-words report.           |
| `./scripts/sentence.sh`| `scripts/03_sentence_practice.py` | Generate Italian sentences for active recall. |

---

## Prerequisites

- Python 3.11+
- `ffmpeg` on PATH (audio compression)
- [Anki](https://apps.ankiweb.net/) + [AnkiConnect](https://ankiweb.net/shared/info/2055492159) for the `sync` step
- API keys (one per line):
  - `.openrouter` — OpenRouter; uses `google/gemini-flash-latest` for enrichment and `sourceful/riverflow-v2-fast` for images.
  - `.elevenlabs` — ElevenLabs for TTS.
