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

## Opt-in Claude conversation deltas

`observe --delta` keeps Hook and native event cursors unchanged. With a positive,
source-attributed conversation cursor and a verified Claude branch, it returns
`conversation_follow_mode: delta_selected_branch`, `conversation_delta_from`, and
`conversation_removed_lines` (physical lines at or before that cursor). The body
repeats the cursor line and then includes only newer selected content. Consumers
must replace that repeated anchor, retire content derived from removed anchors,
and invalidate any pending decision based on removed content.

The initial read, an unverified branch, an unattributed multi-record conversation,
and an explicit source switch retain the full-branch reconciliation response.
A file changed during a delta read is an error; do not advance any cursor. Plain
`observe` retains its prior contract, and other source types are unchanged.
This option does not migrate a supervision target or decide whether a task ended.

## Claude user controls

Claude's native stream now exposes only two selected-history facts: `user_input`
and `user_interrupted`, with a physical line, original timestamp and control ID.
It reuses the reader's human-turn and exact interruption-marker rules; peer
messages, sidechains, compaction summaries and rewound records do not count as
human input. Events have no message body and never assert task completion.
An unverified selected branch returns no controls and
`collection: selected_history_unverified`; this is uncertainty, not an empty
verified history. Native cursors advance only over complete records.

A supervisor can silence an explicit user interruption until a later native
human input. Late Stop/tool Hooks cannot release that pause. After new input,
compare its timestamp with Hook `observed_at` before using a prior Stop: an old
completion cannot complete a new user turn. Keep all three cursors independent.

## Codex continuation cursors

Codex Desktop can resume the same `session_meta.id` into another rollout. ID lookup
selects the latest segment. The shared history reader then follows explicit
`history_base` references, validating both `end_ordinal_exclusive` and
`end_byte_offset`. It retains only the inherited prefix before each cutoff, followed
by the selected continuation. Overlapping ordinals in replaced tails are not used
as deduplication keys. No history is inferred from timestamps alone.

`context`, the HTTP reader, and exports include verified inherited context. Each
record retains its physical source path and line. Explicit paths identify that
segment and its inherited context; `evidence` still reads only the exact raw file.
A fork may inherit a parent prefix only when its fork metadata agrees with the
history boundary. Parent activity after the cutoff and child-agent sessions are
not part of the selected task. Native observation excludes inherited parent events.

Save `transcript_path` alongside the numeric conversation `NEXT_CURSOR` and
`native.next_line_cursor`, passing it back as `--cursor-source-path`. Observation
drains the unread effective tail of the saved segment and every intermediate
segment before reaching the latest. `native.has_more` includes remaining segments,
so drain pages before interpreting the latest task state. Hook cursors are not reset.
A page may name an earlier segment until its unread content has been delivered.

Switching a page's source reports `cursor_reset_reason: transcript_changed`.
Consumers clear old line and turn state, while retaining previously collected
conversation. Numeric cursor output types remain unchanged; `cx1:...` source tokens
are also accepted. A bare nonzero cursor cannot identify a prior segment: its
migration is explicitly marked `cursor_source_missing` and replays verified history.
Persist the returned source identity so later polls remain incremental.

Missing segments, conflicting boundaries, malformed records, and unfinished lines
are reported with source locations. Context and HTTP responses expose
`context_complete`/`history_issues` (text uses `CONTEXT_COMPLETE`/`CONTEXT_ISSUES`).
The anchored export displays `CONTEXT_INCOMPLETE`; the web reader shows a warning.
`observe` fails explicitly with `context_incomplete` rather than advancing cursors
through missing evidence. A cursor inside a replaced tail also requires context
reconciliation; it is not silently reset or treated as successfully read.

No cursor proves process liveness or completion of the user's goal. Runtime facts
remain evidence for a consuming Agent to interpret.

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

## Rebuildable Codex history index

Codex `observe`, source-qualified `follow`, and reader change checks use
`~/.session-logbook/history-index.sqlite3`. The standard-library SQLite index
stores source identities, validated byte/line/ordinal coordinates and prefix hashes,
not message bodies or consumer cursors. Each caller must still retain its independent
Hook/native/conversation cursors and transcript source path. Reading the index never
acknowledges delivery, so retrying an old caller cursor returns the same evidence.

Unchanged sources reuse verification. Appends verify the current segment's old prefix;
old segments stay unread. A large single segment still needs its full prefix checked.
Replacement, missing ancestry, changed candidates or conflicting copies invalidate the
plan. Incomplete sources never advance observation. Concurrent callers use transactions;
a locked, corrupt or unwritable index falls back to the uncached verified snapshot.
A structurally corrupt SQLite file is bypassed, not automatically overwritten. Deleting
this derived index allows a rebuild; never delete original logs to repair it.

`SESSION_LOGBOOK_HISTORY_INDEX=/absolute/path` isolates experiments; `off` disables
the index. Synthetic source roots do not use the resident index by default. An index
rebuild incurs extra reads once; warm observation still pays metadata discovery and
small requested evidence reads. The `history_cache` observation field reports `hit`,
`append`, `rebuilt`, `disabled`, or `unavailable`; it is not a runtime task status.
