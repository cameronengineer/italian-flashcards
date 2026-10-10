# Repository review and architectural recommendations

Reviewed **2026-10-09**, against commit **`dbd7d52`** and the local data available during the review.

This is a new, independent review. `docs/RECOMMENDATIONS.md` remains the historical v4 proposal and implementation status. Recommendations below are based on the current implementation, including places where its behavior differs from that document.

**Follow-up:** [Data and app recommendations](DATA_AND_APP_RECOMMENDATIONS_2026-10-09.md)
reviews the later schema-v5 data, checks the earlier completion claims against
actual outputs, records additional fixes, and lists remaining work. Measurements
below describe the earlier snapshot and should not be read as current counts.

## Assessment

The overall direction is appropriate for a personal learning pipeline: a local Python CLI, SQLite, dictionary-backed enrichment, stable note keys, and Anki as the study interface. Keep that architecture. The next investment should be correctness, recoverability, and explicit state transitions before adding more card types or generating the entire backlog.

Several defects warrant immediate attention. Input normalization corrupts words already present in the database. Reconciliation can delete an unstudied legacy note after its replacement fails to be added, and can overwrite a studied note with content that is still blocked. Human overrides are not reliably preserved. The durable queue persists work, but does not yet provide process-safe claiming, dependency invalidation, or atomic result application.

The recommendations are incremental changes to this application. A server, distributed queue, new UI framework, or database replacement would add complexity without addressing these failures.

## Implementation status (2026-10-09)

Applied in the working tree on top of `dbd7d52` (schema v5, `_m5_integrity`), then reviewed and verified against a scratch copy of the real database: migration, two consecutive lexicon syncs (the second reports 0 identity changes and reopens 0 jobs), queue planning, simulated AI batches and note builds. Tests: 27 passing (`.venv/bin/python -m unittest discover -s tests -t .`); pyflakes is clean apart from the intentional `readline` import.

| ID | Status | Notes |
|---|---|---|
| R01 | Done | Retirement needs a replacement read back from Anki, then re-checks ownership and history. The read-back checks the replacement's key and the direction it takes over, so adopted directions don't cause false failures. A note Anki rejects is reported (`sync_runs.status = partial`) and keeps its predecessor; it no longer blocks the whole sync. |
| R02 | Done | Blocked notes are never adopted. Studied directions stay on. Replaced senses are archived, not deleted. |
| R03 | Done | Article regex fixed; corrected identities are recorded in `identity_aliases` and `audit_reports/input_repairs_latest.json`. **Changed after review:** repaired roots are *not* forced into `needs_review`. On the real data that demoted 223 roots, including correct, already-ready ones. New roots are still verified by Claude. Words with several parts of speech (film *molto* adv + det) no longer show up as false repairs on every sync. |
| R04 | Done | `fc::owned` tag, personal and `leech` tags preserved, profile guard, unowned v4 notes block the sync, leech commands scoped to owned cards and shared presets cloned. Tag changes are batched through `multi`. |
| R05 | Done | `overrides` table; AI and dictionary data never overwrite a human field. |
| R06 | Done | `fc recovery create/verify/restore` with a checksum manifest; restore only into a new folder. |
| R07 | Done | Foreign keys on list membership, senses, prompts and legacy links; list rebuilds are staged in one transaction with a source-shrink guard. |
| R08 | Done | Writer lock, job owners, schema validation before apply, one transaction per batch. **Changed after review:** finished jobs used to re-open whenever their own output changed. The fingerprint covered `draft_fact` (overwritten by every run) and the dictionary version, so every enriched root would re-run forever. Both are now excluded. Failed jobs are no longer revived on every plan, and batches are full (15) again. They had shrunk to 1–4 because of a ±20-rank window. |
| R09 | Done | Cache key includes schema, effort, task version and provider; cached answers are re-validated. |
| R10 | Done | `quality.reasons()` is the single gate for sync, audit and share; derivative forms of disputed verbs are held. |
| R11 | Done | Codex saves `card.png` in its own scratch folder (it may copy the generated file there). The fallback is the path this run printed, never "newest file in the shared folder". Codex is still not logged in, so this has not been tested against the live CLI yet. |
| R12 | Done, adjusted | The gate always reopens. Child processes run in their own process group. Exit codes are 0 done / 1 failed / 2 pending. **Deliberate deviation:** the owner asked that limits, used-up credits *and* logins wait and retry every 5 minutes until they work again. So they stay "unavailable" (with an actionable message) instead of failing jobs. A stated reset time is honoured, at most an hour per sleep. `max_unavailable_minutes` (default 0 = no limit) bounds unattended runs; when it runs out, the jobs go back without being charged an attempt. |
| R13 | Done | Sense identity is the normalised prompt; inactive senses are archived. |
| R14 | Done | 27 regression tests. |
| R15 | Done, opt-in | The planner exists (`planning.py`, `horizon_days`), but it is **off unless `horizon_days` is set**. With the reviewer's default of 7 days on the real data, only about 90 roots were selected. That cancelled 3,513 pending enrichment jobs and left 15 notes publishable, so the pipeline stalled. When the horizon is set, roots and notes already in Anki use no new-card slots. Verb expansion is capped by `max_full_verbs`. `run` now works in rounds of `--batches` AI batches → notes → Anki until the queue is done. |
| R16 | Done | `srt.analyze()` is the single token-accounting function; coverage uses `card_knowledge`. |
| R17 | Done, adjusted | Asset manifest, read-only lookup, validated images, settings-versioned audio/compression names. **Deliberate deviation:** the owner requires an image on every card, so `[images] required = true` is the default. A list's `image_policy` can still opt out. |
| R18 | Done | Typed settings, strict `lists.toml`/`plan.toml`/CSV headers, positive-number CLI checks. |
| R19 | Done | Import into a temp file, integrity check, atomic swap. Content hash and parser version are recorded. Fetched etymology pages are carried over, and one previous index is kept (not one per import). |
| R20 | Done | `shareable` lists only (unless `--include-private`); film lines are never exported; the manifest records hashes and attribution. |
| R21 | Partly | `domain.py`, `planning.py`, `runtime.py`, `doctor`. Larger module splits were not attempted. |

### Second pass (same day)

A follow-up review and cleanup:

