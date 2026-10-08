# Italian Flashcards

A personal pipeline that turns Italian word lists into Anki decks. Every card
gets an AI-checked English side, consistent labels and grammar details,
ElevenLabs audio, an AI illustration, an optional fun fact (where the word
comes from, how it relates to English…), and a GUID that never changes, so
review history survives every rebuild.

```
sources.json ─▶ ingest (AI enrichment) ─▶ SQLite ─▶ cards + fun facts ─▶ audio / images ─▶ .apkg ─▶ Anki
```

> **Next: movie decks.** Load a film's Italian subtitles, build a word dataset,
> and get a deck of the words you don't know yet. The plan, and what's still
> open, is in [`docs/RECOMMENDATIONS.md`](docs/RECOMMENDATIONS.md).

---

## Quick start

```sh
claude                             # log in to Claude Code once (all text AI runs on your Claude plan)
echo "sk-or-..." > .openrouter     # OpenRouter key (images only)
echo "sk_..."   > .elevenlabs      # ElevenLabs key (audio)

./run.sh                           # build → audio → images → compress → export → sync
./run.sh --no-sync                 # everything except touching Anki
./fc.sh <command>                  # any single command, e.g. ./fc.sh practice
```

Both scripts create `.venv` on first run and install the project from
`pyproject.toml` (editable). They reinstall only when that file changes.
Inside the venv the CLI is also available as `flashcards <command>`.

