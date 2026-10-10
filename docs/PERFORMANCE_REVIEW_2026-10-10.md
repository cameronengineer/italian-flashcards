# Overnight run: diagnosis and fixes — 10 October 2026

The run is making progress, but combines hours of provider quota waiting with
avoidable queue failures and serialized calls. This is not an image-generation
bottleneck: `[run] image_limit = 0` skips generation entirely. Signing into Codex
does not supply capacity for text jobs, which this application sends to Claude.

This review used the supplied terminal log, current source, a consistent read-only
snapshot of the live database, profiling on isolated copies, and offline regression
tests. No paid provider request, Anki mutation, or production database migration was
performed for this diagnosis. The running worker continues to use its older code;
the changes below take effect after restart.

## Evidence

At the first snapshot, 6,672 notes were ready and 5,482 were waiting. Those are note
counts, not AI-request counts: a single root can produce multiple notes and verb
forms. There were 1,739 pending lexical jobs, 187 pending phrase jobs, 94 pending
verb jobs, three pending disambiguation groups, and 15 lexical jobs in flight.
Counts will change while the existing worker continues.

| Cause | Evidence and effect |
|---|---|
| Claude quota pauses | The log explicitly waits for resets at 15:20, 02:40 and 07:40, plus an earlier 160-minute wait. `max_unavailable_minutes = 0` intentionally allows indefinite waiting. More concurrent calls cannot remove account quota limits. |
| Missing verb inputs | 116 unfinished verb jobs had SQL NULL payloads: 90 pending and 26 failed. Most pending verbs were not enriched yet. Parsing those inputs raises the repeated `NoneType` error before an AI request. A batch failure also penalizes otherwise valid members. |
| Invalid cached responses | Fifteen lexical jobs had exhausted three attempts with the requested-ID error. The AI client cached schema-valid responses before checking whether their IDs matched the request, allowing the same rejected answer to be reused. |
| Serialized calls | The reviewed queue called one batch at a time even though `settings.toml` specifies concurrency 4. Small batches plus sequential calls lengthen active processing. |
| Repeated table scans | Planning each batch scanned all source observations for each of thousands of roots. SQLite used the list/row primary-key index, which cannot efficiently find one root's evidence. |
| Full-corpus scope | No study horizon is configured: roughly 5,000 roots across 18 lists are in scope. `--batches 20` means 20 batches **between Anki syncs**, not 20 batches total. `--once` stops after one round. |
| Waiting notes are not all runnable | The log includes 665 notes blocked by images while new images are disabled, and hundreds requiring review. Finishing the text queue cannot make all of those notes ready. Reasons can overlap. |

The older “exactly every requested verb form” check has already been relaxed in
the source to accept usable requested forms. The running process still printed
that obsolete error. Its output format also predates the current batch timing
messages. Python imports remain resident until the process is restarted.

## Changes implemented

1. **Recover the known failures once.** Schema v6 creates the source-evidence
   index and returns jobs exhausted by the three identified legacy errors to the
   pending queue. It retains their previous response/error evidence. The normal
   pre-migration backup remains in place. Reopening is a one-time migration, not
   an endless retry loop.
2. **Plan before dispatch.** Eligible jobs receive current payloads; premature or
   obsolete jobs are cancelled without deleting their records or media. On the
   isolated snapshot, planning left zero pending jobs with NULL payloads.
3. **Validate before cache reuse and insertion.** Queue and audit requests enforce
   semantic checks, including exact requested IDs, through the AI client. Invalid
   cached responses become misses; invalid fresh responses are not cached as
   successes. Valid caches still avoid provider calls. Malformed cache JSON is
   recoverable.
4. **Honor concurrency safely.** Up to four independent batches run in each wave.
   Input updates, cache writes, result application and error accounting share one
   SQLite lock. The process-level writer lock stays held throughout. Network
   etymology lookups run outside the database lock. A wave must finish before the
   planner selects the next one; this is bounded concurrency, not a promise of a
   fourfold end-to-end speedup.