**Fixes**
- A reworded sense now keeps its note. Before, rewording "house" to "home" created a new note and orphaned the old one in Anki.
- The source-language etymology (*rubinetto* ← French *robinet*) was implemented in `kaikki.py` but never reached Claude. It is now fetched just before a word is enriched and stored on the lexeme, without reopening finished jobs.
- Verb forms waiting for AI were labelled "needs review".
- Leech commands rescanned the whole collection once per deck.
- A missing ElevenLabs key aborted `run` before its final Anki sync.

**New settings**
- Per-run asset counts in `[run]`: `image_limit` (0 = Codex never starts) and `audio_limit`. In both, 0 = none and -1 = no limit.
- Matching `--images N` and `--audio N` flags.

**Removed**
- Dead code: v3 card templates and helpers, unused utilities, `csvio.py`, the unused single-card audit task, the `codex_concurrency` setting (images run one at a time), and unused `[facts] batch_size` and `[review]` settings.
- The superseded `knowledge` table (replaced by `card_knowledge`).

**Formatting**
- The package is formatted with `ruff format` (line length 110). `ruff check` and pyflakes are clean.
- 34 tests.

## Scope and method

- Read all **34 Python modules, 6,607 lines**, plus manifests, settings, launch scripts, documentation, and repository ignore rules.
- Parsed all **17 input CSVs** and both committed JSONL files; inspected representative source-to-lexeme and lexeme-to-note transformations.
- Opened the live SQLite database read-only and used SQLite's backup API to create a consistent review snapshot outside the repository. Queried that snapshot and used in-memory databases for destructive scenarios.
- Inspected dictionary metadata, subtitle parsing and coverage, existing audit reports, media inventory, and two existing `.apkg` collections.
- Ran the configuration check, parsed all Python modules with `ast`, and ran the installed Pyflakes checker.
- Exercised normalization, human override, list removal, reconciliation failure, review gating, retry-gate, and cache-key scenarios with local fixtures and mocked external calls.
- Checked every media reference on ready notes, verified their 418 referenced JPEGs with Pillow, inspected one house illustration visually, and decoded metadata for three referenced MP3s with `ffprobe`.

No live Anki requests, AI generation, dictionary downloads, paid API calls, or production data mutations were performed. No secrets were read. This review does not certify every translation, fact, recording, or image; it combines complete source review, structural data scans, and targeted output samples. Actual Anki scheduling/template behavior and external provider compatibility still require integration checks in a disposable profile. Existing package samples are legacy outputs, not evidence that v4 export has been exercised successfully.

**Evidence labels:** “Observed” means measured in the snapshot or existing artifacts; “Reproduced” means demonstrated with an isolated fixture; “Code finding” identifies a reachable implementation path without claiming it has already occurred in production.

## Current repository and data

### Architecture as implemented

```text
lists.toml + inputs/ + plan.toml
              |
              v
lists / srt -> lexicon <-> Kaikki index + Italian rules
                 |
                 v
           SQLite working state
            |             |
            v             v
      AI jobs/cache   JSONL export + partial human-edit import
            |
            v
      senses / facts / form prompts / images
            |
            v
      materialized v4_notes
          |          |
          v          v
     reconcile     share .apkg
          |
          v
         Anki -> knowledge -> movie coverage

Audio is generated separately and picked up by a later note rebuild.
Legacy tables and models remain involved in adoption and study tools.
```

`knowledge` is not currently consumed by `study_order()`. JSONL is not a complete recovery source. These distinctions matter when reasoning about the diagram in the README.

### Snapshot measurements

| Area | Observed state | Interpretation |
|---|---|---|
| Lists | 18 lists; 11,420 memberships | Multiple lists share lexemes as intended. |
| Lexemes | 5,046: 390 ready, 60 needs review, 4,596 new | Enrichment is incomplete; pending data is not itself a defect. |
| Dictionary coverage | 3,765 marked `in_kaikki`; 1,281 not | Missing coverage includes phrases and generated numbers; report it by source/POS, not as an error rate. |
| Senses | 476 across 450 lexemes; 26 lexemes have two senses | Multi-sense identity and display behavior already matter. |
| Nouns | 2,104; 369 without a plural, 32 without gender | Distinguish missing data from invariant, mass, and exceptional nouns. |
| Desired notes | 17,014; 668 ready, 16,346 waiting | Current ready flags do not imply independent audit approval. |
| Forms | 11,684 desired verb-form notes, all awaiting content at snapshot time | Forms still account for about 69% of desired notes. |
| Ready note types | 439 vocab; 229 noun phrases | No phrase or form notes ready in the stored projection. |
| Queue | 5,619 jobs: 450 done; 5,169 pending | Pending: 3,513 lexeme, 883 phrase, 254 verb, 515 image, 4 disambiguation. |
| Legacy state | 8,884 entries; 69,424 cards; 8,418 entry mappings | 466 legacy entries lack a mapping; absence must not automatically mean intentional retirement. |
| Knowledge / identity / audits | All three tables empty | No persisted v4 knowledge, sync identities, or current audit verdicts in this snapshot. This does not establish what the live Anki profile contains. |
| Committed lexicon export | 5,046 lexemes, all marked `new` | Export lags the database's enrichment state. |
| SQLite checks | `quick_check = ok`; no declared-FK violations | Most v4 relationships have no foreign keys, so the latter is limited assurance. Explicit scans found no orphan list/sense references at snapshot time. |

All CSVs parsed without extra columns or missing cells. This checks shape, not meaning. The configured readers still produce incorrect normalized entries from well-formed rows.

Local disk usage is approximately **4.6 GB media, 350 MB decks, 6.9 GB backups, 231 MB dictionary index**. Media contains 9,762 PNGs, 10,497 JPEGs, and 26,846 MP3s in each audio directory. There are 32 `.apkg` files and 85 SQLite backup files recursively; the current top-level retention pattern covers 10 snapshots, while older files remain outside that set.

The largest historical audit CSV contains 44,478 legacy results: 31,092 pass, 3,624 warn, 9,749 fail, 13 error. These are old model judgments, not a measured v4 error rate. They are useful candidates for regression examples after human triage.

### Strengths to retain

- Dictionary lookup and deterministic rules are separated from AI generation.
- SQLite WAL, SQL parameter binding, explicit migration versions, and consistent database snapshots are sound foundations.
- Text generation disables model tools, structured tasks declare schemas, and normal card text is HTML-escaped.
- Most media writes use temporary files followed by rename; original media is retained.
- The note builder separates desired state from Anki operations and uses semantic note keys.
- Subtitle files and derived movie datasets are ignored by Git; JSONL export and sharing explicitly remove the example field.
- Existing media is extensively reused. All **853 distinct media filenames** referenced by ready notes were present; all **418 referenced JPEGs** passed structural validation. Three sampled compressed MP3s were valid mono, 44.1 kHz audio. The sampled house illustration clearly matched its subject.

