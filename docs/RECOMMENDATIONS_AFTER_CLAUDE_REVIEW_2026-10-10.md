# Recommendations after Claude’s pipeline rework

Review date: 10 October 2026. Implementation and validation updated the same day.

Claude’s changes improve the run workflow and remove real bottlenecks. Keep the
single `./run.sh` entry point, bounded parallel Claude batches, source-change
detection, faster Anki reads and clearer progress reporting. The implementation
now adds the approval, study-history and generated-card safeguards below.

These recommendations follow the [earlier data and application review](DATA_AND_APP_RECOMMENDATIONS_2026-10-09.md)
and the [performance investigation](PERFORMANCE_REVIEW_2026-10-10.md). References
to D01–D21 below use the identifiers from the earlier data review. The shared
planning changes complete the D12 implementation described in this review;
this does not mark every item in the earlier D01–D21 review complete.

## Implementation status

The code changes for all seven numbered items and the media/recovery follow-up
are implemented. Content adjudication and production integration checks remain
separate work, as described below. Sections 1–7 preserve the original findings
and acceptance criteria so the changes can be checked against the review.

| Item | Implemented behavior | Evidence |
|---|---|---|
| 1. Approvals | Review rows carry a content hash; stale approvals cannot release changed content. Pending marks survive exports. Approvals entered during a run are processed before the final build and sync. | Stale/current approval and late-approval pipeline tests. |
| 2. Meaning identity | Dictionary and source evidence accompany new senses. New meanings receive new identities; superseded meanings remain archived. Explicit human rewording of an existing index requires `same_meaning: true`. Existing unverified identities stay identifiable as legacy data. | Meaning replacement, paraphrase and source-identity regression tests; schema v7 migration on a database copy. |
| 3. Grammar | Generation uses meaning-specific person, auxiliary and noun-form constraints. Each requested verb form requires an answer or a reasoned exclusion. Known unsuitable published drills are held or suspended while retaining study history. | Verb completeness, exclusions, reflexive/auxiliary, plural-gender and linguistic corpus tests. |
| 4. Replacement verification | Retirement checks the actual replacement, including adopted studied notes, its expected content and card direction. Adoption is written and checked before the original direction is disabled. | Verified/missing replacement and interrupted-adoption tests. |
| 5. Sync completion | A rejected sync is not recorded as complete. The final sync reconciles Anki again, and sorting changes affect the local signature. | Partial-sync failure, successful final retry and signature tests. |
| 6. Shared planning | Generation and publication use the same candidate plan, count new card directions and maintain published verb families. Movie forecasts distinguish vocabulary coverage, card workload and introduction days. The horizon remains opt-in. | Shared horizon generation/publication and workload forecast tests. |
| 7. Audits and fixtures | Audit ordering prioritises risky cards; reports separate audited and unaudited notes. Versioned `cue_review.csv` supports reviewed hints and accepted alternatives. Import warnings flag likely swapped language columns. A small cited linguistic corpus covers source readers and card types. | Cue correction, language-direction, reader/card-type and full pipeline fixture tests. |
| Media and recovery | A resumable checksum inventory retains every original. Recovery bundles can include code, dictionary, inputs, pipeline state, an Anki collection and media. Restore verifies copied bytes in a new directory. | Complete local media inventory and an isolated restore/rebuild fixture that preserves Anki review history and media. |

### Validation results

- **106 tests pass**, with Ruff lint and formatting checks passing.
- A fresh database snapshot migrated to schema v7 with integrity `ok` and no
  foreign-key violations. No production database migration, AI call or Anki
  write was performed by this validation.
- Snapshot timings: migration **0.62 s**, source resolution **4.94 s**, queue
  planning **2.58 s**, and note building **1.53 s**. These measure local work,
  not provider latency or live Anki throughput.
- A repeated source pass skipped unchanged inputs; repeated queue planning
  added **zero jobs**. All **2,692** finished lexeme enrichment jobs and **765**
  finished phrase jobs remained finished. Three previously finished verb jobs
  were reopened for incomplete or unsuitable results instead of being silently
  treated as valid.
