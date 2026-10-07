# 2026-09-20 — Conversation identity is additive; record IDs never change meaning

## Decision

Session Logbook now serves two levels of identity instead of one.

* A **record** is one physical transcript — one Claude Code JSONL file, one Codex rollout,
  one Kimi session directory, one Devin row, one Pi file. Its `id`, its path and its `[L#]`
  line anchors mean exactly what they meant before and will keep meaning it. Old
  `/?session=<record id>` links, existing `state.json` keys and Session IDs an Agent wrote
  down all keep resolving to the same transcript.
* A **conversation** is the ordered set of records a person would call one conversation. A
  Claude Desktop rewind or resume mints a new record id for the same conversation, and a
  compaction sometimes starts a new file mid-conversation. Every API item now carries
  `conversation_id`, `conversation_current_id` and `conversation_records` — the members
  oldest to newest, each labelled with how it follows its predecessor.

Conversation identity is computed from the transcripts and the Claude Desktop descriptors
on every read. Nothing about it is persisted, so it can be withdrawn the moment the
evidence changes, and removing the feature leaves `state.json`, the scan cache and every
URL byte-identical.

## Why this shape and not the other one

The rejected alternative was to **re-key on the conversation**: make the conversation id the
item's `id`, keep the record id in a secondary field, persist a record-to-conversation alias
table and migrate `state.json` once.

Three facts argued against it.

1. **The evidence layer is deliberately willing to withdraw a grouping.** `claude_desktop`
   already refuses to group on ambiguous descriptors, and ten guard tests assert the
   annotation disappears entirely on bad evidence. Persisting a key derived from evidence
   that can dissolve creates orphaned state with no owner.
2. **Two stores cannot be re-keyed at all.** The runtime-events journal is keyed by ids the
   clients themselves mint through hooks; Logbook does not own them and can only union them
   at read time. And `state.json` is the user's single irreplaceable file — re-keying it
   means rewriting every entry to gain something a read-time merge already gives.
3. **The Skill contract forbids silent retargeting.** `skills/session-logbook/SKILL.md` and
   [`2026-09-19-claude-copied-history.md`](2026-09-19-claude-copied-history.md) both promise
   Agents that a supervision target is never migrated behind their back. Additive identity
   satisfies that by construction: an Agent keeps targeting a record id and merely *learns*
   the conversation id as one more reported fact.

## Identity rules, per client

Evidence came from a read-only survey of the local session library on 2026-09-20. The
findings are summarised here; no session id, path or transcript content is reproduced.

### Claude Code

Claude Desktop stores a descriptor per conversation, holding the current CLI session id, an
ordered list of prior CLI session ids, and explicit rewind edges. That store is the only
place the three cases can be told apart, because inside the JSONL a rewind child, a resume
child and a fork child all look the same: a copied prefix followed by divergence.

1. A record id named by a descriptor — as its current CLI session, in its prior list, or on
   either end of a rewind edge — belongs to that descriptor's conversation. The relation is
   **rewind** only when an explicit rewind edge says so; a prior id without an edge is a
   plain **continuation**. Prior ids alone were never proof and still are not.
2. Any other record is its own conversation.
3. A **fork** is a separate conversation. Two signals: the descriptor's own
   forked-from pointer, and a file whose first record still carries the *source* session's
   id (a rewind or resume re-stamps every copied record; a fork does not). Either one is
   reported as lineage and never as membership, and a fork can never share a conversation
   with its source.
4. A compaction boundary names the last record before the compaction. When that record was
   not written into the boundary's own file **above the boundary**, the conversation
   continued across a file boundary, and the file that owns the record is this file's
   predecessor — same conversation, relation **compaction-continuation**. "Above the
   boundary" is load-bearing: a file can later acquire a record re-using that uuid further
   down, and a whole-file search would then call the conversation self-contained and lose
   the link. The owner lookup is bounded to sibling transcripts in the same project
   directory and memoized; an unresolvable or ambiguous boundary, and any boundary that
   would make one record the predecessor of two files, produces no link.
5. A spawned background task shares no history with its parent. Its origin is reported as
   provenance only. Sub-agent transcripts are turns of a different actor and are never
   conversations of their own.
6. Anything contradictory fails open: a filename that disagrees with its record id, a record
   outside the conversation's working root, a record two conversations claim, a branch, a
   cycle, or two conversations a compaction would merge. The whole group dissolves back into
   single records rather than half-grouping. **Titles and shared history are never identity.**

### Codex, Kimi Code, Antigravity, Devin Local, Pi

`conversation_id` is the existing record id. A survey of all five found no case where these
clients mint a second top-level id for one conversation: Antigravity, Devin and Pi all have
rewind or branching, but each keeps the branch inside the same file or the same database
session. Applying Claude's rule to them would create groups of one and risk mis-grouping
conversations that merely sit near each other in time. Codex's own resume-and-dedup handling
is untouched.

## Personal state: merged on read, never re-keyed

`state.json` stays keyed by record forever. One function,
`session_identity.merge_conversation_state`, folds the members' entries into the
conversation's view. The policy below was **confirmed by the repository owner on
2026-09-20**.

