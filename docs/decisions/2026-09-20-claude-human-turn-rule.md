# 2026-09-20 — One rule decides what a human turn is in a Claude Session

## Decision

`sources/claude_text.anchored_user_text` is the single answer to "did a person say this,
and did the model receive it?". Four consumers ask it and therefore count the same records:

| Consumer | Where it asks |
|---|---|
| the reader's `user` turns, and the Markdown export's `## USER` blocks | `server._claude_text_kind` |
| the card's `user_turn_count` / `single_turn` | `server._selection_user_turn` |
| the history index's cached `user_turn`, which numbers `[U#]` | `claude_history` |
| the anchored transcript's `[U#]` banner | `anchored_transcript.render_claude` |

The rule:

| Record | Human turn? | What it becomes instead |
|---|---|---|
| anything the client marks `isMeta` | no | reader: the skill body when a command or a Skill call announced one, otherwise a short system row (`harness_note`); anchored transcript: omitted, as before |
| the interrupt marker, `[Request interrupted by user]` or `[… for tool use]` | no | reader: a system row with the marker's own wording; anchored transcript: `⚠ EVENT INTERRUPTED`; export: `## NOTIFICATION` |
| the pseudo-messages `is_system_user_string` already listed (task notice, bash output, teammate report, slash-command injection) | no | unchanged |
| a record holding only tool results; blank text; a reminder with nothing after it | no | — |
| a message that is only an image | **yes** | `[image]` |
| typed text that shares a record with a tool result | **yes** | the tool result keeps its line, the text keeps its `[U#]` |
| a message typed after a slash command that announces no body (`/clear`, `/model`) | **yes** | it was being filed as that command's skill |
| the sentence `Continue from where you left off.` | only when a person typed it | the client's own copy is `isMeta` and stays an event |

## What the measurement showed, and where it corrected the brief

The work started from two observed Sessions and two suspected causes. A per-record audit of
the whole local library (2,929 Claude Sessions, read-only) confirmed the first cause and
corrected the second.

**The interrupt marker** was as described: 276 records in 108 Sessions took a `[U#]` and a
card count, and no reader turn. Every one of the 287 markers in the library is a single
text block, never `isMeta`, in exactly two wordings.

**The image case was not what it looked like.** The brief read it as "an image-only message
gets a reader turn but no `[U#]`". The records say otherwise: the image message itself
already had both. The reader's extra turn came from a *second* record the client writes
right after a pasted image — `isMeta: true`, text `[Image: source: <cache path>]`. The
reader never looked at `isMeta` at all, so it drew that note, and every other record the
client writes for itself, as something the person said: 239 records in total (image notes,
hook feedback, messages relayed from another Session, channel messages). No human turn was
being lost from the anchored transcript on this path; harness text was being gained by the
reader.

The audit also surfaced four smaller disagreements of the same kind, fixed here because
they are the same question answered by the same rule:

| Records | Shape | Who was wrong |
|---|---|---|
| 13 | a non-`isMeta` message after a slash command with no body | the reader filed it as `skill`. All 215 genuine skill bodies in the library are `isMeta`; of the 13 non-`isMeta` "skills", 11 were typed by the person and 2 were compaction summaries (see below) |
| 10 | human text wrapped in a leading `<system-reminder>` | the card did not count it; the reader and `[U#]` did |
| 4 | a message that is only an image | the card did not count it |
| 1 | typed text sharing a record with a tool result | the anchored transcript dropped the text — the one real case of a human turn missing from the artifact agents read |

Before: 543 records disagreed across 156 Sessions. After: 0 across 0. The two Sessions the
work started from now read 44 / 44 and 59 / 59.

## Rationale

**`isMeta` is the structural marker for "the client wrote this".** It is the same kind of
evidence the 2026-09-20 event-records decision requires — a flag the client set, not a
guess from what the text reads like. The anchored transcript, the history index and the
card already honoured it; the reader was the only consumer that did not.

**The interrupt marker is matched as the whole text, not as a prefix.** The reader used
`startswith('[Request interrupted by user')`, which also swallows a person's message that
quotes the marker and goes on. The pattern is one bracketed line,
`\[Request interrupted by user[^\]\n]*\]`, matched against the entire text: both wordings
in use pass, a third wording would pass without a code change, and a quoted marker inside
a longer message stays the person's turn.

