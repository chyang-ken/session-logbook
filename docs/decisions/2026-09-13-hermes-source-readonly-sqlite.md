# Hermes sessions are read from the live SQLite store, not from an export

## Decision

Hermes Agent becomes a fourth session source, read directly and read-only from
`~/.hermes/state.db` (SQLite, WAL mode) — never from an export file. A Hermes session is
addressed as a `state.db#<session id>` pseudo-path because the dashboard keys every session
by its `jsonl_path` string; `[L#]` anchors cite `messages.id`, the row id that `evidence`
expands from. Profile stores (`~/.hermes/profiles/*/state.db`) are discovered alongside the
default home.

## Rationale

Hermes stores every conversation in one SQLite database (`sessions` + `messages` tables,
chat-completions message shape) rather than one jsonl file per session. That storage shape
decides the integration: a source adapter (`sources/hermes.py`) opens the store with SQLite
`mode=ro` plus `query_only`, so it can never corrupt or write the live agent's state, and WAL
mode lets readers work without blocking the running agent. The scan stays incremental by
comparing the newest message timestamp per session, and the aggregate listing query makes
repeated polls on an idle store cost a file stat.

## Alternatives and why rejected

- **Consume `hermes sessions export` JSONL dumps**: requires a manual or scheduled export
  step, goes stale between runs, and adds a dependency on the Hermes CLI. The dashboard's
  premise is live observation of local data.
- **Have Hermes write per-session JSONL transcripts**: changes another tool's storage layout
  for this dashboard's convenience; the dashboard is a read-only consumer of existing data.
- **Copy `state.db` to a temp file per scan**: guards against locking that WAL readers do not
  need; the copy adds I/O per poll and still races the live store.

## Evidence

- `tests/test_hermes_source.py` builds synthetic stores and asserts listing, metadata,
  conversation pairing, transcript export, message-id anchors, search AND semantics,
  evidence reads, and the server/CLI integration paths.
- A read-only dogfood pass over a live store (scan, conversation, anchored render, search,
  and the full CLI) ran during development; it also surfaced and fixed one real search bug
  (a per-row AND prefilter dropped sessions whose terms live in different messages). Private
  session content was never committed.

## Commit

Implemented by the commit that adds this decision record.