## Priority map

P0 means resolve before further destructive reconciliation. P1 means resolve before trusting a broad rebuild/backfill. P2 means the next quality and maintainability pass. Effort is relative: S = localized change; M = several modules; L = staged migration and integration work.

| ID | Priority | Recommendation | Effort |
|---|---|---|---|
| R01 | P0 | Retire only after verified replacement success | M |
| R02 | P0 | Preserve studied content until replacement is publishable | M |
| R03 | P1 | Correct input normalization and migrate affected identities | M |
| R04 | P1 | Enforce explicit ownership for every Anki mutation | M |
| R05 | P1 | Make human overrides durable and authoritative | M |
| R06 | P1 | Define complete recovery and identity contracts | L |
| R07 | P1 | Enforce relational integrity and atomic list rebuilds | M |
| R08 | P1 | Add safe queue claiming and transactional result validation | M |
| R09 | P1 | Version dependencies, AI requests, and cache entries | M |
| R10 | P1 | Apply one publication policy to all note types | M |
| R11 | P1 | Correlate image outputs with their requests | S–M |
| R12 | P1 | Separate retryable outages from terminal failures | S–M |
| R13 | P1 | Give senses stable identities and contextual evidence | L |
| R14 | P1 | Add a focused automated regression suite | M |
| R15 | P2 | Make the study plan a usable card budget | M |
| R16 | P2 | Unify movie token accounting and knowledge semantics | M |
| R17 | P2 | Make media policy explicit and versioned | M |
| R18 | P2 | Validate settings, inputs, and command outcomes | M |
| R19 | P2 | Refresh dictionary indexes atomically with provenance | M |
| R20 | P2 | Strengthen export, attribution, and privacy boundaries | M |
| R21 | P2 | Clarify module boundaries, packaging, and operations | M |

## Detailed recommendations

### R01 — Retire only after verified replacement success

**Evidence:** [reconcile.py](../flashcards/reconcile.py), `build_plan()`, `_multi()`, and `run()`; [anki.py](../flashcards/anki.py), `invoke()`.

**Reproduced:** `present_after` includes all *planned* additions. If `addNotes` returns `[None]`, `run()` records no successful identity but still calls `deleteNotes` for the legacy predecessor. An isolated run produced one failed add followed by deletion of its predecessor.

`_multi()` also discards individual action results. A simulated nested error returned normally, so subsequent operations can proceed after a failed update. The retirement ratio setting is unused; only the absolute count is enforced. A failed legacy mapping and an intentional exclusion both become `tgt is None`, which qualifies an unstudied note for retirement.

**Change:** persist an operation plan with explicit predecessor/replacement dependencies. Execute additions and updates, validate every result, read back the resulting notes/cards, and only then calculate eligible retirements. Require a recorded reason for deliberate removal. Check both absolute and ratio limits. Recheck study history immediately before deletion to narrow the planning-to-execution race. Return a failure/partial-success result when any required operation fails.

**Acceptance:** inject null adds, nested multi errors, timeout-after-success, missing mappings, and a newly reviewed predecessor. No required predecessor is deleted; a rerun converges without duplicates. Validate this in a disposable Anki profile before using it on the personal collection.

### R02 — Preserve studied content until replacement is publishable

**Evidence:** [reconcile.py](../flashcards/reconcile.py), adoption loop in `build_plan()`, `_legacy_fields()`, and media collection.

**Reproduced:** adoption is appended before `if not row['ready']: continue`. A blocked desired note still produces an adoption update. During the first sync in `run`, this can overwrite a studied note's English answer with an empty placeholder, or publish disputed content.

Adopted-only notes do not contribute media to `plan.media` when both v4 directions are suppressed. The early `continue` in that case also skips updating an existing v4 note's direction flags. Selection between studied duplicates uses the number of cards, not review history or an explicit persisted choice. `_legacy_fields()` omits v4 examples, IPA, and also-accepted content already incorporated into some fields only indirectly.

**Change:** make adoption a persisted mapping from legacy note/card and direction to a stable v4 sense. Keep the last known good content until the replacement passes the same publication gate as a new note. Gather media for every planned write. Handle existing cards when disabling templates explicitly, based on tested Anki behavior. Retain duplicate scheduling until a deliberate, separately tested consolidation policy exists.

**Acceptance:** blocked/missing-image replacements never change studied content. Fully adopted notes receive all required media. Repeated runs neither recreate duplicate study directions nor erase review history. Test adoption into a collection that already contains the v4 note.

### R03 — Correct input normalization and migrate affected identities

**Evidence:** [lists.py](../flashcards/lists.py), `_ARTICLE`, `split_article()`, `_first_variant()`, `_read_csv()`; [lexicon.py](../flashcards/lexicon.py), `_resolve()` and `_map_legacy()`.

**Observed and reproduced:** `_ARTICLE` permits zero spaces after ordinary articles and tests shorter alternatives first. It strips word prefixes and can match `un` inside `una`. Scanning configured generic CSV readers found **128 rows** with an ordinary article prefix incorrectly consumed without a word boundary:

| List | Affected rows |
|---|---:|
| Exam prep | 25 |
| italki | 30 |
| Interjections | 7 |
| Pronouns | 2 |
| Conjunctions | 11 |
| Alphabet | 1 |
| Fluent Forever | 15 |
| Tutto bene | 37 |

Examples already stored in the database:

| Input | Stored result |
|---|---|
| `Lavoro` | noun `voro`, display `la voro` |
| `lavorare` in generic lists | noun `vorare`, display `la vorare` |
| `lasciare` in italki | noun `sciara`, display `la sciara` after dictionary resolution |
| `uno` | noun `o`, display `l'o` |
| `una studentessa` | noun `a studentessa`, display `l'a studentessa` |
| `ics` | noun `cs`, display `il cs` |
| `lei` in several generic lists | noun `primo`, after stripping and dictionary resolution |

The 128-row measure covers this specific boundary defect, not every normalization issue. Explicit list POS is also overridden by article detection: alphabet `i lunga` becomes noun `lunga`. `vu / vi` and `ipsilon / i greca` become phrases because phrase detection runs before variant parsing. Only 22 of the alphabet's 26 memberships resolve to letter lexemes.

