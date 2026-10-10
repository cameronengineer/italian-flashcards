# Italian Flashcards

A personal pipeline that turns what you want to learn — CILS levels, class
notes, frequency lists, **the subtitles of a film you're about to watch** —
into Anki cards. It is built word-first: every Italian root word exists
once, gets its grammar from Wiktionary, its card text and a fun fact from
Claude (which also checks the dictionary data), an image from Codex, and
lands in Anki in the order your study plan says you need it.

Everything runs on subscriptions — Claude (`claude -p`) and ChatGPT/Codex
(`codex exec`) — with no pay-as-you-go APIs. When a usage limit is hit the
run waits and carries on by itself.

---

## Using it

One command does everything, and it is safe to run as often as you like:

```sh
./run.sh
```

Unchanged lists aren't re-read, valid finished AI work is reused, and Anki only
receives the differences. Repairs can revalidate an incomplete or unsafe result;
they preserve its existing media and study history. Stop it any time
with Ctrl-C; the next `./run.sh` carries on where it stopped.

A run has six segments, each with one line per thing it does:

```
════════════════════════════════════════════════════════════════
  Italian flashcards · Sat 10 Oct, 09:12
════════════════════════════════════════════════════════════════

[1/6] Prepare
      settings     lists.toml + plan.toml OK (9 lists)
      dictionary   ready
      backup       database.backup.20261010_091209_c1319ee7.sqlite
      lists        unchanged · 3,597 words in 9 lists

[2/6] Cards
      cards        7,280 ready to study · 4,997 waiting
      waiting      4,499 for Claude · 665 for your review · 401 for an image

[3/6] Anki
      09:12  syncing what's ready …
      09:12  +231 new · 6 updated · 357 old cards replaced  (14s)

[4/6] Claude · writing cards
      to write     1,720 words · 249 phrases · 31 verbs · 3 hints
      images       off ([run] image_limit = 0)
      09:12  asking Claude: 3× words (45), 1× verbs (3) …
      09:13  words   ✓ 15  questo, quello, molto +12            52s · 1,705 left
      09:14  ⏸  Claude usage limit reached (resets 3pm) — waiting, next try 09:19.
      ...
      10:02  ── round 1: 300 items written · Anki: +240 new · 2 updated  (9s)

[5/6] Audio & media
      audio        1 new recording · 208 cards still without audio
      compress     nothing new

[6/6] Anki · final sync
      13:40  up to date

════════════════════════════════════════════════════════════════
  Finished in 4h 28m
      Anki         +880 new cards · 31 updated · 1,020 old cards replaced
      Claude       1,649 words · 187 phrases · 91 verbs written
      Movie        Le otto montagne (2022): you know 65% of its words · 185 more → 90%
      Needs you    327 words Claude disputed are holding back 661 cards —
                   open review.csv, put "yes" in the approve column, run again
      Images       off ([run] image_limit = 0) · 401 cards wait for one
════════════════════════════════════════════════════════════════
```

**What needs you** is always in the summary:

- **review.csv** lists the words Claude disputed while checking the
  dictionary data (wrong gender, part of speech, a typo in your list…), with
  its reason. Put `yes` in the `approve` column for the ones that are fine and
  run again; fix the others in the input list. Approvals apply only to the exported
  content version. Changed or old unversioned rows require review again; the
  previous mark is recorded in `review_status`. Late approvals enter the final sync.
- **cue_review.csv** lists ambiguous written prompts, including their image references.
  Add `new_hint` or `accepted_alternatives`, explain the distinction in `reason`,
  and set `apply` to `yes`. Corrections persist separately from generated text;
  changed content requires review again.
- **grammar_review.csv** reports published/planned drills with unsuitable forms or
  unresolved plural choices. Correct the sense's `features` through a human override.
  Unsuitable published drills are suspended; their notes, review history and media stay.
