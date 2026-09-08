# Philosophy

Top-level positioning and boundaries. Read this document before adding or rejecting any feature.

## How the user works (design premise)

Many parallel sessions × a fleet of worktrees × switching across projects. All three kinds of parallelism happen at once — a single flat list is bound to collapse under them.

## What the Dashboard is

| | |
|---|---|
| ✅ Cockpit | Observe, organize, tag |
| ❌ Orchestrator | Does not send messages, does not spawn sessions |
| ❌ Client | Does not write messages, does not push in real time |

The CLI is already the orchestrator; the dashboard does not reinvent that wheel.

## The four-zone hierarchy

| Zone | Entry condition | Default | Purpose |
|---|---|---|---|
| ⭐ Starred | Starred manually | Expanded | "I want to remember this" |
| 🔥 Recent | mtime ≥ now − N days | Expanded | The main working surface |
| 🕸 Dusty | mtime < now − N days | Collapsed | Auto-accumulation zone |
| 📦 Archived | Archived manually | Collapsed | "Out of sight" |

Priority: `archived > starred > mtime`.

## Time decay

The bet: **people won't archive 130 times by hand.** So once a session has been untouched for N days, it automatically drops into the collapsed Dusty zone, keeping the main working surface uncluttered.

N is adjustable: the frontend toggles between 7 / 14 / 21 d (persisted in localStorage); the initial default equals the backend's `DUSTY_AFTER_DAYS`.

## Star ⊥ Archive

| | Meaning | Overrides |
|---|---|---|
| Star | Pin permanently | Time decay |
| Archive | Force-hide | Everything (including star) |

Unarchiving a starred session sends it straight back to the Starred zone, with its star state preserved.

## Context reduction (for whom)

Before shrinking a session, ask one question first: **who is the reduction for?** There are three kinds of consumers and three kinds of artifacts — don't blur them into one, and don't build a second wheel for a consumer that already has one.

| Consumer | Artifact | Anchors / back-reference | Source | Current carrier |
|---|---|---|---|---|
| **Agent reading** (fed read-only analysis, expands back to the original on demand) | rendered anchored transcript (plain text) | `[U#]` for human turns + `[L#]` for the **original line number** — one jump and you're there | Claude + Codex ✓ | `sources/anchored_transcript.py` via the web download or Agent CLI |
| **Human reading** (review, clipboard) | token-optimized Markdown | None (read once, then discard) | Dual-source ✓ | modal `export` button → `extract_transcript` |
| **Programmatic parsing** (structured re-assembly) | structured JSONL | call_id / turn_id | Codex only | No current product surface; the early trim attempt was superseded by render |

Hard rules:

1. **The rendered anchored transcript is the standard reduction artifact for agents** — it does not replace the raw jsonl; it's a **navigation layer carrying original-file coordinates**. User and Assistant message text stays complete. Tool actions retain their target path, search scope, or command prefix. Successful result bodies collapse to status and size; leading error text stays visible. Thinking, injected context, and binary content are reduced. The agent uses `[L#]` to fetch hidden operational detail back from the original with precision. The raw jsonl always lives in `~/.claude` / `~/.codex`, so "read the conversation, expand operations on demand" holds naturally on the local machine. The dashboard download and `session_logbook_cli.py context` emit the same artifact through `sources/anchored_transcript.py`.
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
| Multi-user / authentication | A single-user, local-machine tool |
| SSE / WebSocket | Manual ↻ is already enough; a persistent connection costs 100× its value to sync |
| Full-text session browsing (beyond search snippets) | `code $jsonl_path` does the job |
| Auto star / archive via ML | The decay threshold already replaces 99% of the need |
| Cross-machine sync / mobile support | The work environment is right here on this machine |

Before adding a feature: run it past this table first, then past the three negations under "What the Dashboard is."

## Database-backed source coordinates

Devin Local uses its selected SQLite message chain as the original evidence.
`[N#]` addresses `message_nodes.row_id` within the identified session; it never
pretends that a generated transcript line is an original source line. Context and
HTTP anchored export share the Devin renderer. Follow returns the full current
chain because edits and compaction can replace prior nodes. Evidence can still
expand an old node belonging to that session. See
[the Devin Local decision](decisions/2026-09-08-devin-local.md).
