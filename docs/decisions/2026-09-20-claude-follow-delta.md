# An opt-in delta for Claude follow, not a changed default

## Decision

`follow` on a Claude Session keeps returning the full selected branch. A new opt-in
`--delta` returns only the cursor onward and names, in `REMOVED_BEFORE_CURSOR`, the physical
lines at or before the cursor that the selected branch no longer contains. When the branch
cannot be verified it reports `unknown` rather than an empty list.

## Rationale

The full-branch default exists because a Claude Desktop rewind hides earlier records and
Logbook keeps no record of what a reader has consumed; returning everything lets any reader
reconcile. That stays correct for a reader without durable state.

Readers that do hold a cursor pay for it on every poll, and each one rebuilt the same
filter. Two independent consumers on the maintainer's machine had done so: one filtered
rendered blocks by line number and hashed full snapshots to notice removals; the other
filtered by line number only and therefore kept content from abandoned branches. Only
Logbook can tell which lines left the selected chain, so the removal list belongs here.

Measured on two real Sessions (content not recorded), with a cursor 30-40 lines from the end:

| Branch state | plain `follow` | `follow --delta` |
|---|---|---|
| verified, with hidden records | 126,404 bytes | 3,819 bytes, removed anchors listed |
| unverified (`ambiguous_session_identity`) | 296,073 bytes | 14,705 bytes, removal `unknown` |

In the unverified case the full output carried no removal information either: every saved
record is preserved, so there is nothing for a reader to diff against.

## A cursor belongs to a record, so a conversation id alone does not carry one

Added when this branch merged conversation identity
([`2026-09-20-conversation-identity.md`](2026-09-20-conversation-identity.md)). A conversation
id resolves to the conversation's *current* record, and a rewind or a resume can make that a
different file from the one a reader's cursor was taken in. Physical line anchors are per file,
so applying a bare line number to the current record could skip lines the reader never saw —
the opposite of what a delta is for.

`--delta` therefore applies to a multi-record conversation id only when the caller also passes
`--cursor-source-path`, which the header already tells every reader to save and which maps the
cursor explicitly. Without it the command falls back to the documented full selected branch and
prints `# DELTA_NOT_APPLIED:` with the current record and the fix. Targeting a record id is
unchanged: a record id is never retargeted, so its cursor always belongs to the file it names.

Refusing outright was rejected: the full branch is always reconcilable, so a caller that cannot
name its cursor's record should get more data and a clear reason, not an error. Applying the
cursor with a warning was rejected too — a warning does not restore content the reader never
received.

## Alternatives and why rejected

- **Make delta the default:** silently changes a locked contract for existing readers that
  rely on the full branch to reconcile.
- **Remember per-reader cursors inside Logbook to report exact removals:** adds writable
  consumer state to a read-only retrieval layer. Reporting every hidden line up to the cursor
  is a stateless superset, and retiring an anchor twice is harmless.
- **Return an empty removal list when unverified:** would read as "nothing was removed".
  `unknown` keeps the uncertainty visible.
- **Leave it to consumers:** already produced two divergent filters, one of them wrong after
  a rewind.

## Evidence

- `tests/test_rewind_integration.py`: five synthetic cases - cursor-onward output with removed
  anchors, a rewind that happens after the cursor was taken, no rewind, an unverified branch,
  and opt-in plus zero-cursor behaviour. Disabling delta mode fails four of them; emptying the
  hidden-line list fails two.
- The byte counts above come from `session_logbook_cli.py follow <id> --cursor-line N`
  with and without `--delta` on local history. No transcript content is included.
- `tests/test_conversation_api.py::CliDeltaFollowTests`: three cases on a synthetic
  three-record conversation whose current record was itself rewound - a conversation id with a
  named cursor source gets the delta and reports the record it used, a conversation id with a
  bare cursor gets the full branch and `DELTA_NOT_APPLIED`, and a record id is untouched.
  Dropping any one clause of the attribution check fails exactly one of them.

## Follow-up

`observe` now offers an opt-in `--delta` with structured removal anchors, while
plain observation retains the full reconciliation contract. Unverified branches,
source changes and unattributed conversation cursors keep the full response;
concurrent source writes fail without advancing cursors. The consuming monitor
can request a full reconciliation when a removed anchor was actually read, rather
than repeatedly reacting to every historical hidden line. See runtime-observation.md
and the observe cases in tests/test_rewind_integration.py.

## Commit

Recorded by the commit that adds this decision.