| Field | Rule | Why |
|---|---|---|
| `starred` | true when **any** member is starred; `starred_at` is the earliest | A star set before a rewind must not vanish when the rewind mints a new id |
| `archived` | **only the current record's** state counts | A union would hide a live conversation behind an ancestor archived long ago |
| `note` | the current record's note is the conversation's note; older records' notes come back separately as `older_notes` | Notes are free text with no timestamp; concatenating them into storage is unrecoverable, and dropping one is worse |
| `title_override` | the current record's, else the newest older record that has one; the source record is reported | Same missing timestamp, but a title is replaceable and a visible fallback beats a blank card |
| `human_confirmed` | true when any member is confirmed | It only ever reveals; a union cannot hide anything |
| `brief` (cached) | stays per record, never merged | A brief is validated against one file's size and mtime; serving one record's brief for another's transcript would be a lie |

Writes land on the **current record**, whether they are addressed to a conversation id or to
any member record id. The one exception is un-starring, which has to clear every starred
member — otherwise the union would bring the star straight back.

## What changed at the edges

* `/api/stats` now counts conversations. Before this it counted records while the list
  showed one card per chain, so the counter and the list disagreed.
* `/api/session-choices` offers only current records. A superseded transcript is not where
  new work lands.
* `/api/search` results carry `conversation_id` alongside the record id, and keep their
  per-record snippets and line anchors so the evidence stays traceable to one file.
* A conversation id resolves to its conversation's current record in `_find_jsonl` and in
  the CLI's `locate`, `context`, `follow`, `status` and `evidence`, and every one of them
  reports the substitution rather than performing it silently. A record id still resolves to
  exactly that record; record resolution is tried first and is never overridden.
* `save_state` reads the file back after writing it and reports a mismatch, and its rotating
  backup now follows `STATE_FILE` instead of being skipped whenever that path is rebound —
  which was exactly the configuration of the prescribed pre-merge smoke test.
* Scan metadata gained the file's head session id and its unresolved compaction parents, so
  `CACHE_SCHEMA_VERSION` had to move. This change claimed 12 and shipped as 13: a branch
  developed in parallel claimed 12 for a different shape change, and the integrator gave the
  combined build a number of its own. The comment at the constant in `server.py` is the
  running account of what each number covers; quoting a number here would go stale.
* The frontend draws **one entry per conversation** (`conversationItems`). Superseded records
  are listed inside that entry instead of beside it as peers; a link to a superseded record
  still opens exactly that record and says it is no longer current rather than redirecting
  (`noticeConversationMoved`); and `setItems` / `findItem` / `currentItemFor` became the one
  id resolver, replacing twelve independent linear scans of `state.items`. This landed in the
  same pull request, one commit after the server change. The older display fields
  `rewind_current_session_id` / `rewind_history` are still produced by
  `claude_desktop.annotate_sessions` and still served, but nothing in the UI reads them.

## Evidence

* Unit tests: `tests/test_conversation_identity.py` (identity rules, fail-open cases, the
  state merge) and `tests/test_conversation_api.py` (scan evidence, `save_state`, the API,
  the CLI). Every one of them fails against the previous commit, and 30 deliberate
  single-guard mutations were each caught by a named test.
* Read-back against the real local library, read-only, on a spare port with the state file
  and scan cache rebound to a temporary copy: recorded outside the repository because it
  necessarily names real sessions. What it established, without the ids: of 3250 records,
  28 fold into 14 multi-record conversations (8 rewinds, 17 resumes, 3 cross-file
  compactions), 8 forks all stay separate from their sources, and a field-by-field diff of
  every API item against the running previous build showed **one** item changed on any
  pre-existing field — a conversation whose star had been stranded on the record a
  compaction superseded. That was the intended fix and the only behaviour change.

  The read-back also caught a real bug before merge: asking "is this uuid anywhere in the
  file" instead of "above the boundary" found zero cross-file compactions, because a file
  can acquire a record re-using that uuid further down.

## Open items

* The CLI reads the dashboard's warm scan cache instead of scanning the library itself. While
  that cache is absent or was written under an older schema, the CLI reports
  `conversation_evidence: "record_only"` with a reason and falls back to record-level
  identity rather than guessing a wider conversation; starting the dashboard once on the
  current build refreshes it. The API is unaffected — it scans.
* Compaction lineage is resolved per project directory. A conversation that continued into a
  different project directory is not linked; no such case exists locally to design against.

## Accepted by design, not a gap

Claude Desktop began writing descriptors only at a point in its own history, and on a
long-lived machine they cover a clear minority of the Claude transcripts on disk. A rewind
older than the descriptor store therefore leaves no evidence anyone can verify: inside the
JSONL its child is indistinguishable from a resume or a fork. Those records stay separate
single-record conversations, and that is the intended outcome rather than a limitation to
remove later — rule 1 requires a descriptor, and rule 6 exists precisely to refuse the
copied-prefix inference that would be the only alternative.
