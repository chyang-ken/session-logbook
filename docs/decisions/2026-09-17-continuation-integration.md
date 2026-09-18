# Combine independently developed Codex continuation fixes

## Decision

Retain the implementation delivered by PR #20 as the shared retrieval layer.
Integrate the additional independent regression cases for nonstandard filenames,
Hook cursor preservation during a source reset, and exact-path historical evidence.
The maintainer explicitly requested comparing and combining both fixes, followed
by the existing staging delivery process.

## Rationale

Both implementations selected a newer same-ID segment and associated observation
line cursors with its path. PR #20 additionally covers search and dashboard
selection, HTTP cache invalidation, source-qualified follow cursors, and native
pagination. It validates same-thread continuation before accepting a source change.
The narrower observation implementation does not improve those shared behaviors.
Consumer-side turn reset and notification deduplication remain the consuming
supervisor's responsibility, outside this read-only repository.

## Alternatives

Applying both implementations wholesale would replace the broader implementation
and remove existing validation. Retaining two resolvers would introduce conflicting
source selection. Preserve one implementation and combine the complementary tests.
Do not import filesystem modification time as a recency fallback: a touched old
record is not evidence of a newer segment.

## Evidence

- PR #20 and its synthetic tests in `tests/test_codex_continuation.py`.
- This integration adds three independent boundary cases to the same test suite.
- Run `python3 -m unittest discover -s tests` and
  `python3 scripts/check_no_cjk.py`; Python 3.9 is also required before push.

## Commit

The commit introducing this record also introduces the integrated regression cases.

## Follow-up: effective history, not just the latest segment

Further inspection showed that selecting the latest file solved visibility but
not continuity. The integration now adds one shared history resolver, used by the
web reader, exports and CLI. It validates explicit ordinal and byte boundaries;
keeps the inherited prefix; excludes replaced tails; and preserves physical origins.
This also distinguishes a verified fork prefix from unrelated child-agent activity.

Observation drains every unread effective segment using the existing numeric
cursor plus source-path contract. Missing or conflicting history blocks observation
with an explicit diagnostic. A cursor already inside a replaced tail requires
reconciliation rather than silent replay. Context views expose incomplete status.

Additional evidence is in `tests/test_codex_history.py`: goals and restrictions,
three-segment catch-up, replacement overlap, fork boundaries, cross-segment tool
results, missing segments, and partial writes. The installed supervision consumer
was also exercised against isolated synthetic segments without sending messages.
Local real-history readback and browser evidence remain private, outside Git.

Delivery is paused at the maintainer's explicit request: local implementation and
validation only; no merge or deployment of this follow-up.

## Identity, evidence pointers, and retrieval cost

The maintainer requested checking displayed IDs and file paths across related
features, and using pointers and demand-driven retrieval to reduce overhead.
The selected Session ID remains stable. The latest segment path is a separate
field; historical evidence includes its physical source and line range. Browser
copy/share/export and CLI/web search now use that distinction. Effective-history
search excludes replaced tails and supports terms spanning segments.

The packaged Skill instructs callers to retain source-qualified cursors, retrieve
bounded evidence by exact source path, and avoid repeating full context requests.
It does not promise that incremental output means incremental disk reads. The
resolver still verifies history on each invocation; fully demand-driven storage
reads remain unfinished. Installed Skills and resident services are unchanged.

The resolver now builds boundary indexes once per invocation and traverses ancestry
iteratively. Synthetic fresh-process measurements on the development machine:

| Input | Before | After | Peak memory after |
|---|---|---|---|
| 100 segments, 64.76 MB, 16,100 records | 1.313 s | 0.499 s | 116.8 MB |
| 1,100 segments, 0.52 MB, 2,200 records | timed out at 55 s | 0.252 s | 20.2 MB |

These measure resolution, not an entire monitoring cycle or arbitrary future
workloads. The deep-chain case is a regression test. A supplied effective page
manifest also prevents redundant history resolution and incorrect cutoff labels.
The installed monitor consumer passes isolated catch-up and quiet-poll checks;
each observation runs in a fresh process with retained numeric cursors and paths.
Actual browser checks covered copying IDs, current and historical paths, sharing,
export, and a fourth continuation with an unchanged ID and refreshed source list.

Validation: 353 tests on Python 3.9 and 3.14, one existing skip on each. Delivery
remains paused; neither these local changes nor the packaged Skill are deployed.

## Follow-up: demand-driven ancestry boundaries

Probe the record ending at the declared byte boundary before loading a candidate.
Only a matching candidate's retained prefix is parsed and validated. Discarded tails
remain untouched, and conflicting valid prefixes still block history resolution.
This uses per-invocation indexes only; it introduces no persistent cache or service.

