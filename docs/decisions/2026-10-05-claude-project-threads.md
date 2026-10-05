# 2026-10-05 — Claude Project threads: show the person's words, and a coordinator's note is not a human turn

Follows [2026-09-21 — A compaction summary is not a human turn](2026-09-21-compaction-summary-and-record-level-previews.md)
and `a50d22d` ("make Claude Project worker sessions searchable"), which unwrapped the
Project wake envelope for search only.

## Decision

**1. A Project wake envelope is shown unwrapped wherever the person's words are shown.**
A Project worker receives the person's message as a user record wrapped in
`<wake><project><thread><message from="human" …>escaped text</message>…</wake>`.
`claude_text.human_turn_text(record)` is the text of a human turn as a reader sees it:
`anchored_user_text`'s verdict, taken on the record as written, with the envelope reduced to
its message bodies (`project_wake_words`, unchanged from `a50d22d`). Surfaces:

| Surface | Before | Now |
|---|---|---|
| card preview: first message (head scan and selected branch), recent messages | the XML | the message |
| CLI `recent` title, `/api/session-choices` preview | the XML (they read the card's first message) | the message |
| reader `you` turn | the XML | the message |
| Markdown export `## USER` | the XML | the message |
| anchored transcript `[U#]` (CLI `context` / `follow`, `/anchored`) | the XML | the message |
| search, CLI `search` | the message (since `a50d22d`) | unchanged |
| CLI `evidence --line N`, the JSONL | the record | unchanged: the envelope stays one `[L#]` away |

The verdict did not move for any wake: `anchored_user_text` still returns the raw text, and
every consumer still asks it.

**2. A note a Project's coordinator relays into a thread is not a human turn.** It arrives
as `<project_claude_message session=… thread_id=…><relay from="coordinator" …>`, or, in older
records, the `<relay>` alone. Both prefixes join `SYSTEM_USER_PREFIXES_EVENT`
(`PROJECT_RELAY_PREFIXES`), and `parse_system_user_event` renders the note as a
`teammate_message`: `[coordinator] <note body, unescaped>` — the reader's `team` row, the
export's `## TEAMMATE`, the anchored transcript's `EVENT TEAMMATE_MESSAGE`. It is no longer
on the card, in search, or in the CLI message stream as the person's words, and a queued
copy is no longer announced as queued input.

**3. A worker the coordinator checked in on is still not a one-shot run.** `single_turn`
is false when the session received a check-in (a coordinator note the client did not mark
`isMeta`), even if the person spoke once; the dashboard's `isSuspected` honours
`single_turn === false`. The `isMeta` copy — the brief a coordinator hands a worker it
spawns, `reason="spawn"` — opens the session like a first message and does not count.

## What the measurement showed

Whole local library on 2026-10-05, read-only, structural fields only.

**Wake envelopes.** 344 delivered wake records are human turns. Every one has
`origin.kind = human` and `projectsUserTurn`; every `<message>` inside is `from="human"`;
every one reduces to non-empty text; none has text outside its envelopes. Unwrapping loses
none of the person's words.

**Coordinator notes.** Every delivered note record is `from="coordinator"`, has a `<note>`
body, and carries the sentence "The note below was written by the coordinator session, a
Claude session, not by your user." Counted over the frozen list used below, re-read after
the runs:

| | records | client fields | counted as a human turn before |
|---|---|---|---|
| check-in, `<project_claude_message>` | 155 | `origin.kind task-notification`, `subkind projects-relay`, `turnOrigin peer` | yes |
| check-in, bare `<relay>` | 37 | the same | yes |
| spawn brief, `<relay reason="spawn">` | 90 | `isMeta`, `turnOrigin system` | no |

(The audit below saw 191 check-ins and 281 notes in all: live Sessions in the list were
still being written.) Another 550 queue entries hold notes; each was announced as queued human input
until delivered. Note bodies are HTML-escaped: no raw `<`, `>` or `&` in any of 275 bodies
checked, 21 with entities.

## Rationale

The record says who wrote it, in a sentence and in `origin` / `turnOrigin`. Calling the
note the person's words is the error `isCompactSummary` and `isMeta` were fixed for; the same
rule now covers it, so the reader, the card count, `[U#]`, search and the history index stay
in agreement (the audit below checks that on real data).

Unwrapping only in previews would leave the reader and the transcripts showing XML for the
same words the card shows plainly, and a find-in-reader for `&` would miss `&amp;`. Every
surface that shows what the person said now shows the same text; the evidence command and
the file keep the record as written.

The rule keys on the XML wrapper, not on `origin.kind`, because a queued note carries no
`origin` and the queue path has to recognise it too. The wrapper is client-written, like
`<task-notification>` and `<teammate-message>`.

Point 3 exists because the strict reading hides work the owner looks at: with notes no
longer counted, 20 worker sessions (each addressed by the person once, then checked in on)
had one human turn and became "suspected" — out of `session_logbook_cli.py recent` by default,
and, since all 20 have a selected branch that bypasses the card's clamp, out of the dashboard
too. Running `recent` for one project before and after showed nine of them leave the
default list. The one-shot heuristic exists to find sessions that took only their head
message; a worker that took check-ins did not.

## Alternatives rejected

* **Keep notes as human turns and only unwrap them for display.** The coordinator's words
  would stay `you` on the card, in the reader and in search snippets, against what the
  record says.
* **Let the 20 workers drop into the suspected filter.** Consistent with how a plain
  session the person typed into once is treated, and reversible per session; rejected for
  now because it removes sessions from the owner's default `recent` list as a side effect of
  a display fix. It stays open — see "Still open".
* **Count the spawn brief as a check-in too.** It would move 18 sessions (one human turn,
  only a spawn brief) out of the suspected filter, a change in the other direction that
  nothing here asked for.
* **Decide on `origin.kind` instead of the wrapper.** Misses the queue (no `origin`), and
  `task-notification` also covers seven "background agents were stopped" notices that are
  out of scope here.
* **A new reader row type for the coordinator.** The `team` row already says "another agent
  speaking"; the `[coordinator]` label says which.

## Blast radius

`extract_metadata` from `staging` at `8cd44e1` against this change, field by field, over a
frozen list of 5,086 Session files:

* `single_turn`: identical on all 5,086.
* `user_turn_count`: lower on 49 (only check-ins were removed); 20 now read 1. The dashboard's
  suspected verdict (old frontend on `staging` data against new frontend on this change's
  data) is identical on all 5,086; without the `isSuspected` line it would differ on those 20.
* `recent_msgs`: 89 Sessions. 167 previews that were a raw wake are now its message, 58
  coordinator notes left, 187 entered (the 167, plus older messages taking a note's slot).
  Previews starting with an envelope: 234 → 0.
* `activity_at`: 8 Sessions, always earlier — 12 seconds to 8.2 hours. Their last
  "conversation activity" had been a coordinator note. Ranking and `--since` move with it.
* No other field differs except one file written to between the two runs.
* `[U#]` moves down by one after each check-in in a worker Session. `[L#]` anchors, record
  ids and `state.json` do not move.
* `claude_history.SCHEMA` 8 → 9 (cached `user_turn` moved). `CACHE_SCHEMA_VERSION` 15 → 16
  (cached `user_turn_count`, `recent_msgs`, `activity_at` moved). One full rescan and one
  index rebuild.

## Evidence

* `scripts/audit_claude_human_turns.py`, extended: it labels wake envelopes and coordinator
  notes, and counts the notes any consumer calls a human turn and the card previews that
  still start with an envelope. Same frozen list of 5,086 files, both builds:

  | | `staging` `8cd44e1` | this change |
  |---|---|---|
  | records any consumer calls a human turn | 12,037 | 11,847 |
  | Sessions where the four consumers disagree | 0 | 0 |
  | coordinator notes a consumer calls a human turn | 191 of 281 | 0 of 281 |
  | card previews of the person starting with an envelope | 233 | 0 |
  | writable paths after the run | temp dir empty | temp dir empty |

  190 rather than 191 fewer: Sessions were being written while the two runs went one after
  the other, and a live one gained a turn in between. Not verified record by record.
* `tests/test_claude_project_display.py` — 22 tests over synthetic fixtures, covering every
  surface in the table above, both note wrappings, the queue, the spawn brief, a check-in
  outside the 300 KB tail, and the dashboard's `isSuspected` run under Node. 22 deliberate
  single-guard mutations (each display call site reverted, the verdict taken on unwrapped
  text, either prefix dropped, the event branch removed, the note left escaped or wrapped,
  the sender label fixed, each check-in branch removed, the spawn brief counted, the
  dashboard line removed, either schema left unbumped) were each caught by a named test, run
  with `-B` and a fresh bytecode cache per mutation.
* Smoke against the real library through an isolated launcher (state, scan cache, history
  index and runtime journal rebound to a temp directory): `recent --since 14d` for two
  projects listed 41 envelope titles before and 0 after, with the same Sessions apart from
  list churn from Sessions written during the run.
* Per-record and per-Session outputs name real Sessions and stay in `_private/`.

## Assumptions that may stop holding

* **Every message in a wake is from the person.** True for all 358 messages today
  (`from="human"`). A wake that carries another author's message would show it as the
  person's words; `project_wake_words` does not filter on `from`.
* **A coordinator note always opens with one of the two wrappers.** A third wrapping would
  be a human turn again in every consumer at once. The audit's "coordinator notes" row only
  sees the known wrappers; `origin.subkind = projects-relay` on a counted turn is the signal
  to look for.

## Still open

* **Whether a worker the person addressed once should count as a one-shot run.** Point 3
  keeps today's behaviour. Removing it is two lines and puts 20 current workers in the
  suspected filter.
* A Session whose only previewed "user" messages were coordinator notes now has none: its
  CLI title falls back to the project folder name, where it used to be the raw envelope.
  Two in the library.
* The card's assistant preview still reads text blocks only; a worker's channel reply is
  searchable (since `a50d22d`) but not on the card.
* Seven "N background agents were stopped by the user" records (`origin.kind
  task-notification`, plain text) are still counted as human turns.

## Commit

Filled in by the implementing commit's message (`Decision: docs/decisions/2026-10-05-claude-project-threads.md`).