- The rebuilt snapshot had **8,337 publishable notes**, **238 cue-review rows**,
  and **65 grammar holds**. Publishable does not mean independently audited:
  all **8,337** still lacked a matching independent audit. **3,574** existing
  senses retained explicitly unverified source identities.
- All **73,956 files** under `media/` were inventoried: **73,951** valid assets
  and **5** metadata files. No referenced media was missing. **53,064** files
  without a recorded reference were retained. A final comparison found no
  missing, added, resized or modified files relative to the initial baseline.
  All files were hashed and images decoded; audio validation did not include
  full audio decoding. A repeated scan reused all inventory records.

Snapshot data was captured at **2026-10-10 07:23 UTC**. A separate live worker
can continue changing production counts. Compact machine-readable evidence is
in [REVIEW_IMPLEMENTATION_EVIDENCE_2026-10-10.json](REVIEW_IMPLEMENTATION_EVIDENCE_2026-10-10.json).

### Remaining operational work

1. Run `./run.sh` with Anki open to exercise the migration, provider responses
   and real sync. The implementation checks above used isolated database copies
   and simulated provider/Anki responses; they do not certify a production run.
2. Review the generated `review.csv`, `cue_review.csv` and `grammar_review.csv`.
   Grammar holds require an underlying meaning or grammar correction; a root
   approval alone is not a substitute. Legacy source identities need reviewed
   dictionary/source matches over time. Existing finished vocabulary is not
   regenerated wholesale just to populate these fields.
3. Run targeted independent content audits and adjudicate ambiguous cues before
   interpreting publishable counts as language-quality assurance. The checked
   linguistic fixtures are small regression examples, not a certification of
   the whole generated corpus.
4. Make a recovery bundle with the actual Anki collection and media, and rehearse
   opening its restored collection in an isolated Anki profile. The automated
   restore fixture verifies stored history and offline rebuilding; it does not
   launch Anki or install external runtimes and provider credentials.

No media regeneration is required for these changes. See the [README](../README.md)
for review-file instructions and the inventory/recovery commands.

## Original review scope and evidence

The review inspected the current code, ran the repository’s 73 tests, rebuilt a
fresh database snapshot offline and used isolated reproductions for the findings
below. All 73 repository tests passed. Passing tests do not establish that the
new run workflow or generated language content is fully correct.

The rebuilt snapshot had:

| Observation | Count |
|---|---:|
| Publishable notes | 7,654 |
| Publishable notes with a matching independent audit | 0 |
| Active senses without a source ID | 3,115 |
| Groups with identical written production cues but different answers | 71 |
| Requested forms omitted from the completed `accadere` job | 20 |

Different images may distinguish some of the 71 prompt-collision groups; they
are review candidates, not 71 proven mistakes. Counts describe the review
snapshot and can change while the live worker continues.

The original review made no production database changes, Anki writes, Claude
calls or media changes. The findings below describe that baseline; the status
and validation section above records the subsequent implementation.

## 1. Fix the approval workflow

**Priority: P1. Related recommendations: D09, D20.**

`review.csv` approvals are matched by lexeme ID without checking whether the
content is still the version the user reviewed. An isolated reproduction wrote
an approval, changed the word’s grammatical data and issue, then imported the
old approval. The changed word was released and its new issue cleared.

A second reproduction confirmed an ordering problem: approvals entered during
a long run are applied after the final card build and Anki sync. The run exited
successfully and removed the approved word from `review.csv`, but its generated
note remained blocked until another run.

Recommended changes:

- Include a content version or hash in each review row and validate it on import.
- Require another review when the content or issue has changed since export.
- Apply approvals before final planning, note building and syncing. Report any
  newly queued dependencies that must wait for another run.
- Preserve unprocessed marks and distinguish approval from correction of an
  underlying grammar, meaning or source error.

