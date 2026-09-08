# Read Devin Local directly from SQLite

## Decision

Support Devin Local, excluding legacy Cascade and cloud sessions. The user explicitly
selected Devin Local and authorized implementation on 2026-09-08. Read the database
in SQLite read-only mode, including committed WAL data; do not require export files,
install hooks, launch the IDE, or copy private transcripts into this repository.

Follow `sessions.main_chain_id` through `message_nodes.node_id` and `parent_node_id`.
A missing or cyclic chain is an error; do not concatenate alternate histories.
Use namespaced session IDs and database record references in the existing source
path field. N anchors address `message_nodes.row_id`, scoped to the session.
Follow returns a full snapshot because the selected chain is mutable.

## Rationale and alternatives

The inspected local installation contained a sessions database but no transcript
exports. A JSON-export-only reader would miss its conversations. Reading every row
in timestamp order would mix abandoned retries with the current conversation.
Using JSONL line anchors or an append-only cursor would falsely imply that earlier
content cannot change. Generated permanent JSONL copies would add another store
and would weaken direct evidence provenance.

The implementation uses Python's standard library and existing neutral UI tokens.
Only metadata previews use the dashboard's existing warm cache. Its invalidation
includes both the database and WAL file signatures, separate from session activity
used for recency. No new service or dependency is needed.

## Evidence and references

- `tests/test_devin_source.py`: synthetic WAL visibility and source read-only checks,
  selected branches, broken chains, tool failures, node evidence, full-snapshot follow,
  role-filtered search, cache refresh, hidden records, and encoded HTTP IDs.
- [agentsview Devin parser](https://github.com/kenn-io/agentsview/blob/main/internal/parser/devin.go):
  reference for the sessions/message_nodes storage shape and selected-chain semantics.
- [Devin Local documentation](https://docs.devin.ai/desktop/devin-local):
  Devin Local shares its harness with Devin CLI and differs from Cascade.

The adapter was implemented here; no external code or private transcript fixtures
were copied. macOS local storage was checked; other operating systems were not run.

## Commit

The implementing commit includes this decision. Resolve its full identity with
`git log --diff-filter=A --format=%H -- docs/decisions/2026-09-08-devin-local.md`.
