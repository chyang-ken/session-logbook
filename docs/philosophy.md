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
| **Agent reading** (fed read-only analysis, expands back to the original on demand) | rendered anchored transcript (plain text) | `[U#]` for human turns + `[L#]` for the **original line number** — one jump and you're there | Dual-source ✓ | `tools/session-review/pipeline/render*.py` |
| **Human reading** (review, clipboard) | token-optimized Markdown | None (read once, then discard) | Dual-source ✓ | modal `export` button → `extract_transcript` |
| **Programmatic parsing** (structured re-assembly) | structured JSONL | call_id / turn_id | Codex only | `scripts/trim_session_jsonl.py` (an early attempt, **now superseded by render**) |

Hard rules:

1. **The rendered anchored transcript is the standard reduction artifact for agents** — it does not replace the raw jsonl; it's a **navigation layer carrying original-file coordinates**. The details aren't in the transcript itself; the agent uses `[L#]` to fetch them back from the original with precision. The raw jsonl always lives in `~/.claude` / `~/.codex`, so "read the small transcript, expand on demand" holds naturally on the local machine. The dashboard's "download reduced version" button emits exactly this artifact (dual-source via `render.py` / `render_codex.py`).
2. **A model-written summary (a brief or a session-review report) is downstream of reduction, not a fourth kind of reduction** — it consumes any of the artifacts above and produces a shorter insight. Don't conflate "summary" with "reduction" as if they were the same layer.
3. **Before adding any new "reduction / summary" variant, come back to this table** and prove the existing three can't cover the case before doing anything. The structured use case for `trim` has no live demand right now; to revive it, first prove the rendered anchored transcript can't feed your consumer.

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
