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