Synthetic acceptance verifies that an 8 MB discarded tail costs less than 16 KB
of explicit binary reads, preserves access to original evidence, and rejects an
invalid prefix even when its endpoint is valid. Long boundary records and 1,100
segments also pass. The complete suite reports 355 tests on Python 3.9 and 3.14,
with one existing skip; installed-consumer isolated catch-up remains correct.

This reduces unnecessary disk reads but does not reuse previously verified history
across separate process invocations. Cross-process reuse would need a recoverable
local index or a caller-carried validation checkpoint, with explicit invalidation
on changes to history. Neither is implemented or enabled by this change.

## Isolated cross-process index experiment (not enabled)

The maintainer's approved local performance investigation continues while release
is paused. The experiment in `tests/history_index_prototype.py` has no production
entry point and refuses state paths outside its named temporary test directory.
Run `python3 tests/history_index_experiment.py`; its fixtures and index are removed
on exit. It uses standard-library modules only and never installs or starts a service.

### Existing cache assessment

`server.py`'s scan cache stores session-card metadata and project lookup information.
It does not store verified ancestry boundaries or prefix hashes. Reuse its policy
(rebuildable, atomic replacement, no writes on unchanged data), not its current
format: a CLI writer sharing that JSON file could overwrite the dashboard's data.
The experiment therefore uses a separate explicit temporary index.

### Measured result

Every request starts a fresh Python process against 100 synthetic segments totaling
67,805,992 bytes. The index contains file identities, sizes, timestamps including
ctime, a verified current-file byte/line/ordinal boundary and prefix hash, plus a
format version and checksum. It contains no transcript body or generated summary.

| Request | Source bytes read | Reader time (Python 3.14) |
|---|---:|---:|
| Cold verification | 69,328,856 | 0.4304 s |
| No changes, new process | 0 | 0.0057 s |
| One appended message, new process | 678,330 | 0.0094 s |
| Retry after result was not delivered | 678,330 | 0.0071 s |

The initial index is 22,598 bytes. Append reads decrease by 99.02% relative to cold
verification. These counters measure bytes returned by application reads of source
JSONL files, not physical device I/O. They exclude reading the small index and stat
calls. Reader timing excludes Python startup and result serialization. An unchanged
poll still lists/stats source files; metadata work scales with file count.

### Single-large-segment control

Run `python3 tests/history_index_experiment.py --single-segment`. With the same
roughly 65 MB in one segment, a quiet check still reads no source body, but append
verification reads 67,781,170 bytes: the full old prefix plus the new message.
Cold setup reads 135,562,026 bytes because it both parses and hashes the initial
segment. Reusing the in-progress hash removes a redundant second prefix read on
append. This does not make a mutable large file append-only or remove the need to
verify its prefix. Do not generalize the multi-segment 99% result to single files.

### Correctness and invalidation

- Unchanged file inventory and signatures allow reuse without reading source bodies.
- File growth alone is insufficient. Rehash the current segment's old prefix before
  accepting appended rows; retained historical files can stay unread.
- Same-size ancestor edits, even with restored mtime, and atomic replacements trigger
  full reconstruction. New segments also rebuild conservatively.
- Missing ancestry or unfinished records report incompleteness without advancing the
  stored checkpoint. Restored files are revalidated.
- Deleted or corrupted indexes rebuild from source; they are never the only copy.
- The cache checkpoint is distinct from the caller's delivery cursor. Repeating an
  older caller cursor after an undelivered result returns the same appended message.
- Every successful rebuild/delta is compared with the uncached resolver, including
  physical origins; no semantic summary substitutes for original messages.

The experiment covers 14 cold/warm/mutation/recovery requests on Python 3.9 and
3.14, plus the four-request single-large-segment control on Python 3.14. It is a single-writer
prototype, not an integrated CLI/HTTP cache. Production concurrency, eviction and
consumer integration have not been implemented or accepted by this experiment.

### Recommendation and actual user boundary

The measured benefit supports a rebuildable index. For a production implementation,
use a separate `~/.session-logbook/history-index.sqlite3` under the existing Logbook
state directory, with standard-library SQLite transactions for multiple readers and
writers. Keep shared file metadata once and keep caller cursors outside the index.
Do not add backups or a service. Deleting the index should cost only a cold rebuild;
source histories must never be deleted or changed. Storage grows with indexed file
metadata, not transcript text; the prototype's JSON size is not a SQLite size claim.

This is a concrete proposed runtime artifact, not an enabled location. The only
remaining user decision is approval to activate that additional local derived state
as part of a future release. Release is still paused; no production index, installed
Skill update, push, merge or deployment has occurred. The isolated experiment itself
is complete and does not require an additional approval to investigate.