**Change:** require whitespace after standalone articles, recognize apostrophe forms separately, and preserve explicit POS where the list supplies it. Represent alternate spellings, gender pairs, and sentence stems as structured input rather than destructively selecting the first slash variant. Preserve original row IDs and normalization decisions. Quarantine implausible transformations instead of allowing dictionary fallback to legitimize them.

Fixing the parser changes lemma/POS hashes. Produce a reviewed old-ID → new-ID migration and update memberships, legacy mappings, overrides, and note identity aliases before regeneration. Do not merely delete malformed lexemes and let sync infer retirements.

**Acceptance:** regression fixtures cover all examples above, apostrophes, `il cane`, `uno studente`, `una studentessa`, explicit pronouns/letters, and slash variants. A repair report identifies every affected existing identity and its proposed destination.

### R04 — Enforce explicit ownership for every Anki mutation

**Evidence:** [reconcile.py](../flashcards/reconcile.py), `V4_QUERY`, `_legacy_query()`, tag updates, and `run()`; [commands/leech.py](../flashcards/commands/leech.py), `_decks()`, `_process_deck()`, `_update_config()`, `doctor()`.

**Code findings:** v4 queries select by notetype only, without a deck/identity ownership boundary. `leech` defaults to every deck except `Default`, and its card query has no pipeline model restriction. `doctor` starts from global `tag:leech`. Updating a deck's shared configuration can affect other decks using that preset. Reconciliation removes *all* existing tags before adding desired tags, including personal and leech tags. `ensure_model()` runs before the drift guard, so even a refused sync can modify the collection's model.

**Change:** centralize an ownership predicate based on known keys, an explicit owner tag/namespace, accepted models, and collection/profile identity. Treat imported copies and unknown keys as a conflict to inspect. Preserve non-owned tags; only reconcile a dedicated tag namespace. Scope leech commands to owned cards by default, and inspect shared preset references before altering configuration. Run every guard before any mutation, including model changes. Detect duplicate `Key` values rather than silently collapsing them into a dictionary.

**Acceptance:** an unrelated deck, copied v4 notetype, duplicate key, personal tag, and shared deck preset all survive a normal pipeline run unchanged. A drift refusal results in zero external writes and is visible in both plan and apply output.

### R05 — Make human overrides durable and authoritative

**Evidence:** [lexicon.py](../flashcards/lexicon.py), refresh of `_LEXEME_COLS` in `sync()`, `import_human_edits()`; [queue.py](../flashcards/queue.py), `_apply_lexeme()`, `_apply_phrase()`, disambiguation application.

**Reproduced:** a human display survives one sync but its provenance becomes `{}`; the second sync replaces the display with the generated value. Because a word may appear in multiple lists, repeated updates can happen inside one sync. Export then removes the lost marker from the editable artifact.

`_apply_phrase()` deletes and replaces a human sense unconditionally. `_apply_lexeme()` overwrites `english_plural` without checking its human provenance. Disambiguation can overwrite human hint/also fields. The importer records arbitrary field names as human even when it has no implementation to apply them, and cannot import forms or facts as advertised by the broad editing guidance.

**Change:** store overrides separately from generated observations, with explicit supported field paths, value, author/source, reason, and version. Compute effective values using one precedence function used by every writer. Merge provenance instead of replacing it. Validate overrides before applying them; unsupported paths must fail clearly. Export atomically so interruption cannot destroy the next run's edit source.

**Acceptance:** every supported override survives repeated syncs, multiple source memberships, AI refresh, phrase enrichment, disambiguation, export/reimport, and restore. Invalid override paths produce actionable errors rather than silently appearing protected.

### R06 — Define complete recovery and identity contracts

**Evidence:** [lexicon.py](../flashcards/lexicon.py), `export_jsonl()` and `import_human_edits()`; [db.py](../flashcards/db.py); [backup.py](../flashcards/backup.py); [reconcile.py](../flashcards/reconcile.py), identity insertion; [lexicon/README.md](../lexicon/README.md).

**Observed/code findings:** the committed export still has every lexeme marked new while SQLite contains 450 enriched lexemes. `work` and standalone `apply` do not export their new content/identities. There is no full JSONL importer or identity CSV importer. The human importer skips records not already in the database.

The export omits recoverable state such as form prompts, mistakes, mnemonics, image associations, review decisions/issues, and legacy adoption mappings. Its sense list does not preserve explicit sense IDs/provenance. The identity column named `guid` receives the numeric Anki note ID, whereas `.apkg` uses a generated GUID. These identifiers serve different purposes and cannot substitute for one another.

**Change:** choose and document the authority explicitly. A pragmatic first step is to declare SQLite plus media the authoritative state, add verified restore tooling and a recovery manifest, and describe JSONL as an editable review projection. If rebuilding from Git is a requirement, implement a versioned, complete export/import format and round-trip checks before claiming it.

Separate semantic key, actual GUID, Anki note ID, card IDs, direction, and profile identity. Back up the connection being migrated rather than assuming the global DB path. Protect schema versions newer than the application understands. Make snapshot filenames collision-resistant and test interrupted migrations. Restore media mappings along with content, and explain which cached assets can be regenerated.

**Acceptance:** a clean scratch workspace restored from the documented recovery set reproduces note keys, effective human edits, form prompts, media references, mistakes, and adoption mappings. Reconciliation against a disposable restored collection creates no duplicates. Perform a restore drill, not just a backup-file existence check.

### R07 — Enforce relational integrity and atomic list rebuilds

**Evidence:** [db.py](../flashcards/db.py), v4 table definitions; [lexicon.py](../flashcards/lexicon.py), `sync()` and `_map_legacy()`.

**Reproduced:** removing a configured list deletes its `lists` row but leaves `list_items`, because only memberships for still-present lists are cleared. A fixture retained one orphan membership. Downstream queries treat any membership as live, so removed sources can keep notes/jobs active and affect order. No orphan memberships were observed in the current snapshot; this is a lifecycle defect.

Most v4 relationships lack foreign keys and status constraints. Old `entry_lexeme` mappings are not comprehensively reconciled. A malformed source cannot be distinguished cleanly from an intentionally empty source.

**Change:** stage a complete validated source generation, then replace list/membership state in one transaction. Add foreign keys and useful `CHECK` constraints after auditing existing data. Define explicit archival/removal semantics for lexemes and aliases; preserve history while excluding inactive memberships from planning. Store source row identity, source hash, and import generation. Abort suspicious source-count drops before publishing a new generation.