5. **Keep interruption recoverable.** Cancellation signals provider subprocesses,
   joins the workers before releasing ownership, preserves completed transactions,
   and returns unfinished claims to pending. A currently running request can need
   repeating. Images run one at a time and retain the configured per-run cap.
6. **Isolate failed batches on retry.** Subsequent attempts run one job per call,
   retaining the five-minute retry delay and three-attempt limit. Changed or
   revived inputs clear obsolete retry delays. Unrelated failed jobs remain failed.
7. **Reduce repeated local work.** Re-plan once per wave and export JSONL once per
   drain, including interruption, instead of rewriting exports after every batch.
   Each successful batch still commits its database results immediately.
8. **Expose progress without blocking the worker.** `./run.sh jobs` now only reads
   the database. It shows counts, in-flight owners/start times, the last completion,
   retry cooldowns and common errors. It does not acquire the writer lock, migrate,
   re-plan or consume provider capacity. Worker output identifies each batch and
   gives elapsed times and round totals.

## Measurements and verification

Both profiling runs used copies of the same database snapshot and the same inputs.
No AI or Anki calls were involved.

| Measurement | Before | After |
|---|---:|---:|
| Initial queue planning, profiled | 12.54 s | 1.96 s |
| Subsequent planning, unprofiled | Not measured | 0.73 s |
| Source-evidence lookup plan | Full index scan | Indexed search on `lexeme_id` |
| Pending jobs with NULL input after recovery/planning | 304 in initial snapshot, across text and images | 0 |

The initial planning comparison is approximately 6.4 times faster; filesystem and
image-validation caches can affect timings. It does **not** measure provider or
Anki throughput. The original JSONL export itself took only 0.16 seconds, so avoiding
its repeated writes is a secondary improvement. There is no defensible completion
ETA until actual post-restart throughput and provider availability are observed.

Validation: 70 offline tests pass, including overlapping provider calls, exact
batch/image budgets, selective migration recovery, cache poisoning, per-job retries,
atomic result application, interruption recovery and read-only status. Ruff,
compilation and whitespace checks pass. The fixture database helper isolates JSONL
exports in temporary directories. One test initially exposed an export isolation
gap; the affected generated files were restored from a consistent database snapshot.

All 73,956 baseline media files remain present with unchanged sizes and modification
times. No media was regenerated or deleted. Database and Anki content were not
modified by this investigation; generated JSONL exports were restored as described.

## Resume with the fixes

Stop the existing worker with **Ctrl-C**, wait for the shell prompt, then resume the
same command you were using:

```sh
./run.sh --allow-retire
```

The next invocation backs up and migrates the database, repairs the known failures,
replans dependencies, and resumes from completed results. The earlier data-review
changes can legitimately reopen a small number of completed jobs whose source
evidence changed; the isolated snapshot reopened 58 such jobs. Existing responses
and media are retained.

From a second terminal, inspect progress without disturbing the worker:

```sh
./run.sh jobs
```

For a bounded first run, add `--once --batches 20`. For less frequent full Anki
reconciliation, a larger `--batches` value is available, but this review did not
measure Anki's share of wall time and did not change that default.

## Remaining recommendations

- Measure real per-batch throughput after restart, separating provider waits,
  etymology fetching, planning, note building and Anki reconciliation. Persist
  provider wait reasons/next-probe times and heartbeat information; current status
  shows job state, not a reliable provider ETA.
- Keep the requested wait-and-retry behavior for quota, login and network failures.
  The model remains `opus`; no quality or model-cost tradeoff was changed silently.
- Use an explicit study horizon only if completing a smaller near-term corpus is
  preferable to preparing every list. That changes scope, not processing speed.
- Resolve image and human-review blockers deliberately. Enabling images consumes
  generation resources; it is not a cure for slow text processing. Existing media
  should continue to be reused.
- Retain rejected provider responses in a separate diagnostic archive, outside the
  reusable success cache. This follow-up records errors but does not add that archive.
