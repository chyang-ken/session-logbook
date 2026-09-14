# Philosophy

Top-level positioning and boundaries. Read this document before adding or rejecting any feature.

## How the user works (design premise)

Session volume eventually makes manual lifecycle management unsustainable. Whether a live
Session should continue or be archived is decided in its agent client. The durable need here is
to recover work later, often across many clients and projects.

## What Session Logbook is

| | |
|---|---|
| ✅ Retrieval layer | Find and re-read work across agent clients |
| ❌ Orchestrator | Does not send messages, does not spawn sessions |
| ❌ Lifecycle manager | Does not decide which client Sessions stay alive or get archived |
| ❌ Client | Does not write messages or push in real time |

The primary journey is deliberately short:

1. Find a Session by time or search.
2. Open Session Detail and recover the full context.
3. Reuse the work elsewhere.

The browser and Agent CLI are complementary interfaces to local Session history. The browser is
optimized for human recall and reading; the CLI is optimized for bounded Agent retrieval and
evidence expansion.

## Product direction

The primary browser surface is one reverse-chronological timeline across projects, with
search as the other main entry. Project boundaries can help narrow a result, but should not split
the default history into separate places.

Explicitly identified sub-agent logs are machine-to-machine work, not conversations the user
remembers having. They should not appear as standalone human Sessions. When the source does not
provide reliable identity, the product may use a simple, visible **suspected non-human Session**
filter. Initially, single-turn Sessions without human confirmation are suspected and hidden by default in the timeline and search. Empty results explain the active filter and offer to reveal hidden matches. A suspicion is not a fact: the user must be able to reveal the filtered results and mark a
Session as human-participated. Opening, naming, or copying a Session does not silently classify it.

Local metadata belongs only when it improves retrieval. A personal title is useful when the source
title is hard to remember: it may override display and participate in search, while the source title
stays intact and clearing the personal title restores it. This is different from managing the
Session's lifecycle.

Start with the smallest useful rule and correct real failures as they appear. Do not build a
classification framework, scoring system, or edge-case matrix in advance.

## Existing organization is secondary

The current project view and its four zones remain useful compatibility tools. They are not the
product's primary lifecycle model.

### Four-zone hierarchy

| Zone | Entry condition | Default | Purpose |
|---|---|---|---|
| ⭐ Starred | Starred manually | Expanded | "I want to remember this" |
| 🔥 Recent | conversation activity ≥ now − N days | Expanded | Newer Sessions in the project view |
| 🕸 Dusty | conversation activity < now − N days | Collapsed | Auto-accumulation zone |
| 📦 Archived | Archived manually | Collapsed | "Out of sight" |

Priority: `archived > starred > conversation activity`.

Timeline, search, and displayed recency use the latest parsed user/assistant message time.
Rewriting a source file does not count as a new conversation. If no valid message timestamp
is available, fall back to source modification time; filesystem time still detects changes.

### Time decay

Within the project view, once a Session has been untouched for N days, it automatically drops into
the collapsed Dusty zone.

N is adjustable: the frontend toggles between 7 / 14 / 21 d (persisted in localStorage); the initial default equals the backend's `DUSTY_AFTER_DAYS`.

### Star ⊥ Archive

| | Meaning | Overrides |
|---|---|---|
| Star | Pin permanently | Time decay |
| Archive | Force-hide | Everything (including star) |

Unarchiving a starred session sends it straight back to the Starred zone, with its star state preserved.

## Context reduction (for whom)

Before shrinking a session, ask one question first: **who is the reduction for?** There are three kinds of consumers and three kinds of artifacts — don't blur them into one, and don't build a second wheel for a consumer that already has one.

| Consumer | Artifact | Anchors / back-reference | Source | Current carrier |
|---|---|---|---|---|
| **Agent reading** (fed read-only analysis, expands back to the original on demand) | rendered anchored transcript (plain text) | `[U#]` for human turns + `[L#]` for the **original line number** — one jump and you're there | Claude + Codex + Kimi ✓ | `sources/anchored_transcript.py` via the web download or Agent CLI |
| **Human reading** (review, clipboard) | token-optimized Markdown | None (read once, then discard) | All four sources ✓ | modal `export` button → `extract_transcript` |
| **Programmatic parsing** (structured re-assembly) | structured JSONL | call_id / turn_id | Codex only | No current product surface; the early trim attempt was superseded by render |

