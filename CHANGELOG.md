# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- Report a Codex sub-agent rollout as the complete Session it is. A thread Codex spawned for another thread was flagged "Conversation history is incomplete" in the reader, and refused by the Agent CLI, whenever its client also wrote a redundant `forked_from_id` — although a spawned thread inherits nothing by reference and its own file is its whole record. It now reads complete, and the Session that spawned it is reported as lineage (`spawned_from`) rather than as a missing prefix. A user fork with no recorded history base still reports one.
- Stop labelling the harness's own user-role records as human turns in the anchored transcript. A background-task notice, bash-mode output, a teammate report or a slash-command injection took a `[U#]` there, while the dashboard's turn count had always excluded it, so the two artifacts disagreed about what the person said. `[U#]` numbering shifts on Sessions that contain such records and their content becomes a marker line; `[L#]` anchors, record ids and stored metadata are unchanged.
- Keep the whole of a message during an Agent CLI `follow`. The cursor filter read `[L#]` anywhere in a rendered line as that line's anchor, so a User or Assistant message that quotes an anchor (pasting an anchored transcript into a conversation does exactly that) moved the cursor and silently dropped the rest of that message. Only a line's leading anchor counts now; this also affected Kimi Code follows.
- Order sessions and show recency by conversation time rather than file rewrites; rebuild cached metadata for existing sessions.
- Preserve genuine Claude user text appended after desktop handoff reminders in search, previews, the reader, and exports; refresh cached metadata for existing sessions.

### Added
- Show the three kinds of Claude record the reader used to drop, each as a quiet system event at its true position: text a person typed while the agent was busy that nothing in the file shows being handed over, a background-task completion notice, and a failed-and-retried request (one row per run, with the retry count and the span). None of them counts as a user turn or reaches `j`/`k` navigation. Unconfirmed queued text is searchable under its own role, `queued`; notifications and errors are not indexed.
- Show the conversation's note in the reader, and edit it there. It was visible only on the list card, so the one place a person re-reads the work was the one place their own note about it was invisible. Notes left on superseded records are listed beside it, each labelled with its record and never merged into the current one.
- Read Antigravity sessions from the `/anchored` download and the Agent CLI (`locate`, `context`, `follow`, `status`, `evidence`, `search`, `recent`). The export used to render a header with no body and call itself a Claude Code session, and the CLI refused the source outright. Both now read the live history of an in-file rewind, state how many raw lines that rewind abandoned, and keep those lines reachable with `evidence`; `follow` is cursor-based and names the abandoned lines at or before your cursor with the same `REMOVED_BEFORE_CURSOR` field the Claude delta follow uses.
- Add an opt-in `follow --delta` for Claude Sessions: it returns only the cursor onward and lists the earlier anchors a rewind removed (`REMOVED_BEFORE_CURSOR`), so cursor-holding readers no longer re-read and diff the whole branch. Against a conversation id with several records it needs `--cursor-source-path` to know which record the cursor came from, and otherwise returns the full branch with `DELTA_NOT_APPLIED`. Default `follow` is unchanged.
- Add a `recent` Agent CLI command that lists recently active Sessions without a known target, with title, source, project, latest user time, conversation id and the dashboard's selection hints; single-turn Sessions and sub-agents are opt-in. Each conversation is offered as its current record only, so a transcript a rewind superseded is never listed as a live peer. `--since` also accepts hours such as `6h`.
- Read Pi sessions from local JSONL: selected-branch search, reader, exports, and Agent CLI, with original-line evidence and existing local metadata controls.
- Browse sessions across projects in a default, newest-first timeline; the project view remains available.
- Hide suspected automated runs with an explicit, reversible filter. Single-turn sessions are the initial signal; users can confirm their participation to keep a session visible.
- Give sessions personal titles without changing source records, and search both personal and source titles.
- Read Devin Local sessions directly from SQLite, including selected message chains, search, conversation/export, organizing controls, and database-node evidence through the Agent CLI.
- Introduce the `staging` → local soak → `main` release flow: `scripts/release_flow.py` (`check` / `deploy` / `release`), a project session-start hook that reports when something is ready for `main`, and CI on `staging` pushes. Documented in CLAUDE.md "Branch model and release flow".
- Add Kimi Code CLI (`$KIMI_CODE_HOME`, default `~/.kimi-code`) as a fourth session source: dashboard cards, conversation view (including AskUserQuestion as Q&A turns), markdown export, anchored transcript, full-text search, and the Agent CLI. Sub-agent streams are skipped like Codex sub-agents.
- The standalone single-session reader (`/?session=<id>`) now follows a running session in place: it polls the session file's fingerprint and redraws only when the file changed, keeping the scroll position and expanded blocks. Finished sessions are never redrawn.
- Add a server-independent, read-only Agent CLI for locating, handing off, following, auditing, and searching Claude Code and Codex Sessions.
- Ship one `session-logbook` Skill as the Agent-facing entry point, including opt-in subagent discovery and line-anchored incremental reads with a one-line overlap.

## [0.1.1] - 2026-08-09

### Security
- Reject cross-site and DNS-rebinding requests to the loopback HTTP server.
- Restrict Files-panel roots to projects discovered from local session metadata.
- Reject non-JSON or oversized state-changing requests.

## [0.1.0] - 2026-08-09

### Added
- Initial public release of Session Logbook.
- Unified, read-only dashboard for Claude Code, Codex, and Antigravity sessions.
- Four-zone layout (Starred / Recent / Dusty / Archived) with automatic time decay and project grouping.
- Card previews, full conversation view, standalone reader, and user-message navigation (`j`/`k`).
- Full-text search (ripgrep-accelerated, pure-Python fallback).
- Star / Archive / Note, persisted to `~/.session-logbook/state.json`.
- Files panel (recent files + `fd`-backed name search).
- Downloadable anchored transcript export.

[Unreleased]: https://github.com/chyang-ken/session-logbook/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/chyang-ken/session-logbook/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/chyang-ken/session-logbook/releases/tag/v0.1.0
