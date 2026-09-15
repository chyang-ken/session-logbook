# Runtime observation (0.2.0 implementation in progress)

`observe` provides runtime facts alongside anchored conversation for an Agent to
interpret. It does not decide whether the user's goal was achieved.

```sh
python3 session_logbook_cli.py observe '<session-id-or-path>'
python3 session_logbook_cli.py observe '<session-id-or-path>' \
  --event-cursor 12 --native-line-cursor 340 --cursor-line 340
```

The three cursors address different streams; do not interchange them. Hook results
are bounded to 50 events per page (up to 200 with `--limit`). Drain `has_more` before
judging the latest state. Conversation follows existing full-text/branch semantics.
These cursors belong to the current source files and journal. If either is replaced,
restored, or deleted, discard its saved cursor and start that stream at zero. They
are not durable identities across storage replacement.

## Collection setup

Use a stable checkout path. Hook configuration points to this checkout and Python
interpreter; moving either requires reinstalling. Existing unrelated hooks remain.

```sh
python3 scripts/setup_runtime_hooks.py preview --source claude
python3 scripts/setup_runtime_hooks.py install --source claude
python3 scripts/setup_runtime_hooks.py status --source claude
python3 scripts/setup_runtime_hooks.py uninstall --source claude
```

Use `--source codex` for Codex. This edits hooks.json only; it does not change feature
flags, trust, or permissions. The installed Codex must support and enable native
Hooks. Restart the source client to load changed configuration. `status` checks
configuration presence only; actual delivery must be verified in the journal.

For Pi, load `scripts/logbook-pi.ts` with `pi -e <absolute-extension-path>`.
The installed Pi must support agent_settled (verified in local 0.85.1 documentation).
For Kimi, `observe` reads existing turn.prompt/turn.ended wire events directly.
Devin runtime observation is not supported.

Events are saved in `~/.session-logbook/runtime-events.sqlite3`. Set
`SESSION_LOGBOOK_EVENTS` to an isolated file for tests; collector and reader must
use the same override. Recording works without the dashboard. Failed recording
emits a diagnostic to stderr and never blocks permissions or emits approval output.

## Current delivery status

Implemented: metadata journal, independent cursors, native Codex/Kimi facts,
Claude/Codex configuration utility, Pi extension, Agent observation guidance.

Pending release acceptance: real lifecycle tests for each source and an independent
Agent using the Skill. No stable cross-source monitoring claim yet. No resident
service is installed. Heartbeats are exposed as facts, not converted to liveness.
Legacy explicit_terminal is historical metadata, not an authoritative current-turn
verdict; use the event identities and conversation in observe.

### Real CLI smoke tests, 2026-09-15

Tests used temporary observation storage and did not install permanent hooks.

| Source | Observed result | Not yet verified |
|---|---|---|
| Claude Code 2.1.272 | Temporary hook configuration delivered SessionStart, UserPromptSubmit, Stop, SessionEnd | Blocked Stop, permissions, interruption, continued turns |
| Codex 0.154.0 | Real rollout delivered task_started and task_complete with matching turn_id | Logbook-owned hook delivery, Desktop coverage, interruption |
| Pi 0.85.1 | Explicit extension delivered session_start, before_agent_start, agent_start, agent_end, agent_settled, session_shutdown | Retries, queued follow-ups, interruption, heartbeat expiry |
| Kimi Code 0.36.1 | Existing authentication failed; wire reader preserved turn.ended reason=failed | Successful authenticated turn and recovery |

Kimi authentication was not changed. The failure case is evidence for preserving
failure, not evidence that a successful Kimi lifecycle has been validated.