- Anki must be open (with [AnkiConnect](https://ankiweb.net/shared/info/2055492159))
  for the sync segments; otherwise they're skipped and the next run catches up.

Options, all optional:

| | |
|---|---|
| `--images N` / `--audio N` | new Codex images / ElevenLabs recordings this run (defaults: `[run] image_limit`, `audio_limit`; `-1` = no limit) |
| `--batches N` | Claude batches between Anki syncs (default 20) |
| `--once` | stop after one round |
| `--no-ai`, `--no-sync` | skip Claude/Codex/audio, or skip Anki |
| `--keep-old` | keep unstudied old cards even when their replacement is in Anki |

Exit code 0 = everything done, 1 = something failed (details in
`audit_reports/run_latest.json`), 2 = work left for the next run.

### Extras

`./run.sh help` lists them; run one with `./run.sh <name>`:

| | |
|---|---|
| `movie` | coverage table for a film + `datasets/<film>.csv` (lemma, count, first appearance, forms heard, example line) |
| `practice` | translation practice from words you've learnt; your mistakes become cards |
| `learnt` | export every word you've graduated |
| `leech` | suspend + tag cards you keep failing; `leech --doctor` writes memory aids |
| `audit` | Claude reviews conjugations, ambiguous cues and gender-changing plurals first; matching audits are counted separately from publishable notes |
| `share` | `.apkg` for someone else (only lists marked `shareable`; film lines left out) |
| `doctor` / `data-review` | health report / data-quality census (read-only) |
| `media-inventory` | resumable checksums, image validation, references and provenance; originals and unreferenced assets are retained |
| `recovery create PATH` | verified database, dictionary, code, runtime versions and inputs; add `--include-media` and optionally `--anki-collection PATH` |

### First time

```sh
claude                        # log in to Claude Code (Claude subscription)
codex login                   # log in to Codex with ChatGPT (only needed for images)
echo "sk_..." > .elevenlabs   # ElevenLabs key (audio)
./run.sh                      # creates .venv, downloads the dictionary once (~10 min)
```

Prerequisites: Python 3.11+, Node (for Codex), `ffmpeg`, Anki with AnkiConnect.

---

## Configuration

| File | Holds |
|---|---|
| `lists.toml` | **What** to learn: every list (kind, input file, deck, type pill, `recognition_only`, `facts`, plus `optional`, `shareable`, `image_policy`, `max_drop_ratio`, `allow_empty`). Unknown keys are errors. |
| `plan.toml` | **When**: list priorities (a film with a watch date first), new cards per day, optional `horizon_days`, which tenses get full conjugation cards, which extra card types are on. |
| `settings.toml` | **How**: Claude model / effort / concurrency, outage waiting, **how many new images and audio files each run creates** (`[run] image_limit`, `audio_limit`: 0 = none, -1 = no limit), whether images are required, audio voice, backups, Anki profile. Values are type-checked. |
| `lexicon/*.jsonl` | The generated lexicon, in git — review AI output as diffs; edit a field and mark it `human` to keep your version forever (stop the run first). |
| `review.csv` | Words held for your review (written by every run, read by the next). |

**Adding or removing a list:** edit `lists.toml` (and its priority in
`plan.toml`) and run. A removed list's cards leave Anki on that run, except
anything you've studied, which always stays.

### List kinds

| kind | Input |
|---|---|
| `csv` | `italian,english` CSV (words, expressions, sentences, sentence stems) |
| `cils` | CILS lists — uses the lemma (`italian_original`) and part of speech (`function`) columns |
| `verbs` | CSV of infinitives |
| `avere` | avere-expressions |
| `subtlex` | top-N verbs / nouns from SUBTLEX-IT |
| `numbers` | ordered `english,italian` CSV in `inputs/italian_numbers.csv`: difficult teens and contrasts first; [study order and spelling notes](inputs/italian_numbers.README.md) |
| `movie` | Italian subtitles: `inputs/movies/<slug>/subs.it.srt` (gitignored) |

---

## How it works

### 1 · Lexicon — one record per root word (`lexicon.py`)

Each list row is resolved to a **lexeme** (lemma + part of speech) through
the Kaikki index of English Wiktionary (`kaikki.py`): gender, plural,
adjective forms, full conjugation tables (with irregular verbs flagged),
auxiliary, participles, IPA, Wikimedia Commons recordings and etymology.
Rules (`italian.py`) do the rest: standard spelling, articles,
articulated prepositions, possessives, compound tenses, numbers, cognate
detection (*-zione = -tion*). A word in several lists is **one** lexeme.
This step is skipped when no list, input file, dictionary or resolver code
has changed since the last run.

### 2 · AI queue — verify, don't recall (`queue.py`, `tasks.py`)

Durable jobs in study order (the film's words first):

| Job | What Claude / Codex does |
|---|---|
| words (`lexeme_enrich`) | Given the Wiktionary senses, the lists' own glosses and the film line: choose the sense, write the card prompt + hint + register + note, **check the dictionary data** (disagreements → review.csv), write a fun fact from the sourced etymology — plus one hop further down, fetched from kaikki.org just before the word is enriched (*rubinetto* ← French *robinet* ← *Robin*, "sheep"). 15 words per call. |
| phrases | English prompt + hint for sentences and stems. |
| verbs | English prompts for the Wiktionary conjugations (forms that don't exist, like the imperative of *dovere*, are left out). |
| hints | Hints + "also accepted" when several words share a prompt (*dunque / quindi / perciò*). |
| images | Codex draws one flat, text-free illustration per root; every card of that root uses it. Existing images are reused, never deleted. |

Up to `[ai] concurrency` (4) Claude batches run at once. Usage limits,
used-up credits, a logged-out CLI or an outage pause all AI work: the run
retries every 5 minutes (or sleeps until the reset time it's told), says so
once and then every 30 minutes, and carries on by itself. Answers are
schema- and ID-checked before they're applied; a batch is applied all or
nothing. Jobs remember a fingerprint of their inputs, so finished work never
runs twice unless its inputs change. A failed job is retried one at a time,
and again at the start of the next run. Human edits always win.

By default every root is worked on, in study order. Set `horizon_days` in
`plan.toml` to prepare and publish that many days of new card directions. One
shared plan counts recognition, production and derived drills, subtracts directions
already published, and selects their generation dependencies. Enrichment can add
new candidates, which are budgeted on the next pass. Existing verb families are
maintained independently of which new words have highest priority.

### 3 · Notes (`notes.py`)

One notetype, **Italian Flashcard v4**, with two templates switched on per
note: *Recognition* (Italian → English) and *Production* (English → Italian).
Every card has its root's image. A note goes to Anki only when its content
and image exist, its root isn't held for review, and it hasn't failed an
audit (`quality.py` — one rule for sync, audit and share).
`[images] required = false` or a list's `image_policy` lets notes go
without waiting for their image.

| Card type | Deck |
|---|---|
| vocab — word, gender · plural or the four adjective forms, IPA, film line, note, fun fact | the root's highest-priority list (`Italian::Movies::…`, `Italian::CILS::A1`, …) |
| phrase — sentences, stems, expressions | same |
| form — conjugations for model verbs (*parlare, credere, dormire, finire*), irregular verbs and the most frequent N; compound tenses built by rule | `Italian::Verbs::<Tense>` |
| nphrase — rule-built article / preposition / possessive drills | `Italian::Noun Phrases` |
| cognate — one card per suffix rule with its examples | `Italian::Cognates` |
| mistake — practice errors | `Italian::Mistakes` |

### 4 · Anki (`reconcile.py`)

Targeted AnkiConnect calls — no `.apkg` imports. Anki is read once per sync;
the ~60k notes from before v4 are indexed once a week, and each sync only
reads the ones you've studied.

- refuses before writing anything if the wrong Anki profile is open, a v4
  note it doesn't own is found, or many notes it put in Anki have vanished;
- adds ready notes, updates changed ones, moves them when their home deck
  changes; your own tags (and `leech`) are kept, pipeline notes carry
  `fc::owned`;
- **adopts studied notes from before v4**: updated in place, moved to the v4
  deck, and the matching direction of the new note switched off;
- removes an unstudied old note once its replacement is confirmed in Anki —
  the new card, or a studied old card adopted for the same thing (an unstudied
  duplicate). Every note is re-read just before deletion. `--keep-old` turns
  this off;
- **never deletes a studied note** and never touches notes outside the
  pipeline's decks; never deletes media;
- puts new cards in study-plan order (only the ones that are out of place);
- reads what you know (interval ≥ 21 days) back into `card_knowledge`, which
  drives study order and movie coverage.

### 5 · Audio (`commands/media.py`)

ElevenLabs audio for ready notes, in study order, up to `[run] audio_limit`
new files per run. The spoken text drops display variants
(*sono andato/a* → "sono andato").

---

## Movies

1. Put Italian subtitles in `inputs/movies/<slug>/subs.it.srt` and add a
   `kind = "movie"` list (see the *Le otto montagne* entry).
2. Give it a watch date in `plan.toml`; its words come first.
3. Every run's summary estimates vocabulary coverage; `./run.sh movie`
   gives the full coverage table and `datasets/<list>.csv` (gitignored).

Subtitles are parsed (ads, tags and hearing-impaired brackets dropped),
lemmatised with SUBTLEX + Wiktionary forms (+ clitic stripping), names are
dropped, and every root gets its count, first appearance and an example
line, which appears on the card. Movie cards are recognition-only by default.
Forecasts show additional roots, currently planned new card directions and minimum
introduction days at your daily limit. They do not estimate comprehension or the
time needed to learn and retain the material.

---

## Data and safety

| Table | Holds |
|---|---|
| `lexemes`, `senses` | root words and their card text (senses keep stable ids; replaced ones are archived) |
| `overrides` | your edits — always applied over AI and dictionary data |
| `lists`, `list_items`, `source_observations` | list membership, rank, source glosses, film context |
| `ai_jobs`, `ai_cache` | the queue; every AI answer |
| `form_prompts`, `word_facts`, `mnemonics`, `mistakes` | AI output |
| `v4_notes` | desired Anki notes |
| `card_knowledge` | what you know, per card and direction (from Anki) |
| `identity`, `adoptions`, `legacy_notes`, `sync_runs` | notes sent to Anki, adopted old notes, the weekly index of old notes, sync history |
| `review_decisions`, `cue_overrides`, `form_exclusions`, `asset_manifest`, `metadata` | versioned approvals/cue corrections, reasoned verb exclusions, media checksums, checkpoints |
| `entries`, `verb_forms`, `noun_phrases`, `cards` | v3 data, kept to adopt your studied notes |

Every run starts with a database backup (`backups/` keeps the newest 10);
migrations are backed up too. Before the first change to Anki, each run also
backs up the whole Anki collection — every deck, note, card and review, without
media — to `backups/anki/` (newest 5; restore by quitting Anki and putting the
unzipped `collection.anki2` back in your profile folder). One run at a time (`database.sqlite.lock`).
Media is never deleted or overwritten. Raw subtitles and movie datasets never
go to git.

Sense identities now cite dictionary/source evidence. Legacy senses keep their
keys and are explicitly labelled unverified until matched. For a reviewed
paraphrase of the same meaning, retain `idx` and add `same_meaning: true` to the
human sense override; for a new meaning, omit `idx` and `source_id`. Old meanings
are archived, and a new meaning's derived drills receive separate identities.

Recovery levels differ: ordinary database backups contain pipeline state; a
`recovery create PATH --include-media` bundle also includes the dictionary,
source code, inputs, all included media, and recorded Python/package versions.
Adding `--anki-collection /path/to/collection.anki2` includes a consistent Anki
collection snapshot, review history and the sibling `collection.media` directory.
`recovery verify PATH` checks hashes. `recovery restore PATH --destination NEW`
restores to a new directory and verifies the copied bytes. Python, external tools
and account logins still need to be installed/configured; package versions are in
`requirements-recovery.txt`. Anki recovery requires restoring the supplied
collection and its media into an appropriate profile while Anki is closed.

`./run.sh media-inventory` resumes unchanged checks by size/mtime;
`--rehash` verifies every file's bytes again. Audio is checksum-verified, not
fully decoded. The inventory never removes or repairs originals.

Implementation and validation: [10 October follow-up](docs/RECOMMENDATIONS_AFTER_CLAUDE_REVIEW_2026-10-10.md).

Tests: `.venv/bin/python -m unittest discover -s tests -t .`

Dictionary data: [Wiktionary](https://en.wiktionary.org) via
[kaikki.org](https://kaikki.org), CC BY-SA 4.0 (see `lexicon/README.md`).
Frequency data: [SUBTLEX-IT](https://osf.io/zg7sc/).