Hard rules:

1. **The rendered anchored transcript is the standard reduction artifact for agents** — it does not replace the raw jsonl; it's a **navigation layer carrying original-file coordinates**. User and Assistant message text stays complete. Tool actions retain their target path, search scope, or command prefix. Successful result bodies collapse to status and size; leading error text stays visible. Thinking, injected context, and binary content are reduced. The agent uses `[L#]` to fetch hidden operational detail back from the original with precision. The raw jsonl always lives in `~/.claude` / `~/.codex` / `~/.kimi-code`, so "read the conversation, expand operations on demand" holds naturally on the local machine. The dashboard download and `session_logbook_cli.py context` emit the same artifact through `sources/anchored_transcript.py`.
2. **A model-written summary (a brief or a session-review report) is downstream of reduction, not a fourth kind of reduction** — it consumes any of the artifacts above and produces a shorter insight. Don't conflate "summary" with "reduction" as if they were the same layer.
3. **Before adding any new "reduction / summary" variant, come back to this table** and prove the existing three can't cover the case before doing anything. The structured use case for `trim` has no live demand right now; to revive it, first prove the rendered anchored transcript can't feed your consumer.

## Agent access has one product surface

The repository's [`session-logbook` Skill](../skills/session-logbook/SKILL.md) is the only
Agent-facing discovery and routing layer. Finding, handoff, incremental observation, evidence
expansion, and historical mining are modes of the same read-only Session-history product, not
separate Skills.

The Skill calls `session_logbook_cli.py`, which owns source detection and reuses the same parsers
and anchored renderer as the dashboard. It works without the HTTP server. Following an active
Session is polling: the caller supplies the previous `[L#]` cursor and receives that cursor line
once more before later rendered content, avoiding a miss when the last record was only half-written.
This does not introduce SSE, WebSocket, messaging, Session spawning, or any claim that a
quiet transcript proves liveness or completion.

## The won't-do list (with reasons)

| Won't do | Reason |
|---|---|
| Send messages / spawn sessions | The CLI is already the orchestrator |
| Manage client Session lifecycle | Each agent client already owns whether its Session stays alive or is archived |
| Multi-user / authentication | A single-user, local-machine tool |
| SSE / WebSocket | Existing polling supports list and reader updates without a persistent push connection |
| Predict human identity or lifecycle with ML | Visible suspicion plus reversible correction is enough until real use proves otherwise |
| Cross-machine sync / mobile support | The work environment is right here on this machine |

Before adding a feature, ask whether it makes historical work easier to find, read, or reuse. If
not, it should normally stay out. Then run it past this table and the boundaries under "What
Session Logbook is."

## Database-backed source coordinates

Devin Local uses its selected SQLite message chain as the original evidence.
`[N#]` addresses `message_nodes.row_id` within the identified session; it never
pretends that a generated transcript line is an original source line. Context and
HTTP anchored export share the Devin renderer. Follow returns the full current
chain because edits and compaction can replace prior nodes. Evidence can still
expand an old node belonging to that session. See
[the Devin Local decision](decisions/2026-09-08-devin-local.md).


## Session selection is shared metadata

Session Logbook owns source relationships and selection hints for downstream clients.
The CLI and HTTP selector reuse `sources/session_identity.py`; clients must not infer
parentage or maintain a second classification registry. The selection endpoint returns
recent primary and single-turn groups separately so a burst of automated work cannot
crowd all other candidates out. A single user turn is useful evidence for folding likely
automation, not proof: `interaction_kind` stays unknown, and clients can reveal the
other group. Names and stars are not required. Explicit children are linked to their
parent by the CLI and omitted from standalone selector candidates. Search and source
records remain available; selection never archives, deletes, or grants permission.
Large Claude logs are checked for a second user turn instead of treating file size as
evidence of a conversation. The existing preview count remains backward compatible.
