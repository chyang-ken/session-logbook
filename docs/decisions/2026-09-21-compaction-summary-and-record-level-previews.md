# 2026-09-21 — A compaction summary is not a human turn; previews and search ask the record

Follows [2026-09-20 — One rule decides what a human turn is](2026-09-20-claude-human-turn-rule.md)
and closes the first two items of its "Found, and deliberately not changed here".

## Decision

**1. The record the client marks `isCompactSummary` is not a human turn.**
`sources/claude_text.anchored_user_text` returns `''` for it, so the four consumers that ask
the rule stop counting it together. What it becomes instead:

| Artifact | Before | Now |
|---|---|---|
| reader | a `you` turn | a folded `summary` block, full text, skipped by `j`/`k` and the message counter |
| anchored transcript | a `[U#]` banner | `[L#]   ⚠ EVENT COMPACTION_SUMMARY (written by the client, N chars):` followed by the whole summary |
| Markdown export | `## USER` | `## COMPACTION SUMMARY`, whole |
| card count, history index `user_turn` | counted | not counted |
| card previews, dashboard search, CLI `search` | the person's words | absent |

The reader's presentation was put to the owner as a choice (folded block that opens to the
full text, or a one-line system row) and the owner chose the folded block. Everything else
in this record was decided by the implementer.

**2. Nothing reads "what the person said" from message content alone any more.**
`server._user_text(content)` is deleted, not given a second parameter. Card previews (first
message, `recent_msgs`, the selected-branch first message) call `anchored_user_text(record)`;
dashboard search and `session_logbook_cli._message_from_row` call the new
`claude_text.human_turn_words(record)` — the same verdict, without the `[image]` placeholder.

## What the measurement showed

Whole local library on 2026-09-20, 2,940 Claude Session files, read-only, structural
fields only.

**Compaction summaries: 88 records in 39 Sessions, one shape.** Every one is a `user`
record with string content, carries `isCompactSummary` and `isVisibleInTranscriptOnly`, is
not `isMeta`, opens with "This session is being continued", and directly follows a
`system` / `compact_boundary` record. No record has the opening sentence without the flag.
Length 6,675 – 20,751 characters, median 13,515. For 73 of them the boundary's
`logicalParentUuid` is in the same file; for 15 it is not — the compaction opened a new
file, and the summary is that file's only account of what came before.

**The content-only helper was wrong in both directions.** The brief named one direction.
Comparing `_user_text(content)` with the rule, record by record:

| Records | Shape | What the helper did |
|---|---|---|
| 122 | `isMeta` string content: 30 image-size notes, 18 channel messages, 11 hook feedback, 63 other | accepted it as the person's words |
| 161 | blocks: text and an image | rejected a human turn |
| 6 | blocks: text only | rejected a human turn |
| 4 | blocks: image only | rejected a human turn |
| 1 | blocks: text beside a tool result | rejected a human turn |

The second group is the larger one. The helper returned `''` for any block list that did
not start with a reminder — a shape-based guess meant to keep skill bodies out. Every one
of the 215 skill bodies in the library is `isMeta` (2026-09-20 audit), so the guess caught
no skill body and hid 172 messages the person wrote, most of them with a screenshot
attached, from previews and from search.

## Rationale

**The flag decides, not the opening sentence.** This is the same standard as the two
earlier decisions: a marker the client set, never a reading of the text. A person can type
"This session is being continued"; a later client can reword its summary. Both are tested.

**The summary is kept, and kept whole, in both places a reader looks.** It is the only
record of what the agent still knew after a compaction, and what a reader opens it for —
what was pending, what came next — is at its end. The reader never truncated it
(`CONV_USER_MAX` is 200,000); the folded block keeps that and shows more of the tail than
of the head, because the head is the client's boilerplate. In the anchored transcript it
stays whole because of the 15 cross-file cases: cutting it to the 300 characters other
events get would leave an agent reading such a file with a boilerplate sentence and nothing
else.

**The reader block reuses the skill block's classes.** Scope filtering, in-conversation
search and the standalone font size all key on `.conv-skill`, and a summary is the same
kind of thing: reference text the model received and nobody typed. One modifier class moves
the tint from the external-content orange to the existing system-event gray
(`--role-system-rgb`). No new token, no new component.

**A summary is not indexed for search.** It restates records that are themselves searched
— in the same file for 73 of 88, in the parent file for the rest. Indexing it under another
role would return a hit for nearly every query a Session's real messages also match.

**`[image]` is shown in a preview and never matched by a search.** On a card it says that
the message carried a picture. In search it is our word, not the person's: matching it would
answer "image" with every pasted screenshot, under the role `you`. A message that is only an
image is still a turn and still on the card; it has no words to find.

**The unsafe helper is removed rather than fixed.** A function that takes content cannot see
`isMeta` or `isCompactSummary`, whoever calls it and however carefully. Keeping the name
with a new signature would leave the next call site one careless argument away from the
same leak. A test asserts the name is gone.

## Alternatives rejected

* **A one-line system row in the reader** ("context compacted, summary N chars"). Offered
  to the owner; not chosen. It removes from the reader text that is visible there today.
* **Treat the summary as a `harness_note`.** That is what the rule alone would have done:
  the reader's fallback for a non-turn is a 300-character system row, which for this record
  is exactly the client's boilerplate.
* **Truncate the summary in the anchored transcript**, to save an agent 3–5k tokens per
  compaction. Wrong for the 15 cross-file records, and it changes what agents read beyond
  the counting question. An agent that does not want it can skip one marked block.
* **Match the opening sentence as well as the flag**, as a fallback. No such record exists
  in 2,940 files; a rule for a shape nobody has observed is a guess, and this one would take
  a turn from a person who quotes the sentence.