Acceptance: a stale approval cannot release changed content, and an approval
accepted during a run either reaches the final build/sync or is clearly reported
as still pending. It must not silently disappear while its cards remain blocked.

Relevant code: [review importer](../flashcards/commands/review.py),
[`pipeline.run`, final stage](../flashcards/pipeline.py).

## 2. Protect studied identities when meanings change

**Priority: P1. Related recommendations: D03, D04, D10.**

Sense matching still assigns an unmatched new meaning to an existing sense
index. An isolated reproduction replaced a prompt with a completely different
meaning and retained the same identity. This can transfer existing study history
to different content without deleting a studied card.

All 3,115 active senses in the snapshot lacked a source ID. The application
therefore cannot reliably distinguish a wording revision from a change in the
underlying meaning using persisted source identity.

Recommended changes:

- Introduce stable meaning IDs backed by dictionary entries and source evidence.
- Preserve the existing identity for a wording revision of the same meaning.
- Give genuinely new meanings new identities; require an explicit reviewed
  mapping before transferring a studied identity to changed semantics.
- Archive superseded meanings and retain their history and media references.

Acceptance: paraphrasing and reordering preserve the intended identity, while
replacement with an unrelated meaning cannot inherit study history automatically.

Relevant code: [`save_senses`](../flashcards/overrides.py),
[legacy target mapping](../flashcards/reconcile.py).

## 3. Correct generated grammar before expanding the corpus

**Priority: P1. Related recommendations: D01, D02.**

The rebuilt snapshot still contained ready cards such as
`succedo → “I happen / I am happening”` and
`succediamo → “we happen / we are happening”`. Morphologically possible forms are
being combined with meanings that do not suit those forms.

The completed `accadere` job also omitted 20 requested forms. The current
response handling accepts usable forms without recording whether each omission
was a deliberate exclusion or incomplete output.

Recommended changes:

- Add meaning-specific restrictions for persons, auxiliaries and constructions.
- Keep “this form exists” separate from “this form is suitable for this meaning.”
- Require each requested form to have either a valid prompt or an explicit
  exclusion with a reason.
- Model noun gender and plural features at the appropriate form/meaning level,
  rather than relying on one scalar gender for all derived exercises.
- Rebuild affected notes for review while preserving existing media and history.

Acceptance: impersonal meanings, modal imperatives, reflexives, dual auxiliaries
and gender-changing plurals have reviewed fixtures. An incomplete response cannot
silently mark a whole verb complete.

Relevant code: [verb response validation](../flashcards/queue.py),
[verb derivation](../flashcards/planning.py),
[dictionary feature extraction](../flashcards/kaikki.py).

## 4. Finish automatic duplicate cleanup and replacement verification

**Priority: P1. Related recommendations: D10, D11.**

An isolated reproduction found a missed cleanup case: both directions already
have studied legacy homes, so no v4 note is needed, but an unstudied duplicate
is not scheduled for removal. Candidate selection still requires a possible v4
note even when an adopted legacy note serves the direction.

Existing adopted replacements can also qualify through the planner’s `served`
set without being freshly verified at retirement. The checks on the note being
deleted are useful and should remain; the replacement needs equivalent care.

Recommended changes:

- Recognize adopted legacy homes as replacements even when both directions are
  adopted and no v4 note is created.
- Verify the replacement’s continued existence, intended content and matching
  direction before deleting its unstudied predecessor.
- Retain the existing ownership and study-history checks immediately before
  deletion, and continue preserving every studied note and all media.
- Add integration tests for both-direction adoption, rejected replacements,
  interrupted adoption and a replacement disappearing mid-sync.

Acceptance: eligible duplicates are removed only after their actual replacement
is confirmed; a planner flag alone is not treated as confirmation.

Relevant code: [`build_plan` and `_retire`](../flashcards/reconcile.py).

## 5. Make sync skipping account for incomplete syncs and external changes

**Priority: P2. Related recommendations: D11, D17.**

