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
