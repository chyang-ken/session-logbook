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

## Codex continuation cursors

Codex Desktop can resume the same `session_meta.id` into another rollout. ID lookup
selects the latest segment by its native metadata timestamp, including with a warm
HTTP cache; file size and filesystem modification time do not establish recency.
Explicit paths still read exactly that file. Context is the selected segment, not
a reconstruction of inherited history from `history_base` or the Desktop database.

Save `transcript_path` alongside the numeric conversation `NEXT_CURSOR` and
`native.next_line_cursor`. Pass it back as `--cursor-source-path` on `observe`
and `follow`. Physical `[L#]` anchors remain local to that file.

For Codex, a verified same-thread source change resets both cursors and reports
`cursor_reset_reason: transcript_changed`. Legacy nonzero cursors without a
source path report `cursor_source_missing` and replay the selected segment once.
Subsequent calls with the returned path are incremental, including native pages.
Consumers must clear old turn state on a reset, and never compare line numbers
across source files. An unrelated source or truncation is an explicit error.

The additive conversation `SOURCE_CURSOR` and native `source_cursor` fields
provide opaque `cx1:...` tokens for callers that prefer one value. These may be
passed unchanged as cursor arguments without `--cursor-source-path`. Existing
numeric output fields retain their types. Native ordinals may overlap across
segments and are not used to splice history. No cursor proves process liveness.

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

Basic real lifecycle and independent Agent consumption checks are recorded below.
No stable cross-source monitoring claim yet. No resident
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

### Multi-turn, interruption, and recovery follow-up

The maintainer subsequently restored Kimi authentication. Each source was exercised
with two short replies, an interrupted request, and a recovery request in the same
session. Claude, Codex, and Kimi used separate CLI invocations to resume the session;
Pi used consecutive RPC requests in one process, including the native abort command.
No tools, permanent installations, authentication changes, or model overrides were
performed by this test. SIGINT targeted only the test-owned process groups.

| Source | Actual result | Boundary |
|---|---|---|
| Codex | Two completed turns, an interrupted turn with matching turn_aborted identity, and a completed recovery turn | Native rollout path verified, not Logbook-owned Hook delivery or Desktop coverage |
| Claude | Two completed turns; SIGINT interrupted the third; same-session recovery and a plain greeting both received provider rejection | Recovery not passed; no model change or repeated attempts to bypass the provider response |
| Pi | Two completed turns, abort, and successful recovery; the modified extension was loaded in a new real process and verified again | agent_settled fires after abort too; agent_end now exposes native stop_reasons without message bodies |
| Kimi | After user login, two completed turns and successful post-SIGINT recovery | The interrupted turn left no turn.ended; it must remain unknown from logs alone, not inherit the preceding completion |

A separate Claude run used a temporary Stop blocker. It emitted Stop with
stop_hook_active=false, continued to produce the requested extra line, then emitted
Stop with stop_hook_active=true. Both observations reached Logbook. This validates
that receiving the first Stop is not proof the runtime has stopped continuing.

Codex native events were additionally read in two-event pages: all eight lifecycle
events were returned once, then an empty page. Pi exposed stop, stop, aborted, stop
in the four agent_end observations. A fresh Agent, without the construction discussion,
found the installed Skill wrapper, read observe, distinguished the unfinished third
request from the successful fourth reply, and returned independent cursors without
modifying or resuming the inspected session.

Remaining acceptance gaps include Claude post-interruption provider recovery,
Codex-owned Hook delivery and Desktop coverage, permission waits, Pi automatic
retry/compaction and queued follow-ups, and abrupt termination without cleanup.
These are not grounds for converting missing events into a completion claim.

Local private reproduction inputs and source references are retained in
`_private/runtime_probe.py`, `_private/runtime-probe-results.json`, and
`_private/runtime-probe-followup.json`; these are intentionally excluded from Git.
