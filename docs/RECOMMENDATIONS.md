# Recommendations — v4

Reviewed 2026-10-08; **v4 implemented 2026-10-09** (see the status below).
The analysis that led here is kept underneath for reference.
The follow-up safety review and what was applied from it:
[RECOMMENDATIONS_REVIEW_2026-10-09.md](RECOMMENDATIONS_REVIEW_2026-10-09.md).

**The thesis:** look things up first, generate by rule where Italian is
regular, and use Claude to *judge, connect and explain* — not to recall.
Content is rebuilt from scratch on that basis; only identities and review
history carry over from the old collection.

## Status

| # | Recommendation | Status |
|---|---|---|
| P1 | Word-first lexicon, roots first | **Done** — `lexicon.py`; 5,046 roots replace 8,884 source-scoped entries; facts and images live on the root |
| P2 | Kaikki/Wiktionary first, Claude verifies | **Done** — `kaikki.py` (streamed index, form→lemma map, conjugations, IPA, Commons audio URLs, etymology + one-hop source entry); `lexeme_enrich` verifies and flags disagreements |
| P3 | Regenerate content from scratch | **Done (running)** — every root is re-enriched through the queue; old glosses are not reused |
| P4 | Durable AI job queue | **Done** — `queue.py`; study-order priorities; sleeps until a stated reset time |
| P5 | Anki as a reconciled projection | **Done** — `reconcile.py` (`plan` / `apply`), owned notetype, drift guard |
| P6 | One note, two templates | **Done** — "Italian Flashcard v4", Recognition/Production switched per note |
| P7 | Knowledge + study plans | **Done** — `plan.toml`, knowledge read back on apply, film coverage report |
| P8 | Reviewable data in git | **Done** — `lexicon/*.jsonl` with provenance; `human` edits win; identity map |
| P9 | Media | **Done** — every card has its root's image (Codex); audio in study order; separate audio text. *Open:* use Wikimedia Commons recordings before ElevenLabs |
| P10 | Patterns, not tables | **Done** — model verbs + irregular + top-N full forms; numbers by rule; cognate families |
| P11 | Learning loop | **Done** — `leech --doctor`, practice mistakes → cards, film lines on cards. *Open:* cloze cards from example sentences |
| P12 | Sense-aware cards | **Done** — `disambiguate` task: hints + "also accepted" |
| §7 | Movie decks | **Done** — *Le otto montagne* is the first list (937 roots; 686 roots → 95% coverage) |
| §6 | Ideas from `../italiananki` | **Done** — cognates, audio column, adjective forms, `share`; Fluent Forever 625, Caffè, Tutto Bene ported as lists |
| §9 | Quick wins | **Done** — audio text, CILS POS/lemma, recognition-only giveaways, notes off the prompt, reset-time waits, batching everywhere |

### Next

1. **Finish the first pass** (needs you): `codex login` for images, open Anki,
   `./run.sh` — the queue (~4,000 roots, ~280 Claude calls) spans several
   plan windows and resumes by itself.
2. **Approve or fix disputed roots** (`./run.sh review`) — Claude flags real
   dictionary mismatches (e.g. it caught *di* matched to the letter name).
3. **Studied duplicates**: v4 adopts one studied note per word and direction;
   other studied copies stay where they are. A scheduling-transfer tool
   (copy interval/ease onto the adopted note, then retire) would finish the
   consolidation — test FSRS memory state on a few cards first.
4. **Commons audio** for roots (free native-speaker recordings, licence per
   file), ElevenLabs for the rest.
5. **Codex as a second verifier** for disputed roots or facts (independent
   model + Wiktionary agreement), within ChatGPT plan limits.
6. **Cloze cards** from film lines once a film is watched.
7. Remove the empty legacy notetypes from Anki once nothing uses them.

---

## 1. Where things stood (2026-10-08, before v4)

- Pipeline: `sources.json` → modes → SQLite → cards → `.apkg` → Anki.
  All text AI runs on Claude (`claude -p`, your subscription) and pauses
  when the plan limit is reached; images use OpenRouter (key currently 401).