The pipeline records a signature even when Anki rejects additions. An isolated
reproduction confirmed that the final sync is then skipped if local notes have
not changed. The signature also excludes card order and cannot detect changes
made inside Anki.

Recommended changes:

- Cache completion only after a fully successful reconciliation.
- Retain pending operations after a partial result so later stages can retry.
- Include card ordering in the relevant signature.
- Give the final stage a lightweight external-state check so “up to date” reflects
  Anki’s state as well as unchanged local fields.
- Preserve the faster bulk reads and legacy-note index.

Acceptance: a rejected addition is not hidden by the signature shortcut, an
ordering-only change is recognized, and relevant external changes are detected
without restoring repeated full scans of all legacy notes.

Relevant code: [pipeline sync shortcut](../flashcards/pipeline.py),
[`notes.signature`](../flashcards/notes.py).

## 6. Finish D12 with one shared card plan

**Priority: P2. Related recommendations: D06, D12.**

AI selection budgets one or two cards per root, while publication budgets actual
note directions, including drills. These calculations can disagree. The selected
conjugation families can also rotate as vocabulary becomes known, instead of
maintaining already-published families independently of new-work priority.

The movie estimate of “185 more words → 90%, approximately eight days” divides
roots by the daily card limit. It does not account for multiple card directions,
derived drills or the time needed to learn and retain those words.

Recommended changes:

- Use one explicit plan for generation, publication and workload forecasts.
- Count actual new card directions and their required generation dependencies.
- Maintain already-published verb families separately from the selection of new
  families to study.
- Distinguish estimated vocabulary coverage, actual card workload and learning
  time in movie reports.
- Keep the study horizon opt-in, preserving the current full-corpus default.

Acceptance: a configured horizon selects a consistent set of generation jobs and
publishable cards, learning a vocabulary card does not unexpectedly abandon its
existing conjugation family, and forecasts explain their counting assumptions.

Relevant code: [study planning](../flashcards/planning.py),
[note horizon](../flashcards/notes.py),
[movie forecasts](../flashcards/commands/movie.py).

## 7. Add targeted content audits and regression fixtures

**Priority: P2. Related recommendations: D07, D08, D18.**

All 7,654 publishable notes in the rebuilt snapshot lacked a matching independent
audit. Enrichment’s verification flag is not an independent audit of every
derived card. There were also 71 groups with identical written production cues
but different expected answers.

Recommended changes:

- Start audits with conjugations, ambiguous prompts, gender-changing plurals and
  the corrected café inputs.
- Add meaningful distinctions or accepted alternatives for retained prompt
  collisions, taking the visible image and learning objective into account.
- Add language-direction checks that flag another swapped-column import, with
  review rather than automatic rejection of legitimate loanwords or mixed text.
- Maintain a small, reviewed linguistic regression corpus across every source
  reader and card type.
- Exercise the complete run workflow in tests, including the approval, partial
  sync and retirement cases above, alongside the existing unit tests.
- Report audited and unaudited denominators separately from publishable counts.

Acceptance: changes to parsing, dictionary selection or prompts expose which
reviewed examples changed and why. A passing unit suite is accompanied by
evidence about output quality and full-run behavior.

Relevant code: [data review](../flashcards/commands/data_review.py),
[audits](../flashcards/commands/audit.py), [tests](../tests/).

## Follow-on work: preserve and verify expensive media and recovery

**Priority: P2, after the immediate correctness work. Related recommendations: D14, D16.**

Build an inventory of existing assets with checksums, references and known
provenance. Verify them in a resumable scan. Report missing or invalid assets
without deleting or overwriting originals, and retain unreferenced media.

Test a complete restore that includes dictionary dependencies and Anki study
history, not only the pipeline database and exported notes. Clearly distinguish
a database backup, a local pipeline recovery bundle and full Anki recovery.

Acceptance: the existing expensive media can be located and verified after
restore, and the recovered application can rebuild its local state with the
required dictionary data and preserve the learner’s review history. Regenerating
media is not a prerequisite for these improvements.
