# 2026-09-20 — Three Claude record kinds become events, and the reader shows the note

## Decision

Two changes, approved together by the repository owner on 2026-09-20 under one principle:
**a queued input is not a delivered one, and notifications and errors must never pose as
human speech.**

### 1. Three record kinds the reader used to drop are now shown, as system events

| Native shape | What it is | What the reader shows |
|---|---|---|
| `queue-operation` with `operation: "enqueue"` and a `content` string, where no record in the file delivers that same string | text a person typed while the agent was busy, with no evidence it was ever handed over | `Queued input — delivery not confirmed`, with the text |
| `attachment` whose `attachment.type` is `queued_command` and `attachment.commandMode` is `task-notification`; or the same `<task-notification>` block sitting in an undelivered queue entry | a background task finished | `Background task finished` as `[status] summary`, with the body collapsed, and the delivery qualifier when it only ever sat in the queue |
| `system` with `subtype: "api_error"`, carrying `error.formatted`, `retryAttempt` and `maxRetries` | a request failed and was retried | one row per run: the error's own wording, plus `— retried N times over <span>` |

Each is a quiet single-line row using the existing `.conv-system-event` component, at its
true position in the conversation. None of them is a user turn: they are not counted in
`user_turn_count`, never reach a card preview, the first-user-message or `recent_msgs`,
are skipped by `j`/`k` navigation, and take a `[L#]` marker line rather than a `[U#]`
anchor in the anchored transcript. Undelivered queued text is searchable under its own
role, `queued`; notifications and API errors are not indexed at all.

The same rules apply to sub-agent transcripts, which are the same records.

### 2. The conversation's note is shown, and editable, in the reader

The note was visible only on the list card — so the one place a person re-reads the work
was the one place their own note about it was invisible. The reader header now shows the
conversation's note, the notes left on superseded records beside it (each labelled with
its record, never merged), and an `Add note` / `Edit note` control that reuses the card's
dialog and its write path. Identical in the modal and on the standalone page.

## How delivery is confirmed, and why it is confirmed that way

The client writes a queued string twice when it picks the entry up: once in the
`queue-operation` enqueue, and again in the delivered record — an ordinary `user` record,
or a `queued_command` attachment when it arrives mid-turn. Nothing links the two but the
string itself. The queue's own bookkeeping cannot stand in for the link: `dequeue` carries
no content at all, and `remove` appears both after a delivery and when an entry was simply
dropped, so neither operation says on its own whether the model ever saw the text.

So confirmation is **an exact match of the raw string, anywhere in the same file**, and the
entry is announced only when no match exists. The comparison is verbatim rather than
normalised, because the client copies the string across unchanged and a looser match would
start silently swallowing real messages. When the same text was typed twice and handed over
once, the queue is first-in-first-out: the earlier entry is the delivered one.

One consequence worth stating plainly: this is a claim about **evidence in the file**, not
about what happened. "Delivery not confirmed" means the transcript does not show the text
arriving. It does not prove it never did.

## Language is never the signal

The 2026-09-20 format audit found Chinese prose inside machine records and English prose
inside human ones. Every predicate in `sources/claude_events.py` keys on an explicit marker
the client wrote — a record type, a `commandMode`, an `origin.kind`, an XML wrapper — and
none of them looks at what the text reads like.

## What changed beyond the three record kinds, and why

**`[U#]` stopped counting the harness's user-role pseudo-messages.** A background-task
notice delivered to the model arrives as an ordinary `user` record whose string content
opens with `<task-notification>`. `server.py` has always excluded those from
`user_turn_count`, but `sources/claude_text.anchored_user_text` did not, so the anchored
transcript an agent reads labelled them `━━ [U3] … USER ━━`. Fixing only the newly-shown
records would have left the same notification reading as a human turn in one artifact and
as an event in another, which is the exact thing the owner's principle forbids. The
predicate now lives in one place, `claude_text.is_system_user_string`, and both counters
use it.

