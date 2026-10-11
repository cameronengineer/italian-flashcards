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
      waiting      5,164 for Claude · 401 for an image

[3/6] Anki
      09:12  syncing what's ready …
      09:12  +231 new · 6 updated · 357 old cards replaced  (14s)

[4/6] Claude · writing cards
      to write     1,720 words · 249 phrases · 31 verbs · 3 cue groups
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
      Flagged      Claude doubted the dictionary data for 327 words (published anyway)
      Images       off ([run] image_limit = 0) · 401 cards wait for one
════════════════════════════════════════════════════════════════
```

**Nothing waits for you.** There is no human review step: when Claude doubts
the dictionary data for a word (wrong gender, part of speech, a typo in your
list…) the word is published anyway and the doubt is kept for information —
`./run.sh review` lists them. Ambiguous prompts get hints from Claude
automatically, and drills that don't fit a meaning are simply not published.

- Anki must be open (with [AnkiConnect](https://ankiweb.net/shared/info/2055492159))
  for the sync segments; otherwise they're skipped and the next run catches up.

Options, all optional:

| | |
|---|---|
| `--images N` / `--audio N` | new Codex images / ElevenLabs recordings this run (defaults: `[run] image_limit`, `audio_limit`; `-1` = no limit) |
| `--batches N` | Claude batches between Anki syncs (default 20) |
| `--once` | stop after one round |
| `--no-ai`, `--no-sync` | skip Claude/Codex/audio, or skip Anki |
| `--keep-old` | keep obsolete notes instead of deleting them (studied or unstudied) |

Each run keeps its summary in `audit_reports/runs/<run-id>.json`, with batch
timings and full failure diagnostics in `<run-id>.events.jsonl` when batches run.
`audit_reports/run_latest.json` is also updated. These logs stay local and are
ignored by Git. Failed **attempts** include work subsequently recovered; the
backlog separately counts runnable, cooling-down and exhausted jobs. Provider
availability waits are separated from other elapsed time.

Exit code 0 = enabled work completed, 1 = an unresolved failure, 2 = work left.
Images disabled by your settings and independent audits can still be outstanding. The ready count is **notes**; a note can generate both recognition
and production cards. Suspension counts are cards newly suspended this run.

### Rebuilding everything with a stronger model

In `settings.toml` set `model = "opus"`, `thinking = true` and bump
`task_version` (e.g. `"5"` → `"6"`), then keep running `./run.sh`. Everything
Claude wrote is redone in study order — words, phrases, verb forms, hints and
word-for-word film lines (hints follow once their words are redone). Anki
cards are **updated in place**, keeping your review history; cards keep their
old text until the new text arrives. Your human edits, images and audio are
kept. Changing only `model` affects work still to do.

The old `review.csv`, `cue_review.csv` and `grammar_review.csv` files are no
longer needed or generated. They used to request word approvals, ambiguous-cue
corrections and grammar checks respectively. `./run.sh review` now lists AI
flags and ignored drills for information only. Previously saved corrections
remain in the database and still apply when their content version matches.

Every completed run writes one `.apkg` per ready deck into `decks/`, including
its images, audio and film examples. Unchanged packages are reused. Changed
media, templates, ordering or content rebuild the affected package. Packages
for obsolete decks move to `decks/archive/`; media is never deleted. These
personal exports are Git-ignored and contain private film examples; use the
`share` extra for exports with the sharing filters. Direct Anki sync remains
the way to update your own collection.

### Extras

`./run.sh help` lists them; run one with `./run.sh <name>`:

| | |
|---|---|
| `movie` | coverage table for a film (its word list is kept in `inputs/movies/<film>/words.csv`) |
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
| `plan.toml` | **When**: list priorities (a film with a watch date first), new cards per day, optional `horizon_days`, conjugation generation and publication settings, which extra card types are on. |
| `settings.toml` | **How**: Claude model / effort / concurrency, outage waiting, **how many new images and audio files each run creates** (`[run] image_limit`, `audio_limit`: 0 = none, -1 = no limit), whether images are required, audio voice, backups, Anki profile. Values are type-checked. |
| `lexicon/*.jsonl` | The generated lexicon, in git — review AI output as diffs; edit a field and mark it `human` to keep your version forever (stop the run first). |
| `decks/` | One `.apkg` per deck, rewritten by every run when that deck changes — a portable copy of your cards (for another device or person; importing it into the same Anki profile would duplicate notes). |

**Adding or removing a list:** edit `lists.toml` (and its priority in
`plan.toml`) and run. Obsolete cards from removed lists leave Anki on that
run, including studied cards and their review history. Generated data and media stay.

### List kinds

| kind | Input |
|---|---|
| `csv` | `italian,english` CSV (words, expressions, sentences, sentence stems) |
| `cils` | CILS lists — uses the lemma (`italian_original`) and part of speech (`function`) columns |
| `verbs` | CSV of infinitives |
| `avere` | avere-expressions |
| `subtlex` | top-N verbs / nouns from SUBTLEX-IT |
| `numbers` | ordered `english,italian` CSV in `inputs/italian_numbers.csv`: difficult teens and contrasts first; [study order and spelling notes](inputs/italian_numbers.README.md) |
| `movie` | Italian subtitles: `inputs/movies/<slug>/subs.it.srt`; every run writes `words.csv` next to it |

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
| words (`lexeme_enrich`) | Given the Wiktionary senses, the lists' own glosses and the film line: choose the sense, write the card prompt + hint + register + note, **check the dictionary data** (doubts are recorded, never a hold), write a fun fact from the sourced etymology — plus one hop further down, fetched from kaikki.org just before the word is enriched (*rubinetto* ← French *robinet* ← *Robin*, "sheep"). 15 words per call. |
| phrases | English prompt + hint for sentences and stems. |
| verbs | English prompts for the Wiktionary conjugations (forms that don't exist, like the imperative of *dovere*, are left out). |
| cue groups (`disambiguate`) | Compare words sharing an English prompt, adding distinguishing hints. Alternatives are accepted only if correct for the specific hint. One group is a comparison of several words, not another word or another card. |
| images | Codex draws one flat, text-free illustration per root; every card of that root uses it. Existing images are reused, never deleted. |

Up to `[ai] concurrency` (4) Claude batches run at once. Usage limits,
used-up credits, a logged-out CLI or an outage pause all AI work: the run
retries every 5 minutes (or sleeps until the reset time it's told), says so
once and then periodically while waiting, and carries on by itself. Answers are
schema- and ID-checked before they're applied. Valid independent members of a
partly invalid response are saved; only invalid or missing members retry, with
the previous validation error included as feedback. Database writes remain
transactional. Jobs remember a fingerprint of their inputs, so finished work
never runs twice unless its inputs change. Failed jobs retry individually after
a five-minute cooldown, up to three attempts. The normal run waits for due
retries; restarting also releases cooldowns left by the previous run. Exhausted
jobs are reported and can be retried on a new run. Human edits always win.

Word enrichment already writes an individual hint. The separate comparison step
needs the other words' finished prompts, so it runs after runnable enrichment
and batches up to eight groups together. This avoids inserting a small hint call
between almost every word batch. Parallel dictionary reads are serialised through
one lock; network and Claude calls remain concurrent.

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
and image exist and its root's text has been written (`quality.py` — one
rule for sync, audit and share). Audits are informational unless
`[audit] block_failures = true`.
`[images] required = false` or a list's `image_policy` lets notes go
without waiting for their image.

| Card type | Deck |
|---|---|
| vocab — word, gender · plural or the four adjective forms, IPA, film line, note, fun fact | the root's highest-priority list (`Italian::Movies::…`, `Italian::CILS::A1`, …) |
| phrase — sentences, stems, expressions | same |
| form — conjugations for model verbs (*parlare, credere, dormire, finire*), irregular verbs and the most frequent N; compound tenses built by rule | `Italian::Verbs::Learning::<Tense>` or `Italian::Verbs::Dormant::<Tense>` |
| nphrase — rule-built article / preposition / possessive drills | `Italian::Noun Phrases` |
| cognate — one card per suffix rule with its examples | `Italian::Cognates` |
| mistake — practice errors | `Italian::Mistakes` |

Study **Italian::Verbs::Learning** to practise these seven subdecks together:
Presente, Passato Prossimo, Futuro Semplice, Imperativo con Lei, Imperativo con tu,
Imperfetto and Presente Progressivo. Other configured forms go into separate
**Italian::Verbs::Dormant** subdecks: Condizionale Presente, Condizionale Passato,
Imperativo con noi and Imperativo con voi. Dormant cards are suspended.

`[conjugation].learning_tenses` and `learning_imperative_persons` select the
Learning group; `tenses` controls generation for both groups. Moving a tense to
Learning reuses its saved prompts and cards and lifts suspensions applied by
this feature. Existing manual/leech suspensions stay. Dormant notes do not
consume the optional study horizon's new-card budget. Study history does not
protect obsolete or unsuitable cards from removal.

Claude can mark any unsuitable form as ignored by returning an `exclusions`
entry with a reason instead of a prompt. That counts as completed work and is
stored against the selected meaning. For example, ordinary imperative drills
for *potere* are excluded. Ignored forms are omitted from publication and any
existing notes for them are removed, even if studied. Noun-phrase drills
whose plural or gender is ambiguous are handled the same way.

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
- removes obsolete old notes once their replacement is confirmed in Anki,
  including studied duplicates. Explicitly removed, disabled or ignored forms
  need no replacement. Every note is re-read just before deletion. `--keep-old`
  turns deletion off;
- **study history does not protect a card from removal** or keep a disabled
  direction enabled. Valid existing notes can still be reused in place. If one
  direction of a two-direction note is disabled, that card is suspended while
  the other direction remains usable;
- never touches notes outside the pipeline's decks and never deletes media;
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
   gives the full coverage table. Each run (when the lists change) writes the
   film's word list to `inputs/movies/<slug>/words.csv` — lemma, part of
   speech, count, first appearance, forms heard, example line. Subtitles and
   word list are in git.

Subtitles are parsed (ads, tags and hearing-impaired brackets dropped),
lemmatised with SUBTLEX + Wiktionary forms (+ clitic stripping), names are
dropped, and every root gets its count, first appearance and an example
line, which appears on the card with a **word-for-word English gloss** written
by Claude (25 lines per call), e.g.:

> “Sono andato da Grana in due ore e 37 minuti. Ottima corsa.”
> word for word: I-am gone to-the-place-of Grana in two hours and 37 minutes. Excellent run.

The gloss follows the Italian word order exactly (hyphens join the English for
one Italian word) and must cover every word, or it's asked for again. Cards
don't wait for it; it appears on the next sync once written.
Movie cards are recognition-only by default.
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
| `review_decisions`, `cue_overrides`, `form_exclusions`, `asset_manifest`, `metadata` | legacy saved corrections, reasoned verb exclusions, media checksums, checkpoints |
| `entries`, `verb_forms`, `noun_phrases`, `cards` | v3 data, kept to adopt your studied notes |

Every run starts with a database backup (`backups/` keeps the newest 10);
migrations are backed up too. Before the first change to Anki, each run also
backs up the whole Anki collection — every deck, note, card and review, without
media — to `backups/anki/` (newest 5; restore by quitting Anki and putting the
unzipped `collection.anki2` back in your profile folder). One run at a time (`database.sqlite.lock`).
Media is never deleted or overwritten. Film subtitles and their word lists are
in git (`inputs/movies/`).

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

Tests: `.venv/bin/python -m unittest discover -s tests -t .`

Dictionary data: [Wiktionary](https://en.wiktionary.org) via
[kaikki.org](https://kaikki.org), CC BY-SA 4.0 (see `lexicon/README.md`).
Frequency data: [SUBTLEX-IT](https://osf.io/zg7sc/).
