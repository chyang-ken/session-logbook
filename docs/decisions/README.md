# Decision Log

Design decisions that rest on an experiment, a comparison, or a measurement are
recorded here as `YYYY-MM-DD-<topic>.md`, so the *why* survives long after the diff.

Each entry should capture:

- **Decision** — what was settled.
- **Rationale** — the philosophical fork and the key data behind the call.
- **Alternatives & why rejected** — often harder to reconstruct later than the decision itself.
- **Evidence** — links to the script / raw output / report that backs it (keep these reproducible).
- **Commit** — the implementing commit hash, filled in once done.

When a decision is backed by an experiment, reference it from the commit message:
`Decision: docs/decisions/<file>`.

> This log starts fresh with the open-source release. The pre-release design
> rationale lives in [`../philosophy.md`](../philosophy.md) and
> [`../design-system.md`](../design-system.md).

## Index

Newest first. Filenames are the stable reference; this list exists so a reader who has
never seen the work can tell which record answers their question.

### Session identity — what counts as one conversation, and what counts as one turn

These nine were written over two days by parallel branches and are best read together.

- [`2026-09-21-compaction-summary-and-record-level-previews.md`](2026-09-21-compaction-summary-and-record-level-previews.md)
  — the client's compaction summary is not something the person said; previews and search
  ask the record, not the text.
- [`2026-09-20-claude-human-turn-rule.md`](2026-09-20-claude-human-turn-rule.md) — one rule
  answers "did a person say this?" for the reader, the card count and `[U#]`.
- [`2026-09-20-claude-event-records.md`](2026-09-20-claude-event-records.md) — the three
  Claude records that are events, not speech, and how each is shown without being counted.
- [`2026-09-20-conversation-identity.md`](2026-09-20-conversation-identity.md) — a record is
  one transcript and keeps its id forever; a conversation is the ordered set of records a
  rewind, resume or cross-file compaction produced. Identity is additive.
- [`2026-09-20-codex-subagent-completeness.md`](2026-09-20-codex-subagent-completeness.md) —
  a thread Codex spawned owns its whole record; its parent is lineage, not a missing prefix.
- [`2026-09-20-antigravity-rewind-history.md`](2026-09-20-antigravity-rewind-history.md) — an
  Antigravity rewind never leaves the file; which rows are live, and how the abandoned ones
  stay reachable.
- [`2026-09-20-antigravity-anchored-and-cli.md`](2026-09-20-antigravity-anchored-and-cli.md)
  — the same live history in the anchored export and the Agent CLI.
- [`2026-09-20-claude-follow-delta.md`](2026-09-20-claude-follow-delta.md) — why an opt-in
  delta follow, and why plain `follow` still returns the whole branch.
- [`2026-09-19-claude-copied-history.md`](2026-09-19-claude-copied-history.md) — shared
  history is evidence, never grounds to resolve a Session to a different id.

### Retrieval, sources and scope

- [`2026-09-19-search-matching-lines.md`](2026-09-19-search-matching-lines.md)
- [`2026-09-17-continuation-integration.md`](2026-09-17-continuation-integration.md)
- [`2026-09-15-runtime-observation.md`](2026-09-15-runtime-observation.md)
- [`2026-09-13-retrieval-over-lifecycle-management.md`](2026-09-13-retrieval-over-lifecycle-management.md)
- [`2026-09-08-devin-local.md`](2026-09-08-devin-local.md)
- [`2026-09-05-project-path-anchor-fix.md`](2026-09-05-project-path-anchor-fix.md)
- [`2026-08-26-agent-action-index-reduction.md`](2026-08-26-agent-action-index-reduction.md)
