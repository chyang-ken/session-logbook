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

## Follow-up

`observe` still returns the full Claude conversation and flags
`conversation_reconciliation_required`. Giving it the same delta is a separate change.

## Commit

Recorded by the commit that adds this decision.