* **Give `_user_text` a `record` parameter.** See above.
* **Emit `isMeta` notes into the anchored transcript or the search index while here.** Out
  of scope, and rejected on its merits in the previous decision.

## Blast radius

Measured as a differential over a frozen list of 2,453 Session files, `extract_metadata`
from `staging` at `579b8e9` against this change, field by field:

* `user_turn_count` and `single_turn`: **identical on all 2,453.** The card counts human
  turns in the last 300 KB of a file and clamps a larger file to at least 2. All 37
  Sessions in the list that hold a summary are larger than 1 MB, and no summary sits in the
  last 300 KB of any of them. The rule is still what the card asks — a summary near the end
  of a file, or a Session of one message and one compaction, is now counted correctly
  (tested) — but no card in this library shows a different number.
* `recent_msgs` differs on 53 Sessions. 59 previews left: 14 compaction summaries (each the
  first record of a file a compaction opened, shown until now as the Session's opening
  message) and 45 `isMeta` notes. 43 entered: 14 carry a picture; most of the rest are older
  messages taking the slot a harness note had occupied.
* `activity_at` differs on 4 Sessions, always earlier: the last "activity" had been a
  harness note. Three move by 2 seconds, one by 51 minutes.
* No other metadata field differs.
* `[U#]` moves down by one after each compaction in a Session. `[L#]` anchors, record ids
  and `state.json` do not move.
* `claude_history.SCHEMA` 7 → 8 (cached `user_turn` moved). `CACHE_SCHEMA_VERSION` 14 → 15
  (cached `recent_msgs` and `activity_at` moved; a stale cache is refreshed only when a
  file's mtime changes). The cost is one full rescan and one index rebuild.
* Reader: 88 `you` turns become `summary` blocks; the message counter and `j`/`k` follow.

## Evidence

* `scripts/audit_claude_human_turns.py`, extended here. It now asks a fourth consumer, **S**
  — `session_logbook_cli._message_from_row`, the function search attributes words with — and
  counts compaction summaries and how many of them any consumer calls a human turn. A turn
  that is only an image has no words for S to carry and is counted apart. New
  `--files-from` option: the library gained about 1,500 files from an unrelated batch job
  while this work ran, so both runs below used one frozen list of 2,453 files.

  | | `staging` `579b8e9` | this change |
  |---|---|---|
  | records any consumer calls a human turn | 7,825 | 7,742 |
  | Sessions where the consumers disagree | 62 (151 records, all `RAC-`) | 0 |
  | compaction summaries a consumer calls a human turn | 83 of 83 | 0 of 83 |
  | writable paths after the run | temp dir empty | temp dir empty |

  The 83 fewer human-turn records are exactly the 83 summaries. (83, not 88: the frozen
  list is older files only, and the audit reads the selected branch of each file.)
  S covers the CLI and, after this change, dashboard search, because both call
  `human_turn_words`. It could not see the `isMeta` leak on `staging`: the CLI already
  dropped `isMeta` records itself, and the dashboard's own call did not. That leak is the
  122-record row in the table above, measured against the helper directly.
* `tests/test_claude_compaction_and_previews.py` — 18 tests over synthetic fixtures; 17 fail
  on `staging`, the other guards that the summary is never cut down to a harness note.
  Compaction tests assert all four counters at once. 18 deliberate single-guard mutations
  (the rule ignoring either flag, each of the three summary branches removed, each preview
  and search call site reading content only, the placeholder kept in search, either schema
  left unbumped) were each caught by a named test. The first mutation run reported two
  wrong catchers: a mutated file and its restore landed in the same second with the same
  size, so Python reused stale bytecode. Rerun with `-B`; the results above are from that run.
* `tests/test_claude_reminders.py` asserted that a text-block list without a reminder is
  never the person's. That was the shape-based guess; it now asserts the flag decides.
* End to end over HTTP, against a synthetic home directory (all state, cache and index
  paths inside it): the reader drew the gray folded `summary` block and opened it to its
  last line; `/api/sessions` previewed the picture message and neither the summary nor the
  hook feedback; `/api/search` found `mulberry` under `you` and returned nothing for
  `marmalade` (hook feedback), `damson` (summary only) or `image`.
* Production state checked afterwards, read-only: the resident scan cache is still schema
  13 and the history index holds no schema-8 row.
* The per-record and per-Session outputs name real Sessions and stay in `_private/`.

## Assumptions that may stop holding

* **The client always sets `isCompactSummary`.** True for 88 of 88 today. A client that
  drops the flag would make every summary a human turn again in all consumers at once, and
  a comparison between consumers cannot see that. The audit therefore prints how many
  records open like a summary and lack the flag; anything but 0 means the rule needs a new
  structural marker (the preceding `compact_boundary` record is the candidate).
* **No card number moved** only because no summary sits in a file's last 300 KB in this
  library. Another library, or a larger `TAIL_BUFFER`, will see counts drop.
* **S stands for dashboard search** only while `server._search_session` calls
  `human_turn_words`. If that call site changes, the audit keeps passing and search drifts;
  `tests/test_claude_compaction_and_previews.py` is what holds that line.
* Not checked on real data, because none exists: a summary inside a sub-agent stream, and a
  summary that is also `isMeta` (the anchored transcript would drop it silently).

## Still open

* A tool result can print a banner-shaped line (`━━ [U3] [L42] USER ━━`). Nine such lines
  in the library; unchanged here. It is the third leftover of the previous decision.

## Commit

`1da4071` — Stop counting the client's compaction summary as a human turn; make previews and search
ask the record.
