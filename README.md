# Italian Flashcards

[Download free Italian Anki flashcards](https://italianflashcards.cameronengineer.com/)
for vocabulary, numbers and verb conjugation, with pictures, audio and real card previews.
No setup of this repository is needed to use the downloadable decks.

A personal pipeline that turns what you want to learn — CILS levels, class
notes, frequency lists, **the subtitles of a film you're about to watch** —
into Anki cards. It is built word-first: every Italian root word exists
once, gets its grammar from Wiktionary, its card text and a fun fact from
Claude (which also checks the dictionary data), an image from Codex, and
lands in Anki in the order your study plan says you need it.

Everything runs on subscriptions — Claude (`claude -p`) and ChatGPT/Codex
(`codex exec`) — plus ElevenLabs credits for audio, with no pay-as-you-go APIs.
A run uses each one until its limit is reached, then stops asking it; the next
run carries on where it left off.

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
      images       as many as Codex allows (stops when its usage limit is reached)
      claude       writes until its usage limit is reached, then stops
      09:12  asking Claude: 3× words (45), 1× verbs (3) …
      09:13  words   ✓ 15  questo, quello, molto +12            52s · 1,705 left
      ...
      10:02  claude     ■ stopped: usage limit reached (resets 3pm) — the rest is written by a later run
      10:02  ── round 4: 300 items written · Anki: +240 new · 2 updated  (9s)

[5/6] Audio & media
      audio        1,204 new recordings · stopped: ElevenLabs credits used up (the rest is made next run)
      compress     nothing new

[6/6] Anki · final sync
      13:40  up to date

════════════════════════════════════════════════════════════════
  Finished in 4h 28m
      Anki         +880 new cards · 31 updated · 1,020 old cards replaced
      Claude       1,649 words · 187 phrases · 91 verbs written · stopped: usage limit reached (resets 3pm)
      Movie        Le otto montagne (2022): you know 65% of its words · 185 more → 90%
      Flagged      Claude doubted the dictionary data for 327 words (published anyway)
      Images       214 made this run · stopped: You've hit your usage limit … · 187 notes wait for one
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
| `--batches N` | Claude and Codex batches between Anki syncs (default 20) |
| `--once` | stop after one round |
| `--only PHASE`, `--skip PHASE` | run only, or leave out, one phase (repeatable; see below) |
| `--no-ai`, `--no-sync` | shorthands for `--skip enrich --skip audio` and `--skip publish` |
| `--keep-old` | keep obsolete notes instead of deleting them (studied or unstudied) |

**Images.** Codex draws on your ChatGPT plan, so by default (`[run] image_limit =
-1`) a run makes as many images as the plan allows. When Codex says it can't
make more (usage limit, credits used up, logged out) the run stops asking for
the rest of that run and carries on with everything else; the remaining images
are made by a later run. It also stops after `[images] stop_after_failures`
images fail in a row. Network outages are still waited out.

**Claude and audio** work the same way. Claude writes until it says its usage
limit is reached (5-hour or weekly), then the run stops asking it and carries on
with images, audio and export; unwritten cards wait for the next run
(`[ai] on_limit = "wait"` instead pauses until the limit resets). Audio
(`[run] audio_limit = -1`) records until ElevenLabs says the month's credits are
used up, the key or plan is refused, or `[run] audio_stop_after_failures`
recordings fail in a row. Changing the `[audio]` voice settings re-records
everything, so with no limit that spends the month's credits.

**Phases.** A run is a fixed sequence of phases (`flashcards/pipeline.py`, one
function each). `--only` and `--skip` take these names:

| Phase | Does | Segment |
|---|---|---|
| `resolve` | your edits, then lists → root words (only when something changed) | 1 Prepare |
| `plan` | works out what still needs Claude / Codex and queues it | 2 Cards |
| `build` | the notes Anki should hold | 2 Cards |
| `publish` | syncs Anki with what is ready | 3 Anki |
| `enrich` | Claude writes cards and Codex draws, in rounds; each round ends with `build` + `publish` | 4 Claude |
| `audio` | new recordings, up to the limit | 5 Audio & media |
| `compress` | small copies of new images and audio | 5 Audio & media |
| `export` | `lexicon/*.jsonl` and one `.apkg` per deck | 6 Final sync |

Settings, the dictionary check and the database backup always run first.
`build` and `publish` run again after `enrich` and after `audio`. For example
`./run.sh --only build --only publish` pushes what is already written, and
`./run.sh --skip export` leaves `lexicon/` and `decks/` alone.

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

`settings.toml` is set up for this now: `model = "opus"`, `effort = "high"`,
`thinking = true`, `task_version = "7"`. To do it again later, change the model
and bump `task_version`, then keep running `./run.sh`.

- **Everything Claude wrote is redone from scratch** in study order: words,
  phrases, verb forms, cue hints and word-for-word film lines. Claude is given
  only the source data (dictionary, your lists, film lines), never its earlier
  answer. Verb forms and cue hints follow once their words are redone.
- **Your cards stay in Anki meanwhile.** They keep their current text until the
  new text arrives, and are then **updated in place**, keeping your review
  history. A word's main meaning always keeps the same note, whichever
  dictionary sense the new answer picks.
- **Claude may ignore an entry** that shouldn't be a card: not Italian, a name,
  a stray abbreviation, a bad parse, or a rare second part of speech of a word
  you already study (e.g. the noun *il camminare* next to the verb). Its notes
  leave Anki; `./run.sh review` lists ignored words with the reasons. A word
  whose senses you edited yourself is never ignored.
- Your own edits, images and audio are kept (media is never regenerated or
  deleted). Changing only `model` affects work still to do.
- Opus with thinking uses your plan's limits much faster than Haiku, so a full
  pass takes days of usage-limit waits. Leave `./run.sh` running, or rerun it;
  it carries on where it stopped.

Every completed run writes one `.apkg` per ready deck into `decks/`, including
its images, audio and film examples. A deck's new-card order is the study plan
alone (list priority and rank), numbered within the deck, so your own progress
and other decks never reorder it. Unchanged packages are reused; changed
media, templates, order or content rebuild that package only. Packages
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
| `leech` | suspend + tag cards you keep failing; `leech --doctor` writes memory aids; `leech --release` unsuspends and untags them all (they count again only if they fail again) |
| `audit` | Claude reviews conjugations, ambiguous cues and gender-changing plurals first; matching audits are counted separately from publishable notes |
| `share` | `.apkg` for someone else (only lists marked `shareable`; film lines left out) |
| `doctor` | health report: tools, settings and a data-quality census (read-only; `--database FILE` reviews a backup; `--anki` checks AnkiConnect still offers every action used — worth running after updating it) |
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
| `plan.toml` | **When**: list priorities (a film with an upcoming watch date first), new cards per day, conjugation generation and publication settings, which extra card types are on. |
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
| `subtlex` | the most frequent verbs / nouns / adjectives / adverbs from SUBTLEX-IT (`verb_limit`, `noun_limit`, `adjective_limit`, `adverb_limit`) |
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
| words (`lexeme_enrich`) | Given the Wiktionary senses, the lists' own glosses and the film line: choose the most obvious meaning (one per card), write the card prompt + hint + register + note, **check the dictionary data** (doubts are recorded, never a hold), write a fun fact from the sourced etymology — plus one hop further down, fetched from kaikki.org just before the word is enriched (*rubinetto* ← French *robinet* ← *Robin*, "sheep"). 15 words per call. |
| phrases | English prompt + hint for sentences and stems. |
| verbs | English prompts for the Wiktionary conjugations (forms that don't exist, like the imperative of *dovere*, are left out). |
| facts (`fact_check`) | Re-read each fun fact shown on a card: keep it, fix it (cut a wrong or obscure claim) or drop it. English words in a fact must be ones a typical native speaker knows. New facts are checked automatically. |
| cue groups (`disambiguate`) | Compare words sharing an English prompt, adding distinguishing hints. Alternatives are accepted only if correct for the specific hint. One group is a comparison of several words, not another word or another card. |
| images | Codex draws one flat, text-free illustration per root; every card of that root uses it. Existing images are reused, never deleted. |

Up to `[ai] concurrency` (4) Claude batches run at once. Claude's usage limit
stops Claude for the rest of the run (see above). A logged-out CLI or an outage
pauses all AI work: the run retries every 5 minutes, says so once and then
periodically while waiting, and carries on by itself. Answers are
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

Every root is worked on, in study order; Anki's daily new-card limit paces what
you see. Verb families already in Anki are maintained whichever new words have
the highest priority.

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
| vocab — word, gender · plural or the four adjective forms, IPA, film line, note, fun fact | every list the word is in, one card per list's deck (`Italian::Movies::…`, `Italian::CILS::A1`, …) |
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
this feature. Existing manual/leech suspensions stay. Study history does not
protect obsolete or unsuitable cards from removal.

Claude can mark any unsuitable form as ignored by returning an `exclusions`
entry with a reason instead of a prompt. That counts as completed work and is
stored against the selected meaning. For example, ordinary imperative drills
for *potere* are excluded. Ignored forms are omitted from publication and any
existing notes for them are removed, even if studied. Noun-phrase drills
whose plural or gender is ambiguous are handled the same way.

### 4 · Anki (`reconcile.py`)

Targeted AnkiConnect calls — no `.apkg` imports. Anki is read once per sync.

- refuses before writing anything if the wrong Anki profile is open, a
  note it doesn't own is found, or many notes it put in Anki have vanished;
- adds ready notes, updates changed ones, moves them when their home deck
  changes; your own tags (and `leech`) are kept, pipeline notes carry
  `fc::owned`;
- removes obsolete notes once their replacement is confirmed in Anki,
  including studied ones. Explicitly removed, disabled or ignored forms
  need no replacement. Every note is re-read just before deletion. `--keep-old`
  turns deletion off;
- **study history does not protect a card from removal** or keep a disabled
  direction enabled. Valid existing notes can still be reused in place. If one
  direction of a two-direction note is disabled, that card is suspended while
  the other direction remains usable;
- never touches notes outside the pipeline's decks and never deletes media;
- adds new notes in study-plan order. AnkiConnect (since October 2026) can't
  move a card once it is in Anki, so cards already there keep their place;
- reads what you know (interval ≥ `known_interval_days` in `plan.toml`, 21 by default) back into `card_knowledge`, which
  drives study order and movie coverage.

### 5 · Audio (`commands/media.py`)

ElevenLabs audio for ready notes, in study order, until the month's credits
run out (or up to `[run] audio_limit` new files per run, if set). The spoken text drops display variants
(*sono andato/a* → "sono andato").

---

## Movies

1. Put Italian subtitles in `inputs/movies/<slug>/subs.it.srt` and add a
   `kind = "movie"` list (see the *Le otto montagne* entry).
2. Give it a watch date in `plan.toml`; its words come first until that date
   has passed, after which the list keeps its written place.
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
| `identity`, `retirements`, `sync_runs` | notes sent to Anki, notes marked to leave it, sync history |
| `form_exclusions`, `film_glosses`, `cue_fixes`, `audits` | reasoned verb exclusions, word-for-word film lines, rewritten prompts, audit verdicts |
| `asset_manifest`, `metadata` | media checksums, checkpoints |

The schema is written out once in `flashcards/db.py`.

A database older than schema 13 (from before the lexicon replaced the old
pipeline) must first be run once with commit `3e6520e`.

Every run starts with a database backup (`backups/` keeps the newest 10, plus the first of each of the last 14 days);
migrations are backed up too. Before the first change to Anki, each run also
backs up the whole Anki collection — every deck, note, card and review, without
media — to `backups/anki/` (newest 5, plus the first of each of the last 14 days; restore by quitting Anki and putting the
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