**Prerequisites:** Python 3.11+, [Claude Code](https://code.claude.com) logged in
to your Claude subscription, `ffmpeg` on `PATH` (audio compression), and
[Anki](https://apps.ankiweb.net/) with
[AnkiConnect](https://ankiweb.net/shared/info/2055492159) for `sync` and the
study tools.

---

## Configuration

| File | Holds |
|---|---|
| `sources.json` | What to build: every input, its mode and its deck(s). |
| `settings.toml` | How to build it: Claude model and effort (default + per task), outage retry interval, voice, image model, compression, worker counts and per-run limits, sync safety limits, backups, fun-fact threshold and batch size, practice / audit / leech defaults. Unknown keys are an error. |
| `.openrouter`, `.elevenlabs` | API keys (gitignored). OpenRouter is used for image generation only. |

### `sources.json`

```jsonc
{
  "sources": [
    { "path": "italian_interjections.csv", "mode": "gloss",
      "deck": "Italian - Interjections", "front_pill": "type: interjection",
      "shuffle_window": 0, "prompt_hint": "Italian exclamations, greetings…" },
    { "path": "italki/italki_verbs.csv", "mode": "verb",
      "deck": "Italian - Italki Verbs",
      "infinitive_deck": "Italian - Italki Verbs Infinitive",
      "label_pill": "source: italki" },
    { "path": "freqdic/subtlex-it.cleaned.csv", "mode": "subtlex",
      "deck": "Italian - Verbs", "infinitive_deck": "Italian - Verbs Infinitive",
      "noun_deck": "Italian - Nouns", "phrases_deck": "Italian - Noun Phrases",
      "verb_limit": 400, "noun_limit": 1000, "shuffle_window": 50 }
  ]
}
```

| Field | Default | Purpose |
|---|---|---|
| `path` | required | CSV relative to `inputs/` (header `italian,english`). |
| `mode` | `gloss` | `gloss` \| `avere` \| `verb` \| `noun` \| `subtlex` |
| `deck` | `Italian - <Stem>` | Main deck; for verbs, the prefix of the tense decks. |
| `enrich` | `true` | `gloss`/`avere`: let the AI write the English side. |
| `audio` / `image` | `true` | Generate audio / an illustration. |
| `facts` | `true` | Show fun facts on this source's cards. |
| `front_pill` | `type: <stem>` | The type pill on every card. |
| `label_pill` | — | Extra pill on every card, e.g. `source: italki`. |
| `shuffle_window` | `50` | Sliding-window shuffle width; `0` keeps CSV order. |
| `prompt_hint` | — | Context sent with every AI request for this source. |
| `infinitive_deck` | — | Required for `verb` / `subtlex`. |
| `phrases_deck` | — | Required for `noun` / `subtlex`. |
| `noun_deck` | — | Required for `subtlex`. |
| `verb_limit` / `noun_limit` / `limit` | 400 / 1000 / — | `subtlex` caps (`limit` splits 1:2). |
| `cards_per_expression` | `2` | `avere`: persons per expression (1–6). |
| `disabled` | `false` | Skip the source. |

`./fc.sh discover` validates the manifest: unknown keys, missing files,
missing deck fields, and deck names that clash (case-insensitively, across
every expanded tense / phrase deck) or that Anki would reject.

---

## How it works

### Package map

```
flashcards/
  cli.py          every command; defaults come from settings.toml
  settings.py     settings.toml loader (typed defaults)
  sources.py      sources.json loader + validation
  ai.py           THE AI client: one HTTP path, retries, cache, JSON-schema output
  tasks.py        every AI task: name, instruction, rules, schema
  cards.py        card presentation: labels, details, fun-fact block, note template + CSS
  facts.py        fun facts (word_facts)
  db.py           schema + numbered migrations
  backup.py       DB snapshots
  anki.py         AnkiConnect client, pipeline notetype lookup, learnt words
  modes/
    base.py       Mode — the shared ingest / retire / card flow
    gloss.py avere.py verb.py noun.py subtlex.py
  commands/
    build.py media.py export.py sync.py          the pipeline
    practice.py learnt.py leech.py               study tools
    audit.py review.py                           quality
```

### Modes

Every mode subclasses `modes/base.py:Mode`, which runs the same flow for all
of them: collapse duplicate rows, match existing rows without AI, send new
rows (and rows whose CSV gloss changed) through the mode's enrichment task,
store the entry, record which entries are still in the input, then generate
children and cards. A mode only declares what is specific to it:

| mode | Enrichment task | Children | Cards |
|---|---|---|---|
| `gloss` | `gloss` (or the CSV gloss if `enrich: false`) | — | one pair per row |
| `avere` | `avere_gloss` | — | the expression conjugated for N persons |
| `verb` | `verb_meta` (lemma, auxiliary, participle, reflexive) | 46 forms across 8 tenses (`verb_forms`) | one per form in `<deck> <Tense>`, plus the infinitive |
| `noun` | `noun_meta` (gender, articles, plural) | definite phrases + one extra family (`noun_phrases`) | definite → `deck`, others → `phrases_deck` |
| `subtlex` | — | — | top-N SUBTLEX verbs and nouns, run through `verb` and `noun` |

### AI

All text / JSON AI goes through `ai.py`; all tasks are defined in `tasks.py`:

`gloss` · `avere_gloss` · `verb_meta` · `verb_forms` · `noun_meta` ·
`noun_phrases` · `word_facts` · `image_prompt` · `card_audit` ·
`practice_sentence` · `practice_feedback`

**Claude.** Every text / JSON task runs through `claude -p` on your Claude
subscription — no API key, usage counts against your plan. Each call uses
the task's system prompt, no tools, `--json-schema` for structured output,
and `[ai] model` / `effort` (default `opus` / `medium`; practice uses
`sonnet` via `[ai.task_models]`). At most `[ai] concurrency` calls (default
4) run at once. **Image generation is the only thing that uses OpenRouter**
(`[images] model`), because Claude doesn't make images.

**Outages wait instead of failing.** If Claude can't serve requests —
plan usage limit reached, logged out, out of credits, network down — all AI
work pauses, one request retries every `[ai] unavailable_retry_minutes`
(default 5), and the run continues by itself when it succeeds. Errors
specific to one request (invalid output, a timeout) only fail that item; it
is retried on the next build. Ctrl-C stops a waiting run immediately.

**One prompt shape.** A shared system preamble, then *Task → Context → Rules
→ Input (JSON)*, with strict JSON-schema output (practice feedback is plain
text). Enrichment tasks all return the same core fields (`english`,
`disambiguation`, `usage_note`, `confidence`, `valid`), so the English side
reads the same in every deck: `english (disambiguation) [usage]`.

**Cache.** Answers are stored in `ai_cache` (keyed by model + task +
messages), so re-runs cost nothing. Practice is never cached.

### Cards

Every card is built by `cards.py`, so all decks look the same:

- **Front:** illustration (if any) → label pills → prompt → audio (on the
  Italian side).
- **Back:** the answer → a muted **details** line → an optional **fun fact**
  → audio.

| Card | Pills | Details line |
|---|---|---|
| gloss | `type` (+ source) | — |
| avere | `type`, `subject` | the base expression (`avere fame`) |
| verb form | `type: verb`, `tense`, `subject` | infinitive |
| verb infinitive | `type: verb`, `tense: infinitive` | auxiliary · past participle · reflexive |
| noun phrase | `type: noun`, `phrase`, `preposition`, `number` | gender · base form (`la casa`) |

Pills are generated in code in a fixed order (type, tense, subject, phrase,
preposition, number, source); the AI never writes labels. All text is
HTML-escaped. On every sync the pipeline notetype's template and CSS in
Anki are set to the ones in `cards.py`, light and dark mode included.

### Fun facts

After cards are built, `build` collects each card's fact word and asks the
`word_facts` task about any word not yet in `word_facts`, 25 words per
request (`[facts] batch_size`). The model is told
to be selective (most words get none), to stick to well-attested
etymology, links to English, false friends or cultural notes, and to decline
when unsure. Example of the target style:

> **rubinetto** — From French *robinet*, from *Robin*, a traditional name for
> a sheep: early taps were often shaped like a ram's head.

- One answer per word, shared by every deck. The leading article is
  ignored, so "la finestra" and "finestra" share one fact.
- Shown on base cards only: gloss and avere cards, verb infinitives, and
  noun definite singulars. Conjugated forms and phrase variants don't
  repeat it.
- Hidden if the model's confidence is below `[facts] min_confidence`
  (default 0.75). Hidden facts are listed by `review`.
- `audit` fails a card whose fact is doubtful.
- Turn facts off per source with `"facts": false`, or globally with
  `[facts] enabled = false`.

### Pipeline stages

| Stage | Command | What it does |
|---|---|---|
| validate | `discover` | Manifest checks (see above). |
| ingest | `build` | Each mode enriches new/edited rows and generates children. |
| retire | `build` | Entries whose row left the input are marked `retired`; rows that come back are restored (same entry, same notes). Skipped for a source if any AI call failed. |
| materialise | `build` | Modes emit `Card`s from live entries. |
| facts | `build` | Fun facts for new words. |
| write + sort | `build` | Two rows per card into `cards`; per-deck order by SUBTLEX frequency then input order, with a sliding-window shuffle seeded per deck (stable across builds). |
| audio | `audio` | One ElevenLabs MP3 per distinct Italian text: `media/audio/<md5>.mp3`. |
| images | `images` | `image_prompt` task → image model → `media/images/<md5>.png`. |
| compress | `compress` | 512 px JPEG / 48 kbps mono MP3 for packaging (atomic writes). |
| export | `export` | One `.apkg` per deck, each media file once. |
| sync | `sync` | Import → enforce template/CSS → delete orphans (safely) → reorder new cards. |

### Database (`database.sqlite`)

| Table | Holds |
|---|---|
| `entries` | One row per item, unique on `(source_path, mode, natural_id)`; `source_path` is relative to `inputs/`. Verb/noun/frequency columns, `retired`, `input_english` (the CSV gloss it was built from), `confidence`. |
| `verb_forms` | Conjugated forms; `card_key` is the forms' persisted card identity. |
| `noun_phrases` | Phrase variants; `card_key` likewise. |
| `cards` | Rebuilt every build: deck, both sides, labels, details (`back_text`), `fact`, audio/image text, `sort_order`, `guid`. |
| `word_facts` | One fun-fact answer per word. |
| `audits` | Latest audit verdict per card, with a hash of the content audited. |
| `ai_cache` | Every AI answer. |

Schema changes are numbered migrations in `db.py` (`PRAGMA user_version`),
applied automatically by the next `build`, with a backup taken first.

**Backups:** `run` (and every migration) snapshots the DB into `backups/`
with `VACUUM INTO`, skips identical snapshots, keeps the newest
`[backups] keep_last` (default 10), and can mirror each one to
`[backups] mirror_dir`. Files you put elsewhere in `backups/` are not touched.

### Card identity

The GUID of each note is `genanki.guid_for(natural_key, direction)`:

| Card | `natural_key` |
|---|---|
| gloss / avere | `gloss:<entry id>` / `avere:<entry id>:<person>` |
| verb infinitive | `verb_infinitive:<entry id>` |
| verb form / noun phrase | the row's `card_key`: semantic for new rows (`verb_form:<entry>:<tense>:<person>:positive`), `verb_form:<rowid>` for rows from before v1 |

IDs are stored once and never re-derived, so moving or renaming the repo
changes nothing. The legacy keys exist only in `database.sqlite`, which is
why it's the one file you can't regenerate.

### Input lifecycle

- **Remove a CSV row** → its entry is retired and its cards leave the deck;
  `sync` deletes the notes if they were never studied. Put it back and the
  same notes return.
- **Edit the English column** → picked up on the next build (`enrich: false`
  uses your text; `enrich: true` re-asks the AI with it as the hint).
- **Re-ask the AI for a whole source** → `./fc.sh build --refresh <source>`.
- `build --source X` rebuilds only `X`'s cards.

---

## Sync safety

`sync` can delete notes from Anki. Safeguards:

1. **Scope:** only notes on the pipeline's notetype (matched by its exact
   fields) **and** in decks the DB manages. Notes in other decks are never
   touched, whatever their notetype.
2. **Study history is never deleted by default.** An orphan with any
   history (not new, or any review-log entry) is kept unless you pass
   `--delete-reviewed-orphans`.
3. Deletion refuses beyond `[sync]` limits (default > 200 notes or > 10 % of
   a deck) unless you pass `--allow-orphan-delete`.
4. If most notes still have a legacy (integer) `SortKey` after import, the
   import didn't update notes and nothing is deleted.
5. `run` skips sync if any source failed to ingest. Sync aborts on an empty
   `cards` table. Packages for decks no longer in the DB are skipped.

Sync also reports notes sitting in a different deck than the DB expects
(Anki doesn't move existing cards on import). After a big change, preview
first: `./fc.sh sync --dry-run`.

---

## CLI reference

```sh
# pipeline
./fc.sh discover
./fc.sh build    [--source ID] [--refresh ID] [--skip-ai] [--workers N]
./fc.sh audio    [--deck NAME] [--limit N] [--per-deck N] [--workers N]
./fc.sh images   [--limit N] [--workers N]
./fc.sh compress
./fc.sh export
./fc.sh sync     [--dry-run] [--allow-orphan-delete] [--delete-reviewed-orphans]
./fc.sh run      [--no-sync] [--refresh ID] [--audio-limit N] [--image-limit N] [--audio-deck NAME] …

# study tools (need Anki running)
./fc.sh practice [--style STYLE …] [--length short|medium|long] [--subjunctive] [--deck NAME] [--list-styles]
./fc.sh learnt   [--deck NAME] [--output FILE]
./fc.sh leech    [--deck NAME] [--threshold N] [--update-config] [--dry-run]

# quality
./fc.sh audit    [--deck NAME] [--limit N] [--only-problems]
./fc.sh review
```

- **practice**: samples your learnt words, asks for an English sentence
  (with optional grammar constraints), checks your Italian, and gives
  bullet-point feedback. The subjunctive is off by default
  (`[practice] no_subjunctive`).
- **learnt**: a table per deck of every graduated, unsuspended word.
- **leech**: suspends and tags cards with N consecutive "Again" presses,
  including cards stuck in learning, which Anki's own leech check misses.
- **audit**: AI review of every card, including fact accuracy. Writes a CSV
  to `audit_reports/` and saves verdicts in `audits`.
- **review**: one list of low-confidence entries, warn/fail audits that
  still match the card, and hidden facts.

---

## External services

| Purpose | Default | Setting |
|---|---|---|
| All text AI (enrichment, facts, image prompts, audits) | Claude Opus via Claude Code on your subscription | `[ai] model`, `effort` |
| Practice | Claude Sonnet | `[ai.task_models] practice_*` |
| Images | OpenRouter `sourceful/riverflow-v2-fast` | `[images] model` |
| Audio | ElevenLabs `eleven_multilingual_v2`, voice `HuK8QKF35exsCh2e7fLT` | `[audio]` |
| Frequency data | [SUBTLEX-IT](https://osf.io/zg7sc/), AI-cleaned | `inputs/freqdic/` |