**Acceptance:** removing/disabling a list, removing one row, malformed CSV input, and interruption during import leave either the previous complete generation or the next complete generation. No orphan membership can keep a note alive accidentally.

### R08 — Add safe queue claiming and transactional result validation

**Evidence:** [queue.py](../flashcards/queue.py), `drain()`, `_run_text_batch()`, `_run_image()`; [db.py](../flashcards/db.py), `ai_jobs`; [ai.py](../flashcards/ai.py), result decoding/cache writes.

**Code findings:** workers select pending jobs without claiming them. Two processes can run the same job concurrently. Most payloads are built from mutable state at execution time; `payload` and `result` do not currently provide the advertised request/result history. Text responses are applied before complete validation, and a later exception can leave partial writes that the error handler commits. Provider output IDs are not restricted to the requested subjects. Phrase results can create orphan senses because the schema lacks the relevant FK. Disambiguation marks whole groups done regardless of response completeness. An image worker returning `False` is still marked done.

**Change:** for this scale, first enforce a single writer/worker process with a clear lock. Then add atomic job claims with `running`, owner, lease expiry, attempt timestamps, and recovery of abandoned work if multiple processes are supported. Validate the complete response locally against the schema and requested IDs/forms before applying it. Apply each job/result and status transition together in a transaction/savepoint; reject partial or foreign results. Store the request fingerprint and result/artifact reference.

**Acceptance:** simultaneous workers do not duplicate claims. Crashes recover deterministically. Malformed/partial batches write no unintended content, unexpected IDs cannot modify another subject, and unsuccessful image jobs remain retryable.

### R09 — Version dependencies, AI requests, and cache entries

**Evidence:** [queue.py](../flashcards/queue.py), `_enqueue()`, `plan()`, `refresh()`; [ai.py](../flashcards/ai.py), `Task.cache_key()` and `AI.run()`.

**Reproduced:** two tasks with identical model/name/messages but different output schemas produce the same cache key. The key also omits effort and provider/tool version.

Existing senses prevent re-enrichment even when source glosses, dictionary data, or movie context change. Completed image jobs are not reopened when their files disappear. `refresh()` resets jobs but does not ask `AI.run(..., refresh=True)`; an unchanged request can reuse its old cached answer. Form prompts are keyed only by lexeme/tense/person, so a changed chosen sense can retain an old English translation. Fixed queue kind ordering finishes whole task families before reaching images, delaying usable cards for high-priority roots.

**Change:** fingerprint the normalized source observations, dictionary version, selected sense, rule version, task schema/prompt, model, and relevant generation settings. Define explicit dependencies and stale/invalidated states. Distinguish retry from force-refresh. Prioritize a small set of roots through all prerequisites to readiness, then expand the study horizon.

**Acceptance:** changing one relevant input invalidates only affected outputs; unchanged runs do no extra generation. Force-refresh bypasses cache. Deleting a completed image makes its job runnable again. A small high-priority batch can become usable before the entire lexicon is enriched.

### R10 — Apply one publication policy to all note types

**Evidence:** [notes.py](../flashcards/notes.py), `_Builder.add()`, `vocab_and_phrases()`, `verb_form_notes()`; [commands/audit.py](../flashcards/commands/audit.py); [commands/review.py](../flashcards/commands/review.py); [queue.py](../flashcards/queue.py), `_lexeme_payload()`.

**Reproduced:** after supplying form prompts and treating the image as present, all **46 generated forms of the disputed `piacere` verb** were marked ready. Form building checks for a prompt but not the lexeme's `needs_review` status. These forms were still blocked by missing prompts in the actual snapshot; the reproduction demonstrates what happens when those jobs finish.

Audit failures are displayed by `review` but never consulted by the note builder/reconciler. Audit inputs omit labels, IPA, examples, and direction flags despite consistency being an audit objective. The initial lexeme verification payload omits plural and conjugation data, so it is not a full verification of grammar. Approval changes a status without recording who approved which issue/content version. Audit `--limit` is applied before filtering already-audited rows, so repeated small runs can keep selecting the same completed prefix.

**Change:** create a shared publication decision over content validity, parent/sense status, applicable audit hash, required assets, and explicit overrides. Derivative notes inherit blocking problems. Define whether severe audits block publication or require an override, and expose that policy in the plan. Validate deterministic grammar independently and give AI audits the actual fields they are asked to assess. Select unaudited candidates before applying limits. Persist review decisions against content hashes.

**Acceptance:** a disputed parent cannot publish derivative forms, a current severe failed audit follows the configured policy, stale verdicts are visibly stale, and repeated `audit --limit N` advances through the queue. Existing reviewed Anki content remains intact while a replacement is held.

### R11 — Correlate image outputs with their requests

**Evidence:** [codex.py](../flashcards/codex.py), `_newest_image()` and `_run()`.

**Code finding:** `_run()` scans both its request directory and the shared `~/.codex/generated_images` tree, then takes the newest image after its start time. With two configured concurrent image calls, a worker can pick another request's newer image, even if its own `card.png` exists. Unrelated local generation can also qualify. The image is then permanently reused under the wrong semantic key.

**Change:** accept only an artifact explicitly returned for the invocation or its expected request-local `card.png`. If the backend cannot provide correlation, serialize that backend until it can. Validate the output and completion status, store request/artifact hashes, and use unique temporary destinations. Keep normal word/content data in a clearly delimited prompt input.

**Acceptance:** two overlapping image requests and an unrelated newer image cannot cross-associate their outputs. A failed invocation cannot succeed merely because some other image appeared in a shared directory.

### R12 — Separate retryable outages from terminal failures

**Evidence:** [ai.py](../flashcards/ai.py), `_Gate._probe()`, `_classify()`, `seconds_until_reset()`; [pool.py](../flashcards/pool.py); [cli.py](../flashcards/cli.py), `cmd_run()`.

**Reproduced:** if a retry at the parsed reset time raises a request-specific `AIError`, it exits before the `finally` that reopens the gate. The gate remains closed and other workers can wait indefinitely.

Authentication/billing errors are classified alongside temporary outages and can sleep forever. Reset parsing assumes local clock time and no timezone/date. Most worker failures are summarized but command handlers still return success. Ctrl-C sets a thread event, but in-flight `subprocess.run()` work can remain until its timeout. An unbounded queue drain delays later notes/audio/export work for the entire provider outage.

