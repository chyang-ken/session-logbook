# Agent handoffs keep an action index and hide successful result bodies

## Decision

The standard anchored transcript keeps complete User and Assistant messages plus one-line tool
actions with their target path, search scope, or command prefix. Successful result bodies collapse
to status and size. Leading error text remains visible. `[L#]` anchors remain the way to retrieve
hidden details from the raw Session.

## Rationale

A live long-running Session measured on 2026-08-26 contained about 2.16 million rendered
characters after the previous truncation. Tool calls and result bodies contributed about 1.35
million characters. Replacing successful result bodies with status and size while retaining each
action reduced the simulated handoff to about 1.45 million characters. This kept the facts an Agent
needs to avoid repeating work: which files were read or changed, which searches ran, and which
commands were attempted.

The measurement used private Session data and therefore is intentionally not committed. The
aggregate numbers above contain no Session content. Synthetic regression tests reproduce the
observable contract without private data.

## Alternatives and why rejected

- **Keep truncated result bodies:** retained about 710,000 more rendered characters, dominated by
  file contents and command logs that remain available through `evidence`.
- **Collapse all tools to counts:** reached about 880,000 characters, but removed file targets and
  command prefixes needed for an engineering handoff.
- **Remove all tool traces:** saved little beyond tool-count summaries and broke precise discovery
  of operational evidence.

## Evidence

- `tests/test_anchored_transcript.py` verifies that successful payloads are hidden, errors remain
  visible, tool actions stay anchored, and User/Assistant messages remain complete.
- `tests/test_session_logbook_cli.py` verifies cursor overlap and bounded evidence expansion.

## Commit

Implemented by the commit that adds this decision record.