**An interrupt is an event although the model does receive it.** The earlier principle was
"something the person said *and* the model saw". The marker meets the second half only.
An agent reading `[U#]` to learn what the person asked for gains nothing from it and
miscounts the conversation with it; as an `⚠ EVENT` line it still shows that, and where,
the person stopped the agent.

**A harness note is shown in the reader, not hidden.** These records were already visible
(mislabelled as the person's). Several explain what the agent did next — hook feedback, a
relayed message — so removing them would lose evidence. They become the existing quiet
system row, cut to 300 characters; the full record is one `[L#]` away. No new colour, no
new component.

**The resume prompt keys on `isMeta`, not on its wording.** All 62 in the library are
`isMeta`. Matching the sentence alone would take a turn away from a person who types it.

## Alternatives rejected

* **Patch the two named cases where they were observed.** That is how the counters drifted
  in the first place: four copies of one question. Each consumer now calls the rule, so a
  future change cannot reach one and miss another.
* **Drop every `isMeta` record from the reader**, as the anchored transcript does. Skill
  bodies are `isMeta` and the reader shows them on purpose; and the notes carry evidence.
* **Also emit `isMeta` notes as `⚠ EVENT` lines in the anchored transcript.** Defensible,
  but it changes what agents read beyond the counting question, and skill bodies would
  have to be excluded again by a second rule. Left as it was.
* **Keep a text-prefix rule for skill bodies that lack `isMeta`.** None exists in 2,929
  Sessions; a rule for a shape nobody has observed is a guess.
* **Leave the card count alone** to avoid a rescan. It is one of the counters the contract
  names, and it was wrong in both directions.

## Blast radius

* `[U#]` numbering shifts on Sessions that contain an interrupt marker (down) or typed text
  beside a tool result (up). `[L#]` anchors, record ids and `state.json` do not move.
* `claude_history.SCHEMA` 6 → 7: cached `user_turn` values moved, so the index is rebuilt.
* `CACHE_SCHEMA_VERSION` 13 → 14: the card's count or one-shot flag moved on 86 of 2,929
  Sessions (count down on 79, up on 6), and 8 Sessions became `single_turn` — one message,
  then Esc. Every other metadata field is byte-identical before and after. The cost is one
  full rescan.
* Reader: 239 records stop being drawn as the person's message; 13 start. `j`/`k`
  navigation and the message counter follow, because they follow `user` turns.

## Found, and deliberately not changed here

* **Compaction summaries take a `[U#]`.** 88 records carry `isCompactSummary` and read
  "This session is being continued…". The client wrote them, yet every consumer counts
  them as a human turn — they *agree*, so they are outside this change, but by the
  principle above they are wrong together. Two of them followed `/compact` and were shown
  as a skill; they now read as a user turn like the other 86.
* **Card previews and search still use their own text helper** (`server._user_text`), which
  takes message content without the record and so cannot see `isMeta`. A hook-feedback
  string can appear in a preview, or match a search under the role `you`.
* **A tool result can print a banner-shaped line.** Nine lines in the library look like
  `━━ [U3] [L42] USER ━━` and are text quoted inside a tool result or a pasted transcript.
  An agent could take one for a real anchor.

## Evidence

* `scripts/audit_claude_human_turns.py` — the per-record audit, runnable by anyone against
  their own library. It rebinds the state file, the scan cache, the history index and the
  events journal to a temporary directory before importing anything, and prints counts and
  structural shapes only: no message text, no paths, no Session ids. Run on `staging` at
  `7a48701` and on this change; the shape table above is its output.
* `tests/test_claude_human_turn_rule.py` — 16 tests over synthetic fixtures. Each asserts
  all four counters at once for its shape, that `[U#]` runs 1..n without a gap, and the
  exact lines that carry a banner. 14 of the 16 fail on `staging`; the other two guard
  behaviour that was already right (a genuine skill body, a record of only tool results).
  19 deliberate single-guard mutations were each caught by a named test.
* Four existing fixtures in `tests/test_server.py` modelled a skill body or the resume
  prompt without `isMeta`. They now carry it, as every real one does.
* Production state was checked after the runs, read-only: the resident history index holds
  no schema-7 row, and the scan cache on disk is still schema 13.
* The raw per-record output names real Sessions and stays outside the repository.

## Commit

`78222f3` — Count the same human turns in the reader, the card and [U#].