- Fun facts: the backfill reached **1,200 of 4,464 words (378 facts)**,
  then hit the plan's session limit; the background run was stopped by its
  2-hour cap. Facts are saved per batch — `./run.sh` in your own terminal
  continues where it left off. Kinds so far: English link 131, usage 95,
  etymology 94, culture 38, false friend 20 (average confidence 0.9).
- Facts and images already attach to **roots only**: facts on gloss/avere
  cards, verb infinitives and noun singulars; verb forms reuse the
  infinitive's image, noun phrases the singular's. v4 keeps that rule and
  makes it structural (P1).

---

## 2. Data review — what's in the data at each stage

### Inputs

| Finding | Numbers | Fix |
|---|---|---|
| **CILS gives you lemma and part of speech, and the pipeline throws them away.** | Every CILS row has `italian_original` (bare lemma) and `function` (*sostantivo*, *verbo riflessivo*, *agg.*…); only `italian` / `english` are read. CILS verbs like *ballare* become plain gloss cards. | Read both columns; route rows to lexemes by POS (P1). |
| **Adjective inflection is encoded as text.** | 717 CILS rows like `bello/a/i/e`, `gentile/i`, `trovare/si`. These become the literal answer and the audio text. | Store the lemma; get the four forms from Kaikki or rules; show `bello · bella · belli · belle` on the back (the old repo did this). |
| **Pairs and stems in single rows.** | `il professore / la professoressa`, `il/la giocatore/trice`; 87 exam-prep stems ending in `…`; 64 italki full sentences. | Split pairs into two lexemes (or one with gender forms); treat stems and sentences as a separate *phrase* card type (no per-word facts). |
| Duplicate rows | `l'acqua` twice in each CILS file; 4 in italki. | Dedupe at list level (already collapsed at ingest). |
| Numbers | 400 hand-listed numbers. | Generate by rule (P10): learn the system plus irregulars (*undici–sedici*, *ventuno/ventotto* elision, *cento*, *mille/mila*). |

### Entries (AI enrichment)

| Finding | Numbers | Fix |
|---|---|---|
| **The same word is learned many times.** | 8,884 live entries, **4,465 distinct words**; 1,887 words in 2–6 sources ("chi" in six); 4,406 duplicate entries. | P1 lexicon. |
| **English sides are overloaded.** | 32% carry a `(disambiguation)`, 759 a `[usage note]`; longest is 326 characters (*"artist (creative practitioner in art) [A noun of common gender. The plural is…"*). | Short prompt on the front; disambiguation as a small hint; usage notes moved to the back's details line. |
| Low-confidence entries | Only 5 below 0.8 (e.g. *Forsa!* 0.55). | Fine; `review` already lists them. |
| Auxiliary *both* | 55 verbs. | Kaikki/rules give per-sense auxiliaries; show both on the card. |

### Generated forms

