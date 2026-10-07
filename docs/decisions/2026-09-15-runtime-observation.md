# Runtime observation for 0.2.0

## Decision and authorization

The maintainer explicitly approved runtime observation as part of 0.2.0, including
the installation and maintenance cost, then approved implementation. Initial sources:
Codex, Claude Code, Kimi Code, and Pi. Devin keeps retrieval but is outside runtime
observation scope. This supersedes the retrieval-only restriction for observation;
it does not authorize client lifecycle control, messaging, or permission decisions.

Code collects facts and coordinates. The consuming Agent interprets semantic
completion using conversation evidence and the single session-logbook Skill.

## Implementation

- `scripts/record_runtime_event.py`: observation-only Hook collector, no stdout
  decisions. Writes allowlisted metadata into a local SQLite journal. Raw prompts,
  tool outputs, credentials, and reasoning are not copied.
- `scripts/setup_runtime_hooks.py`: preview/status/install/uninstall for Claude and
  Codex JSON configuration. Removes only entries with the Logbook ownership marker.
- `scripts/logbook-pi.ts`: optional Pi extension, preserves native event names,
  serializes delivery, records both agent_end and agent_settled plus heartbeats.
- `observe`: retrieves independent journal, native-record, and transcript cursors.
  No UI or resident server is required to collect or read Hook events.
- Kimi uses its existing durable wire events; no Kimi Hook installer is needed.

## Evidence and tradeoffs

Local installed Pi 0.85.1 documentation distinguishes agent_end from agent_settled:
retries, compaction, and queued follow-ups can continue after agent_end.
Open Island provides a useful comparison, but no code was copied:
https://github.com/Octane0411/open-vibe-island

We do not translate missing heartbeats or permission rejection into successful
completion. Hook receipt order is local observation order, not a guaranteed
cross-process causal order. Missing native turn IDs remain missing. Journal storage
does not prove a Hook is installed everywhere or that a process is alive.

## Validation and release gate

Synthetic tests cover session isolation, paging, incomplete native records, and
preserving blocked outcomes. Real source-by-source startup, interruption, failure,
continuous-turn, recovery, and independent Agent consumption checks remain required
before advertising stable runtime monitoring. Delivery status is in docs/runtime-observation.md.

Follow-up real-client validation found that Pi agent_settled also fires after an
aborted request. Preserve the native assistant stopReason values in agent_end
metadata rather than interpreting settled as success or forcing extra raw-log reads.
The updated extension was verified with real normal, aborted, and recovered requests.
Kimi SIGINT left no terminal event, demonstrating why missing evidence must remain
unknown. Source-specific outcomes and remaining gaps are in the delivery document.
