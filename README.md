# Session Logbook

A minimal, local, zero-dependency retrieval layer for finding and re-reading your AI coding-agent sessions — **Claude Code, Codex, Antigravity, Kimi Code, Devin Local, and Pi** — all in one place.

[![CI](https://github.com/chyang-ken/session-logbook/actions/workflows/ci.yml/badge.svg)](https://github.com/chyang-ken/session-logbook/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)

> Read this in other languages: [Chinese](README_zh-CN.md)

Your agents leave behind hundreds of session transcripts scattered under `~/.claude`, `~/.codex`, `~/.gemini`, `~/.kimi-code`, `~/.pi/agent/sessions`, and the Devin Local database. Session Logbook reads them **read-only** so you can find past work by time or text, open the full Session, and reuse it without leaving your machine.

![Session Logbook screenshot](docs/screenshot.png)

<sub>Screenshot generated from synthetic demo data — see [`examples/`](examples/) to run it yourself.</sub>

## Why

At enough volume, managing every Session by hand stops working. Session Logbook focuses on the two jobs that remain useful:

- **Find past work** — browse by recency or search the words you remember.
- **Read it in context** — open the complete Session Detail instead of reconstructing the work from filenames or snippets.
- **Cross-agent** — Claude Code, Codex, Antigravity, Kimi Code, Devin Local, and Pi sessions share one local surface.
- **Read-only and private** — it never sends a message, spawns a session, or talks to the network. Binds `127.0.0.1` only and serves its browser assets locally.

## Quickstart

Requires **Python 3.9+** (standard library only — no `pip install`).

```bash
git clone https://github.com/chyang-ken/session-logbook.git
cd session-logbook
python3 server.py          # → http://127.0.0.1:47821
```

Open <http://127.0.0.1:47821>. The first scan takes 10–30s depending on how many sessions you have; after that it only re-reads files whose `mtime` changed.

That's it. There is no build step, no `pip install`, and no browser-side CDN fetch — editing `index.html` and refreshing the browser is the entire dev loop.

## Devin Local

Devin Local sessions are read directly from `~/.local/share/devin/cli/sessions.db`,
including committed WAL updates. `XDG_DATA_HOME` changes the data-home base;
`DEVIN_DATA_DIR` overrides the Devin data directory itself. No export, IDE plugin,
or extra Python package is required. This supports Devin Local's database format,
not legacy Windsurf Cascade or cloud Devin sessions. The default path has been
verified on macOS; other installations can point `DEVIN_DATA_DIR` at their data.

The selected message chain is shown; hidden sessions and abandoned retry branches
are excluded. A broken or unsupported chain is reported rather than blended with
other branches. IDs are prefixed with `devin:` to avoid collisions with other sources.

For Agent access, use `--source devin`. Devin anchors are `[N#]` database row IDs,
not JSONL line numbers. `evidence --line 42` expands `[N42]`. `follow --cursor-line 42`
returns a **full current snapshot**, because editing or compaction can replace
messages before that cursor. Compare node IDs and content; do not treat it as an
append-only delta. Copied source references have the form
`<sessions.db>/<encoded-session-id>` and are accepted by the CLI; they are not files.

## Give a Session to another Agent

For optional lifecycle evidence alongside conversation, see
[runtime observation (experimental)](docs/runtime-observation.md).

The dashboard does not need to be running. The read-only Agent CLI accepts a known Session
ID, an exact JSONL path, or a search query:

```bash
# Compact context with [L#] anchors back to the original JSONL
python3 session_logbook_cli.py context '<session-id-or-path>'

# Just the tail: the last 3 human turns, addressed by the [U#] the transcript prints
python3 session_logbook_cli.py context '<session-id-or-path>' --last-turns 3

# On the next check, repeat the cursor line once, then return later content
python3 session_logbook_cli.py follow '<session-id-or-path>' --cursor-line 427

# Search only real user messages across recent Session history
python3 session_logbook_cli.py search 'payment retry' --role user --since 30d
```

Claude Desktop can copy history into another Session ID. Related source files are
shown as evidence links; the selected file stays the transcript, and shared history
never automatically changes a supervisor's target. Explicit source-qualified follow
checks copied record identity and rejects divergent cursors. See the
[Claude history contract](docs/decisions/2026-09-19-claude-copied-history.md).

The repository also ships one Agent Skill, [`session-logbook`](skills/session-logbook/SKILL.md),
covering handoff, follow-up observation, evidence expansion, discovery, and historical mining.
Install it by linking the repository copy into your Agent's Skill directory so the Skill and
CLI always stay on the same version:

```bash
mkdir -p ~/.claude/skills ~/.codex/skills
ln -s "$PWD/skills/session-logbook" ~/.claude/skills/session-logbook
ln -s "$PWD/skills/session-logbook" ~/.codex/skills/session-logbook
```

## Features

Pi reads the last saved branch of each session, including its original messages before
compaction. Its `parentSession` field indicates a fork, not an automated sub-agent.
Set `PI_CODING_AGENT_DIR` for a custom agent home, or
`PI_CODING_AGENT_SESSION_DIR` for a custom session directory. Extension-specific
subagent layouts and per-project session-directory settings are not auto-discovered.

- **Full-text search** — multi-word AND; matched snippets highlighted; source titles, personal titles, and session IDs match too. Backed by `ripgrep` when available, with a pure-Python fallback.
- **Full conversation view** — click a card to expand; user / assistant / tool / skill turns are color-coded. Pop out to a standalone full-screen reader (`/?session=<id>`).
- **Conversation navigation** — jump between user turns with `↑ N/M ↓ go to: __`, use `latest` to reach the newest message, or move with the keyboard (`j` next, `k` prev).
- **Card previews** — opening user message + the most recent user/assistant turns, so you can tell sessions apart at a glance.
- **Cross-project timeline** — the default view sorts Sessions by last activity across projects.
- **Suspected automated-run filter** — single-turn Sessions are hidden by default with an explicit filter and an empty-result recovery action. Confirm human participation to correct a false positive.
- **Personal titles** — add a local name for future retrieval; source titles remain intact and searchable.
- **Optional four-zone project view** — Starred / Recent / Dusty / Archived, grouped by project (by the last two path segments; `.worktrees/` fold into their parent).
- **Time decay** — a Session untouched for N days drops into 🕸 Dusty automatically (toggle 7 / 14 / 21 days in the UI).
- **Star / Archive / Note** — lightweight organizing that persists to `~/.session-logbook/state.json`.
- **Files panel** — browse a project's recently-changed files or fuzzy-find by name (`fd`-backed).
- **Downloadable anchored transcript** — export a compact, navigable transcript with line-number anchors back to the original JSONL (useful for feeding a session to an agent for analysis).
- **One read-only Agent interface** — resolve a known Session, hand it to another Agent, retrieve only later additions, expand exact source lines, or search bounded history without running the dashboard.

## Where to go next

| You want to… | Go to |
|---|---|
| **Try it** | [Quickstart](#quickstart) above |
| **Understand the design & boundaries** | [`docs/philosophy.md`](docs/philosophy.md) |
| **Let an Agent use Session history** | [`skills/session-logbook/SKILL.md`](skills/session-logbook/SKILL.md) |
| **Work on the UI** | [`docs/design-system.md`](docs/design-system.md) |
| **Contribute** | [`CONTRIBUTING.md`](CONTRIBUTING.md) |
| **Report a bug / request a feature** | [Open an issue](https://github.com/chyang-ken/session-logbook/issues) |
| **Report a security issue** | [`SECURITY.md`](SECURITY.md) |

## FAQ

| Question | Answer |
|---|---|
| Change the port? | `python3 server.py --port 47822` |
| Change the default Dusty threshold? | `DUSTY_AFTER_DAYS` in `server.py`, or toggle 7/14/21d in the UI |
| Reset local metadata? | Removing `~/.session-logbook/state.json` resets stars, archives, notes, personal titles, and participation confirmations; source records are untouched. |
| A session has no preview? | It's too short (system-only), or its tail is all tool output — increase `TAIL_BUFFER` |
| What's the colored dot on a card? | Green = last `stop_reason` was `end_turn`; yellow = `tool_use`; gray = unknown. A hint only — archiving is always manual. |
| A session failed to parse? | It's still listed with an empty preview; one bad file never crashes the dashboard. |

## What it deliberately does *not* do

Send messages · spawn Sessions · manage whether a client Session stays alive or archived · multi-user auth · live push (SSE/WebSocket) · cross-machine sync. This is a read-only retrieval layer, not an agent client or lifecycle manager. Rationale in [`docs/philosophy.md`](docs/philosophy.md).

## License

[MIT](LICENSE)