**Change:** cover every gate path with cleanup; categorize auth/configuration failures separately; add bounded runs, persisted retry times, exponential backoff/jitter, and explicit cancellation of process groups. Save completed work and export checkpoints before waiting. Return distinct success, incomplete, and failure exit codes with a resumable summary.

**Acceptance:** reset-retry failure always releases the gate; auth failures terminate with an actionable message; interrupted runs close child processes promptly and preserve completed results. No command reports full success when required work failed.

### R13 — Give senses stable identities and contextual evidence

**Evidence:** [lexicon.py](../flashcards/lexicon.py), `_pick_entry()` and `_resolve()`; [srt.py](../flashcards/srt.py), lemmatization maps; [queue.py](../flashcards/queue.py), payload selection, sense replacement, and `verb_forms()`; [notes.py](../flashcards/notes.py), sense indexing and noun phrases.

**Observed/code findings:** note identity uses the position of a sense in an AI-returned array. Reordering two senses silently attaches the old learning history to the other meaning. Both recognition cards can show the same Italian word with different expected English answers and no sense-specific context on the front. All forms use the first sense. Only three source glosses and one context line reach enrichment.

Dictionary selection takes the first matching entry; article/POS mappings collapse determiners into articles. Actual disputed records include `tuo`, `suo`, `questo`, and `tutto` tagged as articles, `ora` supplied with a noun-use context for an adverb, and `piacere` with a noun-use context for a verb. This is useful evidence of upstream ambiguity, not simply a need to approve more AI output.

Other representational losses include slash variants, multiple gender-dependent plurals, and choosing `avere` whenever the auxiliary is `both`. Noun phrases compose English with a generic `a` and use one root-level English plural; they need restrictions for mass nouns and irregular/meaning-dependent plurals. Stress/accent normalization and reflexive-class detection deserve fixtures too: removing `si` from `lavarsi` leaves `lavar`, which does not match the current `-are` classifier.

**Change:** model dictionary entries, senses, source observations, and selected teaching senses separately. Assign stable sense IDs and preserve aliases when merging. Attach context and auxiliary/valency to the selected sense. Keep uncertain POS/lemma decisions reviewable. Generate recognition prompts that provide enough context to identify the intended sense, or consolidate equivalent recognition answers. Store accepted alternatives structurally. Expand only the linguistic distinctions the current cards need.

**Acceptance:** swapping AI response order cannot change note identity; examples and form prompts stay bound to the intended sense; grammatical alternatives are preserved. A curated fixture set covers common polysemy, determiners, reflexives, dual auxiliaries, count/mass nouns, gender pairs, and accented homographs.

### R14 — Add a focused automated regression suite

**Evidence:** repository inventory, [pyproject.toml](../pyproject.toml), and isolated review checks.

There is no checked-in test suite or CI configuration. All Python files parsed successfully, and `flashcards check` passed. Pyflakes reported one unused `readline` import; its line-editing side effect is intentional, so this is a lint-policy issue rather than a runtime defect. Passing these checks did not catch the reproduced data and sync failures.

**Change:** add offline tests around public behavioral contracts, using temporary paths/databases and fake provider/Anki adapters. Prioritize the destructive paths and linguistic fixtures from this review. Add a tiny checked-in end-to-end corpus with golden note fields, migration/restore fixtures, and failure-injection cases. CI should run them on supported Python versions without credentials, Anki, or the full dictionary download.

**Minimum suite:** article/POS normalization; removed-list reconciliation; human override precedence; response validation/rollback; queue claiming and cache invalidation; studied-note preservation and failed replacement handling; template direction changes; restore fidelity; subtitle denominator accounting; CLI status codes. Keep live provider checks opt-in and disposable-profile Anki checks separate.

**Acceptance:** the reproduced failures become failing regression cases before fixes and pass afterward. A clean clone can run the offline suite without access to the user's database or private subtitles.

### R15 — Make the study plan a usable card budget

**Evidence:** [queue.py](../flashcards/queue.py), `study_order()` and `full_form_verbs()`; [notes.py](../flashcards/notes.py), sort offsets; [plan.toml](../plan.toml); [reconcile.py](../flashcards/reconcile.py), `reorder()`.

**Observed/code findings:** study order is manifest position × 100,000 plus list rank. It neither reads knowledge nor interprets deadlines. `cards.patterns` is unused. The “pattern” behavior is full forms for four model verbs, and those are not independently guaranteed to be available. Irregular verbs are added beyond the top-N limit, yielding 254 queued full-form verbs and 11,684 form notes. Large sort offsets put all vocab ahead of forms and all forms ahead of noun drills.

The 25/day setting supports a movie estimate; it does not enforce a generation budget or Anki's daily limit. Vocab can make two cards, while a root can add dozens of derivatives. Root counts are therefore not interchangeable with new-card counts. Writing due positions alone does not establish the order the user's Anki deck options will present cards in.

**Change:** make an explicit planner return selected roots, card counts, prerequisites, priority reason, and horizon. Separate introduction from optional drills. Treat deadlines and known recognition/production skills deliberately, and explain backlog estimates using actual cards. Implement or remove `patterns`. Validate scheduling assumptions with the target Anki deck options.

**Acceptance:** the plan reports the real card budget, why each item is selected, and credible deadline feasibility. Changing a deadline or knowledge state changes selection predictably. No unlimited derivative expansion bypasses the chosen horizon.

### R16 — Unify movie token accounting and knowledge semantics

**Evidence:** [srt.py](../flashcards/srt.py), `word_items()` and `token_stream()`; [commands/movie.py](../flashcards/commands/movie.py), `run()`; [reconcile.py](../flashcards/reconcile.py), `sync_knowledge()`.

**Observed:** for the local subtitle file, `token_stream()` produced 6,428 nonnumeric tokens, 73 unresolved. Coverage retains 6,355 resolved tokens, including 162 proper-name tokens, and 1,003 distinct roots. `word_items()` applies different name filtering and produced 941 roots / 6,039 tokens before lexicon resolution; the stored movie membership has 937 resolved roots. These are different populations.

Unknown tokens are omitted from the coverage denominator, inflating “100%” into “100% of resolved tokens.” Some counted names never become study items. Empty/unresolvable subtitles divide by zero. Targets already met are not handled before adding another unknown root. Estimates assume one root per daily new card.

Knowledge uses the best state across a note's cards and then, for movie reporting, across card types. Knowing one conjugation or production/recognition direction can imply knowing the root. Suspended cards can qualify by interval, actual interval/lapse/review columns are not populated, and freshness is not communicated.

