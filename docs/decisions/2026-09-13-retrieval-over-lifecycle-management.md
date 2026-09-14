# Retrieval, not lifecycle management, is the product core

## Decision

Session Logbook's primary job is to help one person find and re-read their own historical Agent
Sessions. The main journey is `time or search -> Session Detail -> reuse`.

Agent clients own the lifecycle of active Sessions. Session Logbook will not ask the user to keep
every historical record manually organized. Existing project groups, Starred, Recent, Dusty,
Archived, and notes may remain as secondary compatibility tools, but they no longer define the
product's main path.

Future retrieval work follows three rules:

1. Prefer a cross-project time view and search over project-first navigation.
2. Exclude explicitly identified sub-agent logs as standalone human Sessions. When identity is
   uncertain, label and filter them only as **suspected non-human Sessions**, disclose the filter,
   and let the user reverse it by confirming human participation.
3. Add local metadata only when it directly improves retrieval. A personal title is the first
   example; it supplements rather than replaces source data and does not classify a Session.

Implement these rules with the smallest maintainable mechanism. Edge cases are handled when they
cause a real failure, not through a speculative classification system.

## Rationale

Months of regular use changed the original premise. Session volume grew beyond what could be
managed one record at a time, so lifecycle controls stopped being the durable habit. The repeated
useful behavior was finding a Session, usually through search or recency, and then reading Session
Detail.

Project-first grouping fragments that recovery path. Conversely, hiding every one-turn Session is
useful for reducing automated CLI noise but cannot establish who participated. Treating the same
signal as suspicion, showing that a filter is active, and accepting a direct human correction keeps
the product honest without requiring a perfect classifier.

Personal titles solve a narrower retrieval failure: the user has found the right Session but knows
the source title will not help next time. They are retrieval metadata, not a return to ongoing
manual catalog maintenance.

## Alternatives and why rejected

- **Keep the four-zone dashboard as the product core:** still assumes that the user will maintain a
  historical working set, which regular use did not support.
- **Classify human and automated Sessions automatically:** available source metadata is incomplete,
  and a one-turn rule is evidence rather than identity.
- **Show every source record by default:** lets high-volume automated runs crowd out the
  conversations the user remembers.
- **Build a richer metadata and lifecycle system:** increases maintenance without addressing the
  primary retrieval path.

## Evidence

- The maintainer's product review on 2026-09-13 summarized several months of regular use. The
  private Session history is intentionally not committed.
- The existing product already has full-text search and a full Session Detail reader; these became
  the repeated entry and destination in actual use.
- `sources/session_identity.py` records explicit source relationships while keeping a one-turn
  interaction classified as unknown, which supports suspicion and correction rather than a hard
  identity claim.

No private transcript content or personal corpus statistics are included in this record.

## Commit

Recorded by the commit that adds this decision.
