# Antigravity in the anchored export and the Agent CLI

## Decision

Antigravity becomes a full source of the anchored transcript and of the read-only Agent CLI.
`render_antigravity` formats rows that `sources/antigravity.py` normalizes; the CLI's
`locate`, `context`, `follow`, `status`, `evidence` and `search` accept the source. Three
sub-decisions carry the design:

1. **The renderer owns no Antigravity parsing.** `antigravity.collect_turns()` returns the
   live history as normalized rows with their physical lines, the way `pi.collect_turns()`
   already does; the renderer only formats. One place decides what a rewind abandoned.
2. **`follow` is cursor-based and reports what a rewind took back**, in the same
   `# REMOVED_BEFORE_CURSOR: L1-L3` vocabulary that the Claude delta follow uses
   (`2026-09-20-claude-follow-delta.md`), never a second convention.
3. **One path predicate.** `is_antigravity_path()` replaces four
   `str(path).startswith(str(AG_BRAIN))` checks in `server.py`, matching `is_codex_path` /
   `is_kimi_path`.

## Rationale

### The export was wrong in two visible ways

`GET /api/sessions/<antigravity id>/anchored` fell through to `render_claude`, which finds
no Claude records in an Antigravity transcript. It returned the digest header alone --
about 1.6 KB, no turns -- and that header called the session a *Claude Code* session and
appended a Claude-only `HISTORY_ISSUE: ambiguous_or_missing_session_id`. The CLI simply
refused: `unsupported or unrecognized session transcript`. An agent handed an Antigravity
session had no reduced form to read at all.

### Why `follow` reports removals instead of resending the branch

A Claude or Pi rewind makes `follow` return the whole selected branch, because the reader
must reconcile against a history that changed shape. An Antigravity rewind is different in
one respect that matters: it never leaves the file, and the rows keep their physical line
numbers. Rendered anchors therefore stay strictly ascending, so an append-only cursor still
selects the right suffix. The single thing a cursor cannot express is that an appended
rewind retroactively dropped rows the follower already received.

Naming those lines is strictly more useful than resending everything: the follower learns
which anchors to retire, and pays for the tail only. Antigravity's live-history rule is
always decidable (`rewind_plan` fails safe by keeping too much, never by guessing), so the
field is never `unknown` the way Claude's can be.

Only lines **at or before the cursor** are reported. Lines a rewind abandoned past the
cursor were never delivered, and naming them would ask a reader to retire anchors it never
held.

### A bug this work surfaced

The cursor filter matched `[L#]` anywhere in a rendered line. User and Assistant text is
preserved in full, so a message that quotes an anchor -- pasting an anchored transcript into
a conversation does exactly that, and one real local Antigravity session contains it --
moved the cursor to the quoted number and silently dropped the rest of that message from
the follow. Only a line's leading anchor counts now. Kimi follows were affected too.

## Alternatives and why rejected

- **Full-snapshot follow, as for Claude and Pi:** their rewinds change physical coordinates
  or leave records unverifiable; Antigravity's do not. Resending a whole transcript to
  express "three early lines went away" costs the reader the entire body on every poll.
- **A second removal vocabulary (a JSON field, a different header name):** an agent
  following both Claude and Antigravity would parse two spellings of one idea.
- **Rendering abandoned rows with a marker:** the reader, the export, search and the card
  previews all agree that an abandoned branch is not part of the conversation. A fourth
  surface disagreeing would be the drift the live-history rule exists to prevent. The header
  states the count and the anchors, and `evidence` still reads any raw line.
- **Parsing Antigravity rows inside the renderer:** CLAUDE.md section 5 forbids restating
  source parsing outside its adapter, and the rewind rule is exactly the thing that must not
  be restated.
- **Keeping `startswith(AG_BRAIN)`:** a sibling directory whose name merely begins with
  `brain` matches that prefix. The same class of bug is why `is_codex_path` exists.

## Evidence

- `tests/test_antigravity_rewind.py` -- `AnchoredRendererTests` (plain transcript, single
  rewind, stacked rewinds, the write-order artifact that abandons nothing, tool/result
  anchoring, an errored result, the digest header with and without a rewind, an empty user
  row, and every anchor resolving to its own row) and `CommandTests` (each CLI command,
  follow across a rewind that lands after the cursor was taken, a rewind past the cursor,
  a message quoting an anchor, evidence on an abandoned line, search).
  `tests/test_session_logbook_cli.py` covers the same commands beside the other sources;
  `tests/test_antigravity_source.py` covers the path predicate's directory boundary.
- Nine deliberate breakages -- renderer reading every row, renumbered anchors, the predicate
  reduced to a string prefix, `scan_sessions` binding its root at import, `REMOVED_BEFORE_CURSOR`
  losing its cursor bound, the header dropping the abandoned block, search reading abandoned
  rows, `detect_source` losing its content sniff, an empty user row counted as a turn -- each
  fails at least one test. Reverting the anchor-regex fix fails the quoted-anchor test.
- Real-data read-back over all **73** local Antigravity transcripts, read-only, with the
  dashboard's state and cache paths rebound to a temp directory: **73/73** render without
  error; the anchored `[U#]` count equals the web reader's user-turn count for every session
  (**272** turns total); **3043** anchors resolve to the physical row they name, **0**
  mismatched; both rewound conversations carry `Abandoned by rewind: N raw lines at <ranges>`
  and no abandoned line is rendered anywhere. The larger of the two is 59 raw lines of which
  a rewind abandoned 56 -- previously a header-only export. Report (contains real session
  ids, so it stays out of git): `_private/antigravity-anchored-readback.md`.

## Commit

Recorded by the commit that adds this decision.