**Change:** use one token-analysis output for ingestion, datasets, and coverage, with explicit statuses for resolved, unknown, excluded name, and ignored token. Show the denominator and unresolved share. Keep per-sense/per-direction skills and actual card statistics; define movie recognition coverage separately from production mastery. Display last sync time and handle empty/already-complete/overdue cases.

**Acceptance:** coverage counts reconcile exactly with the exported dataset plus explicit exclusion buckets. Empty input is safe. Learning one form does not automatically mark every sense/direction known. Estimates account for real card workload and existing coverage.

### R17 — Make media policy explicit and versioned

**Evidence:** [notes.py](../flashcards/notes.py), `_Builder.add()` and `_snd()`; [lexicon.py](../flashcards/lexicon.py), `existing_image()`; [commands/media.py](../flashcards/commands/media.py); [util.py](../flashcards/util.py), filename hashing.

Every note requires an image, including abstract grammar, letters, numbers, and mistakes. At the snapshot, **197 notes were blocked only by an image**. Audio is optional and limited to one new file per full run. Existence alone is treated as success in several paths, so corrupt/empty artifacts could become permanently reusable. The current referenced-image sample was healthy, but 417 of 418 images were 512×384, showing that reused assets do not necessarily match the new square-image policy.

Filename keys reflect text, not voice/model/speed or image meaning/style/version. Changing settings does not invalidate existing compressed output or reupload a same-named file already in Anki. `--audio --per-deck` style behavior loops over all decks through `generate_audio_per_deck`; CLI `--deck` is not forwarded on that branch. `_media_path()` can compress files during an export, making a lookup unexpectedly mutate storage.

**Change:** introduce a small asset manifest: semantic subject/sense, generation spec hash, provider, checksum, validation result, licence/source, and derivative versions. Make images required/optional/disabled per card type/list and provide deliberate fallback behavior. Prioritize audio for actual upcoming study. Keep asset resolution read-only; generate/compress in explicit stages. Preserve originals while reporting reclaimable obsolete derivatives. Consider Commons audio as a later adapter with per-file metadata.

**Acceptance:** corrupt files are detected and regenerated; changing voice/compression settings produces traceable new assets; missing optional images do not block text-ready learning; deck/global media budgets work together predictably. Verify pronunciation through listening samples separately from file decoding.

### R18 — Validate settings, inputs, and command outcomes

**Evidence:** [settings.py](../flashcards/settings.py), `_merge()` and `load()`; [lists.py](../flashcards/lists.py), manifest/CSV validation; [cli.py](../flashcards/cli.py); [commands/leech.py](../flashcards/commands/leech.py).

Settings reject unknown keys but do not enforce declared dataclass types, ranges, or cross-field constraints. Lists accept unknown extras and plan keys largely go unchecked. Generic CSV readers can silently yield no items for a wrong header, and surplus columns can fail with a low-level `.strip()` error. Several commands assume v4 tables already exist instead of using the common schema bootstrap.

Unused/misleading settings include `sync.orphan_ratio_limit`, `cards.patterns`, global `facts.enabled`, and `audit.workers` (configured as 500, while the command defaults to AI concurrency). Some CLI flags are silently ignored on alternate branches; `leech --doctor --deck` does not forward the deck. The documented instruction to run `apply` after mnemonic generation is insufficient because `apply` does not rebuild notes. `--no-ai` still permits paid audio generation. Normal default configuration also requires a private ignored movie file, so a clean clone cannot pass `check` unchanged.

**Change:** validate all configuration and input contracts before network calls or mutations. Report unknown keys, invalid enums/ranges/dates, missing headers, row numbers, empty sources, and unsupported flag combinations. Centralize DB bootstrap and command result types. Make generated-state freshness explicit: `apply` should either require a fresh materialization or build it deliberately. Provide a sample/default configuration that works without private inputs, and optional source activation.

**Acceptance:** malformed settings and inputs fail early with context; every advertised flag/setting either works or is rejected; clean-database commands behave consistently; partial pipeline failure returns nonzero. Document precisely which stages can incur external usage.

### R19 — Refresh dictionary indexes atomically with provenance

**Evidence:** [kaikki.py](../flashcards/kaikki.py), `import_stream()`, `_ro()`, `available()`, `source_etymology()`; [inputs/freqdic/README.md](../inputs/freqdic/README.md).

**Code findings:** dictionary import deletes the existing index, then commits batches into the same file. A failed refresh can leave an incomplete index and remove availability, even though a good index previously existed. Cached readers are not explicitly invalidated. Metadata records an import timestamp and URL, but no dataset digest, HTTP revision metadata, parser version, or source-entry revision. Remote lookup failures are cached as empty bodies indefinitely. Some read checks create tables/connections with write behavior. Source-etymology HTTP work runs while the queue holds the shared SQLite lock.

The committed SUBTLEX file has 15,000 rows and is described as AI-cleaned, but there is no reproducible correction script/patch log in the repo. Frequency ranking uses the first observed wordform's position rather than an explicitly defined aggregate lemma frequency.

**Change:** import into a staging database, record source/version/hash and parsing statistics, validate counts and sampled forms, then atomically switch indexes. Reopen readers after a switch. Separate read-only lookup from maintenance. Give negative remote cache entries an expiry/error category; perform remote I/O outside the main DB lock. Preserve an auditable SUBTLEX correction manifest and document ranking semantics.

**Acceptance:** failed/interrupted refresh leaves the last good dictionary available; source provenance can reproduce an enrichment request; temporary lookup failures do not become permanent absences; frequency order has a documented, tested definition.

### R20 — Strengthen export, attribution, and privacy boundaries

**Evidence:** [commands/share.py](../flashcards/commands/share.py); [notes.py](../flashcards/notes.py), templates; [lexicon.py](../flashcards/lexicon.py), exports; [lexicon/README.md](../lexicon/README.md); existing `decks/` and `audit_reports/`.

**Observed/code findings:** two inspected package samples contain the legacy `Italian Card Model` (2,072 notes in the sampled CILS package, 4,800 in the sampled passato-prossimo package). Package filenames do not indicate the content generation/version. Sharing silently omits missing media and checks only `ready`, not audit/review policy. Deck descriptions carry a generic Wiktionary attribution string, while the note `Source` field names lists rather than dictionary entry/version and is not rendered by the templates.

Removing `Example` is a useful privacy measure, but it is not a full export boundary: AI senses, notes, and facts can reflect private contexts; practice mistakes are included in an unrestricted share; imported source text has differing publication rights. JSONL provenance does not identify individual source records or extraction versions.