| Finding | Numbers | Fix |
|---|---|---|
| **Two-thirds of all cards are conjugations.** | 45,540 of 69,424 cards are verb forms, AI-generated, 46 per verb. | Rules + Kaikki forms; teach patterns, drill irregulars (P10). |
| Gendered participles in one string | 374 forms like `sono arrivato/a`. | Separate display text from audio text; audio says *sono arrivato*. |
| Imperatives are ambiguous | "Kill!" has 6 answers in one deck (2 verbs × persons). | Sense-aware cards (P12). |
| Spot checks passed | No *il* before *s*+consonant/*z*; no article before singular family nouns in possessives; every presente prompt has a subject. | Good — the rules the AI followed are exactly the ones a rule engine would encode. |

### Cards (what you actually see)

| Finding | Numbers | Fix |
|---|---|---|
| **Prompts with more than one right answer.** | **1,315 prompts / 2,850 cards** where deck + English + labels are identical but the Italian differs: *therefore, so* → *dunque / quindi / perciò*; *the face* → *la faccia / il viso / il volto*. | P12: one card per sense with "also accepted", or a disambiguating hint. |
| Production cards that give the answer away | 22 (interjections like *Ah!*, *Shh!*, *Olé!* identical in English). | Recognition-only for identical/near-identical items. |
| Audio text that TTS reads literally | 660 strings with `/` (`sono andato/a`, `vu / vi`), 60 with `…`. | Separate `audio_text` (the old repo had an explicit `audio` column for exactly this). |
| Images for words that can't be pictured | 162 images for short function words (*e*, *ma*, *che*…); 4,627 images total, 3.4 GB of originals. | Imageability flag per lexeme (P9). |

### Media and output

- Audio: 11,627 of 25,917 strings have none, at 1 new file per run —
  generated in text order, not study order (P9).
- Export/sync: every run re-zips all media into 32 packages and re-imports
  them; the importer created four notetype copies (P5).
- Facts: quality is high and selective (31% of words got one). They are
  recalled, not sourced — P2 grounds them.

---

## 3. Target architecture

```
 LISTS (what you want to learn)            REFERENCE DATA (looked up, free, citable)
 CILS (with POS + lemma) · italki ·        Kaikki/Wiktionary: POS, gender, forms, IPA,
 SUBTLEX top-N · exam prep · movies        glosses, etymology, Commons audio
        │                                  SUBTLEX: frequency · rules: articles,
        │ resolve to lexemes               prepositions, regular conjugation, numbers
        ▼                                          │
 ┌────────────── LEXICON — one record per root, versioned in git ──────────────┐
 │ lemma · POS · senses (gloss, register) · forms · fact (+sources) · examples │
 │ every field carries provenance: kaikki | rule | claude | human              │
 └──────────────────────────────────────────────────────────────────────────────┘
        │                          ▲
        │                          │ verify · disambiguate senses · write prompts & facts
        │                  AI JOB QUEUE — Claude (primary), batched, resumable
        ▼
 STUDY PLAN (priorities, deadlines, known words) ──▶ desired notes (card types × lexemes)
        │ plan → apply (AnkiConnect, targeted changes)
        ▼
      ANKI ── review state back ──▶ KNOWLEDGE (unknown / learning / known per lexeme)
```

---

## 4. Proposals

### P1 · Word-first lexicon, roots first
The unit becomes a **lexeme** (lemma + POS) with **senses** and **forms**.
Sources become **lists** that reference lexemes (`list_items` carry rank and
context). One note per lexeme-sense per card type, tagged with every list it
appears in (`list::cils_a1`). **Facts, images and examples belong to the
root**; forms inherit them (an imperative never needs its own image or fact).
Payoff: no duplicate learning; "do I know this word?" is a query; movie
decks fall out naturally. Cost: large — the core of v4.

### P2 · Reference data first, Claude second — *Kaikki in depth*

**What Kaikki provides.** [kaikki.org](https://kaikki.org/dictionary/Italian/)
publishes machine-readable extracts of English Wiktionary. The Italian
dictionary is one JSON Lines file (~735 MB, 589,335 word forms, Wiktionary
dump of 2026-09-02). For *rubinetto* it gives: noun, masculine, plural
*rubinetti*, alternative form *robinetto*, IPA /ru.biˈnet.to/, hyphenation
*ru‧bi‧nét‧to*, senses "tap (UK), faucet (US)" and "any similar device used
to regulate the flow of a fluid", etymology "Borrowed from French
*robinet*", and links to native-speaker recordings on Wikimedia Commons.
Inflected forms carry tags (e.g. *andiamo* = first-person plural present of
*andare*), which gives a **form → lemma map** — what turns movie subtitles
into a word list.

**Pipeline.**

1. **Import once:** stream the JSONL into `kaikki_entries(word, pos, json)` +
   a `kaikki_forms(form, lemma, pos, tags)` index (SQLite, a few hundred MB;
   refresh a few times a year). Keep the file out of git.
2. **Resolve:** each list row → candidate lexemes by lemma (CILS
   `italian_original` + `function` make this exact for CILS; SUBTLEX gives
   lemma + POS; free text uses the form map).
3. **Fill deterministically:** POS, gender, plural, forms, IPA, auxiliary
   (from Wiktionary's verb data), articles/prepositions/regular conjugation
   from rules. Store `provenance = kaikki` or `rule`.
4. **Claude verifies and writes** (batched, P4), given the Kaikki record as
   context:
   - *pick the sense* the list means (CILS "la destra" = right-hand side,
     not political right) and write a short, natural English prompt;
   - *check* the looked-up data against its own knowledge and flag
     disagreements instead of silently choosing;
   - *write the fun fact from the sourced etymology*, following the chain
     where it gets interesting — Kaikki says *rubinetto* ← French *robinet*;
     the French entry (Kaikki has every language) takes it to *Robin*, a
     traditional name for a sheep, and Claude turns that into one sentence
     with both sources linked;
   - *generate what Wiktionary lacks*: natural example sentences, mnemonics.
5. **Disagreements → review queue.** When Claude and Kaikki disagree
   (gender, auxiliary, an etymology), the record is marked
   `needs_review`, kept out of new cards, and listed by `flashcards review`.
   A human edit (`provenance = human`) always wins and is never overwritten.

**Other non-AI resources worth using**

- **SUBTLEX-IT** (already here): frequency for ordering and for "is this
  word worth learning?".
- **Wikimedia Commons pronunciations** (linked from Kaikki): real
  native-speaker audio for many roots — free, check each file's licence;
  ElevenLabs for the rest.
- **CILS POS/lemma columns** (already in your files).
- **Cognate rules** from the old repo (§6): deterministic detection of
  "free" words (*favore* ↔ favor) by string similarity after suffix rules.

**Licence:** Wiktionary text is CC BY-SA; for personal decks keep an
attribution line on the card back or in the deck description; if you ever
publish decks, share them under the same licence. Cite wiktextract
(Ylonen, LREC 2022) as Kaikki asks.

**Payoff:** grammar correct by construction, facts with sources, IPA on
cards, a lemmatiser for movies, and an order-of-magnitude fewer AI calls
(the plan limit stops being the bottleneck).

### P3 · Regenerate the content from scratch
Today's glosses, forms and phrases were generated by an earlier model
without grounding. Rather than migrating them, **rebuild every lexeme's
content** through P2 (Kaikki + rules + Claude verification). Carry over only
what can't be regenerated: **note identities and review history**. For
studied notes, the new content updates the existing note in place (same
GUID, new fields), so history is untouched. Keep the current fact backfill
running in the meantime — facts migrate as drafts that P2 then re-verifies
against sources.

### P4 · AI job queue
`ai_jobs(task, subject, input, status, attempts, batch_id, result, error)`.
`build` enqueues and never blocks; `flashcards work` drains in batches for
every task (glosses, verification, facts, audits), sleeps until the plan
window reopens (parse "resets 11:40pm" rather than polling every 5
minutes), and records failures for `flashcards jobs`. Backends are
pluggable: Claude (`claude -p`) first; a second backend (§5) can take
overflow or act as an independent verifier.

### P5 · Anki as a reconciled projection
Own the notetypes (`createModel`, versioned). Compute desired notes;
`flashcards plan` shows *add · update · move · retag · retire · reorder*;
`flashcards apply` does it through AnkiConnect (`addNotes`,
`updateNoteFields`, `changeDeck`, `addTags`, `storeMediaFile`) with today's
safety rules. `.apkg` export stays only for sharing.

### P6 · One note, two templates
v4 notetype: *Recognition* (IT→EN) and *Production* (EN→IT) on one note,
Production conditional on a field. Recognition-only for giveaways (the 22
interjections), cognates if you want, and movie lists. Half the notes;
native sibling burying.

### P7 · Knowledge + study plans
Read review state back into `knowledge(lexeme, card_type, state, interval,
lapses)`. A `plan.toml` orders priorities (`movie:x by 2026-10-20`,
`cils_b1`, `subtlex limit 2000`, new cards per day); the reconciler builds
the new-card queue from it and skips what you know. Today's queue is 67,070
new cards — ~9 years at 20/day; a plan makes it a schedule.

### P8 · Reviewable data, rebuildable database
Lexicon + identity map as sorted JSONL in git with per-field provenance.
Edits are diffs; `human` edits survive regeneration; SQLite is an index you
can delete. Add a **drift guard**: sync refuses if more than a few percent
of known note identities would change.

### P9 · Media
- **Roots get images, forms inherit** (already true; make it a lexeme field).
- **Imageability** per lexeme (concrete nouns yes; *ma*, *dunque*, *siccome* no).
- **Separate `audio_text`** from display text (fixes 660 slash strings).
- **Just-in-time generation** in study order within a budget.
- **Commons recordings** where available (P2), ElevenLabs otherwise.

### P10 · Teach patterns, not tables
- Verbs: pattern cards per tense for regular classes; full forms only for
  irregular/high-frequency verbs and tenses in your plan; in-context drills
  ("Ieri (andare) al cinema").
- Numbers: the system plus irregulars, generated by rule.
- **Cognate families** (from the old repo): group transferable words by
  rule (*-zione* ↔ -tion, *-tà* ↔ -ty, *-oso* ↔ -ous) and teach the rule
  with examples — dozens of words per card, and a natural place for an
  "English link" fact.

### P11 · Close the learning loop
**Leech doctor** (mnemonic or contrast example for failing cards), **mistake
mining** (practice errors become cards), **example sentences** per sense
(later cloze cards).

### P12 · Sense-aware cards (fixes the 2,850 ambiguous cards)
Cards are per *sense*, not per word string. When several Italian words
share an English prompt, either merge them into one card with the primary
answer plus "also: …", or add a short distinguishing hint generated from
the senses (*the face (neutral)* vs *the face (literary)*). The audit task
gets a check for "another common word also fits this prompt".

---

## 5. Your ChatGPT Plus — what it can and can't do

- **No API access is included.** Like Claude, OpenAI bills API use (including
  image models) separately from the ChatGPT subscription
  ([What is ChatGPT Plus?](https://help.openai.com/en/articles/6950777-what-is-chatgpt-plus)).
- **Codex CLI** can sign in with a ChatGPT plan and has a non-interactive
  `codex exec` mode
  ([Codex CLI](https://learn.chatgpt.com/docs/codex/cli.md)), but OpenAI's
  docs say to **use API-key authentication for programmatic workflows**
  ([Codex sign-in](https://learn.chatgpt.com/docs/auth)). Treat
  plan-based automation as personal, low-volume use at your own judgement.
- **Images:** third-party write-ups report that Codex CLI has a built-in
  image tool that runs on the ChatGPT login without per-image billing
  ([example](https://www.skills.sh/giulioco/skills/codex-imagegen)); it is
  not in the official CLI page I checked, and Codex isn't installed here.
  Worth a test (`npm i -g @openai/codex`, sign in, `codex exec --help`)
  before relying on it. Plus image limits are modest (reports put it around
  40–50 per 3 hours), which fits **just-in-time images for imageable roots**
  (P9), not a 4,000-image backfill.
- **JSON payloads:** if `codex exec` supports schema-constrained output in
  your version, it can be a **second backend in the job queue** (P4):
  overflow when Claude's window is exhausted, or — more valuable — an
  **independent verifier**: two vendors agreeing with Kaikki is strong
  evidence; any disagreement goes to review.
- **Don't** script the ChatGPT web app or browser — that breaks OpenAI's
  terms; use Codex or the API.
- Pay-as-you-go APIs (OpenAI images, Claude API Batches) remain the simplest
  reliable route if volume grows.

---

## 6. What to take from `../italiananki`

It is 1.5 GB (mostly media and book extracts), with one folder of scripts
per deck — the sprawl this repo replaced. Worth keeping:

| Idea | Where | Use in v4 |
|---|---|---|
| **Cognate ("transferable") detection** — Jaro similarity between Italian and English, before and after suffix-rewrite rules; kept 333 of 1,195 frequent lemmas (0.7 ≤ similarity < 1.0). | `sources/Z8_transferable/` | Deterministic cognate flag + rule families (P10); a free source of "English link" facts. |
| **Explicit `audio` column** — "usually base form without `/`". | `spreadsheets/schema.md` | Separate `audio_text` (P9); fixes a regression. |
| **Adjective cards showing all four forms.** | Aggettivi deck | Forms line on adjective cards (P1/P2). |
| **`generate_image` flag per row.** | schema | Imageability (P9). |
| **Published decks with descriptions and sources** on AnkiWeb. | README | Optional `flashcards publish` from the lexicon (CC BY-SA attribution if Wiktionary content is included). |
| Lists not yet ported: Fluent Forever 625, Caffè, Tutto Bene. | `spreadsheets/*.csv` | Import as lists — duplicates disappear under P1. |

Leave behind: per-deck scripts, generated CSVs as source of truth, and the
extracted commercial e-books in `sources/10_vulgarity/` (keep those private;
don't publish decks built from them).

---

## 7. Movie decks on v4

1. `inputs/movies/<slug>/movie.toml` + Italian `.srt` (raw text gitignored).
2. Parse cues; strip timestamps, tags, speaker dashes, `[ride]`; merge split sentences.
3. Lemmatise with the Kaikki form map (spaCy fallback; also drops names).
4. `datasets/<slug>.csv` — lemma, POS, count, first appearance, zipf, example line.
5. Coverage from the knowledge model: "you know 91% of tokens; +250 words → 95%; +610 → 98%".
6. Add as a dated plan priority; recognition-only cards with the film line; known words skipped; shared words are the same notes across films.
7. Claude picks the sense used in the film from the lines (*magari*, *mica*, *dai*).

---

## 8. Migration path

| Phase | Work | Anki impact |
|---|---|---|
| **0 · Now** | Finish the fact backfill (`./run.sh` in your terminal); decide on the 2,887 studied duplicates. | none |
| **1 · Foundation** | P4 job queue; P5 reconciler + owned notetype; identity map + drift guard. | sync becomes incremental |
| **2 · Reference data** | Kaikki import + form map; rules module; CILS POS/lemma; cognate detection. | none |
| **3 · Lexicon + regeneration** | P1 schema; map entries → lexemes; P3 regenerate content with Claude verification; P8 JSONL in git. | studied notes updated in place (same GUIDs) |
| **4 · Consolidate** | Canonical note per lexeme-sense (most reviewed wins), retag, retire duplicates (copy scheduling for studied ones; test FSRS memory state first). | duplicates disappear |
| **5 · Plans** | P7 knowledge + `plan.toml`; P10 card budgets; P6 notetype for new/unstudied items (~94% of cards). | queue shrinks to a plan |
| **6 · Movies** | §7. | new lists |
| **7 · Polish** | P9 media, P11 loop, P12 everywhere. | — |

---

## 9. Quick wins (no v4 needed)

1. Separate `audio_text` and strip `/…` variants (660 strings).
2. Read CILS `italian_original` / `function`; route CILS verbs and nouns properly.
3. Recognition-only for the 22 giveaway cards.
4. Move `[usage]` notes from the prompt to the details line; cap prompt length.
5. Parse the plan-limit reset time and sleep until then.
6. Batch the remaining per-item tasks (glosses, audits) like facts.
7. Remove the `Italian Card Model-4ce4e` notetype once its studied duplicates are resolved; archive `backups/old/` (6.4 GB).

## 10. What I would not do

- Replace Anki or build a web app — reconcile into it.
- Add a server or framework — a local CLI over SQLite + JSONL is the right size.
- Regenerate *identities* — content is rebuilt, notes are updated in place.
- Ask a model for anything a dictionary or a rule already knows.
- Automate the ChatGPT web app.