Blast radius: `[U#]` numbering shifts on sessions that contain such records, and their
content is now emitted as a `⚠ EVENT` marker line instead of a user turn. `[L#]` anchors,
record ids, `/api/sessions` and `state.json` are untouched. `claude_history.SCHEMA` goes
5 → 6 to force the cached `user_turn` values to be rebuilt.

**`CACHE_SCHEMA_VERSION` stays at 13.** `extract_metadata` produces byte-identical output
before and after: nothing it reads changed shape or value. Bumping it would force a full
rescan of the whole library for no gain. The history index above is the cache whose values
actually moved, and that one is bumped.

**A superseded record's `/conversation` payload now carries `note` and `older_notes`.** It
previously carried neither, because the state merge only ran for the current record. Now
that the note is read and edited from the reader, a deep link to an earlier record would
have shown an empty note and an `Add note` button that silently wrote somewhere else — the
backend has always landed a note write on the conversation's current record. `title_override`
and `human_confirmed` are deliberately left alone on earlier records; they stay per-record
as before.

## Where the display wording departs from the approved shape

The approved label for the third kind was `Connection error — retried N times`. Most of
these records are not connection errors: `error.formatted` reads `529 Overloaded` about as
often as `Connection dropped (ECONNRESET)`, and a fixed prefix would have mislabelled the
commonest case. The row keeps the error's own wording and appends the retry count and span.
The `error` role chip carries the category instead.

Only `error.formatted` is ever displayed. `error.message` carries the raw upstream body —
for a 529 that is a JSON error payload — which belongs in the source file, not on a
conversation line.

A background-task notice that only ever sat in the queue carries the same
`delivery not confirmed` qualifier as an unconfirmed queued input, rather than reading as
though the agent had been told the task finished.

## Rejected alternatives

* **Treating `dequeue` as delivery and `remove` as loss.** It matches most of the sampled
  data and is wrong in both directions: a delivery through an interruption attachment is
  followed by `remove`, and a `dequeue` with no content proves nothing about what reached
  the model. Reading the queue protocol as if it were a delivery receipt would have turned
  a guess into a displayed fact.
* **Reclassifying a delivered task notice by counting it out of `user_turn_count`.** It is
  already excluded there; only the anchored transcript disagreed, and that is what was
  fixed. Changing the count would have moved every consumer of `single_turn` and the
  suspected-automation filter for no evidence gain.
* **Indexing notifications and errors for search.** A session that retried a hundred times
  would bury real matches under a hundred identical rows, and nobody searches for them.
* **Giving the new rows their own colour.** `design-system.md` §1.2 reserves colour for
  exceptions. The rows reuse `--role-system-rgb` like every other system event; only the
  `delivery not confirmed` qualifier and the error text take `--rust`, which is the
  exception the rule allows for.

## Evidence

* Unit tests: `tests/test_claude_event_records.py` (31 tests — the native shapes, each of
  the three kinds, queued-then-delivered appearing exactly once, retry collapsing, the
  sub-agent file, and every consumer: reader, card preview, search, Markdown export,
  anchored transcript, CLI `context`/`follow`). 16 of the 21 behaviour tests fail against
  the previous commit; the other five are regression guards for behaviour that was already
  correct, including "a file with none of these records renders exactly as before".
  Fourteen deliberate single-guard mutations were each caught by a named test.
* Frontend: `tests/conversation_live_test.js` renders the real `renderConv` in both entry
  modes and asserts the event rows carry `conv-system-event` and never `conv-user`, that
  the message counter and the `goto` ceiling still read 1, and that the note and the older
  notes appear in both modes. `tests/conversation_identity_frontend_test.js` drives the
  real `editSessionNote` through a save, a failed save and a cancel.
* Read-back against the real local library, read-only, on a spare port with the state file,
  the scan cache, the history index and the events journal all rebound to a temporary copy:
  recorded outside the repository because it necessarily names real sessions.