**Change:** add a versioned export manifest with build/source hashes, note counts, media checksums, and content policy. Default shared exports to explicitly shareable sources/card types and apply the same quality gate as sync. Validate every media reference. Persist dictionary entry URLs, source licences, and retrieval versions, and render a compact source attribution where appropriate. Review actual source reuse permissions before publishing; this review did not perform a legal/licensing audit. Keep private examples, practice data, and provider payload logs outside public exports unless deliberately selected.

**Acceptance:** an offline package validation checks note/template counts, GUID uniqueness, media completeness, attribution metadata, and absence of private fixture strings across *all* exported fields. Export artifacts clearly identify whether they are legacy or v4 and which source generation produced them.

### R21 — Clarify module boundaries, packaging, and operations

**Evidence:** imports across [notes.py](../flashcards/notes.py), [queue.py](../flashcards/queue.py), [lists.py](../flashcards/lists.py), [srt.py](../flashcards/srt.py), [anki.py](../flashcards/anki.py), and [cards.py](../flashcards/cards.py); [paths.py](../flashcards/paths.py); [pyproject.toml](../pyproject.toml); [scripts/_venv.sh](../scripts/_venv.sh).

The module sizes are manageable, but responsibilities cross boundaries: note building imports queue planning functions, readers import subtitle code that imports reader models, Anki transport imports legacy rendering, and media lookup can trigger compression. Shared constants/presentation helpers live in legacy modules, making it difficult to remove v3 safely. Global settings and source-root paths make isolated tests and non-editable installs awkward; only the DB and settings paths can be overridden.

Direct Python dependencies are pinned, but transitives/build tooling are not locked. The bootstrap upgrades pip when the project hash changes. Models are mutable aliases, and external CLIs/ffmpeg/AnkiConnect versions are not captured, so “reproducible” currently means less than the comments imply. Backups lack a tested restore command; the optional mirror is disabled. Secret filenames are correctly ignored in the current tree, but that is not a historical secret scan.

**Change:** retain one package but introduce clear responsibilities:

| Boundary | Responsibility |
|---|---|
| Domain | Lexeme/sense/source models, Italian rules, publication and scheduling policy |
| Application services | Ingest, enrich, build, review, plan, apply, export, restore |
| Persistence | Repositories, schema migration, transaction boundaries, snapshots |
| Adapters | Kaikki, CSV/SRT, Claude, image backend, ElevenLabs, AnkiConnect, filesystem |
| Presentation | Typed note fields, HTML templates, CLI reports |
| Legacy compatibility | v3 schema/models/mapping/adoption, isolated until migration is proven complete |

Extract shared models and pure planning functions first; avoid a large directory-only rewrite. Pass an application context containing paths/settings/clock/adapters so tests and dry runs are fully isolated. Support an explicit workspace root. Add a dependency lock/constraints workflow and record external tool versions in each run manifest. Add a `doctor`/health command, structured stage summaries, restore documentation, and optional off-machine backup verification. Update stale references to `sources.json`, old modes, and unsupported guarantees after behavior is fixed.

**Acceptance:** offline services can run entirely in a temporary workspace; installation behavior is documented and tested; each CLI command has a clear side-effect contract; legacy removal has explicit completion criteria; a health report distinguishes missing credentials/tools, stale projections, blocked reviews, and pending work without starting a backfill.

## Implementation sequence

1. **Protect the collection:** implement R01/R02/R04 with mocked failures and disposable-profile tests. Preserve existing content and disable automatic retirement until the invariants pass. Take a consistent DB backup and an Anki collection backup before the eventual real migration.
2. **Repair the data foundation:** fix R03 and generate an identity-preserving repair plan; implement R05/R07 and stable sense aliases from R13. Review changed mappings before applying them.
3. **Make state recoverable:** implement R06 with a restore drill. Establish one authoritative state model and atomic, complete checkpoints.
4. **Make generation dependable:** implement R08–R12, then use a small curated corpus to exercise each input/card type through the entire pipeline. R14 accompanies every stage rather than waiting until the end.
5. **Improve learning and output quality:** implement the study horizon, movie accounting, media policy, source-aware sharing, and validated CLI behavior in R15–R20.
6. **Simplify internals:** apply R21 incrementally after the behavioral contracts are covered. Archive obsolete artifacts and remove legacy code only after adoption/recovery verification.

A useful first milestone is one small end-to-end corpus that can be imported, reviewed, published, rerun without unintended changes, deliberately interrupted, and fully restored. Bulk AI work and new features become safer once that milestone is reliable.

## Verification record and follow-up boundaries

| Check performed | Result |
|---|---|
| Python AST parse, all 34 modules | Passed |
| `.venv/bin/python -m flashcards check` | Passed for this local workspace |
| `.venv/bin/python -m pyflakes flashcards` | One intentional side-effect import warning (`readline`) |
| All 17 CSVs and both JSONL files | Parsed; no CSV overflow/missing cells |
| SQLite snapshot integrity and declared FKs | Passed; missing v4 constraints remain a design gap |
| Article-boundary scan | 128 affected rows across eight lists |
| Two-sync human display fixture | Override marker lost, then value overwritten |
| Human phrase result fixture | Human sense overwritten by AI application |
| Removed-list fixture | Orphan membership retained |
| Blocked adoption fixture | Adoption still planned |
| Failed-add retirement fixture | Legacy deletion still executed after null replacement result |
| Nested Anki action error fixture | Error ignored by `_multi()` |
| Disputed-verb fixture | 46 forms became ready after prompts/images were supplied |
| Reset-time retry failure fixture | Availability gate remained closed |
| Schema-only cache change fixture | Cache key unchanged |
| Ready-note media references | 853 distinct names present |
| Referenced JPEG validation | 418/418 passed |
| MP3 decode metadata samples | 3/3 valid |
| Existing package inspection | Two readable legacy collections |

The probes ran as inline scripts against in-memory fixtures and a consistent database snapshot stored outside the repository under `/tmp/italian_repo_review_20261009`. Temporary data is not a durable deliverable. The report records the fixture conditions and expected regression behavior so these checks can be turned into maintained tests.

Remaining integration work is explicit: real Anki failure/rollback and template tests in a disposable profile; a complete restore rehearsal; opt-in provider smoke tests; rendered-card checks on desktop/mobile/night mode; listening-based pronunciation sampling; and expert review of a stratified linguistic sample. None should be mistaken for checks already completed here.
