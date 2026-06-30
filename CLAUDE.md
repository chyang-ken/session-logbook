# Session Logbook — Agent & Contributor Handbook

This file is the operational guide for anyone (human or agent) working in this repo.
The philosophy and boundaries live in [`docs/philosophy.md`](docs/philosophy.md); this
file is the *how*.

---

## 0. Working conventions (read first — these are guardrails, not suggestions)

This is a public, open-source repository with a worldwide audience. Almost all code
here is written by AI agents, so the conventions that keep it clean and safe must live
*in the repo*, not in anyone's head. Follow them by default.

### This repo is the single source of truth
There is no second "private" copy to sync against. Product development happens **here**.
Don't reintroduce a two-repo / export-and-sanitize workflow — it was deliberately retired.

### English first
Commit messages, public-facing code comments, docstrings, and repository documentation
are written in **English**. A `_zh-CN` companion file (e.g. `README_zh-CN.md`) is welcome
where it adds value, but the English version is the source of truth. Write English
directly — don't write another language and back-translate.

> Note: parts of the existing code still carry non-English inline comments from before
> the open-source cutover. That's a known, in-progress migration: **new** comments are
> English, and existing ones get translated opportunistically when you touch a function.
> Don't mass-rewrite comments blindly — correctness first; tests must stay green.

### Never commit real session data — this point is load-bearing
This tool reads *people's private agent logs*. The single biggest risk for **this**
project is committing real session content by accident.

- Test fixtures and examples are **synthetic only**. No real transcripts, no real
  usernames, no names of other projects. Use obvious placeholders: `/Users/alice/my-app`,
  UUIDs like `aaaaaaaa-…`.
- Personal scratch work — experiments, one-off analysis, anything containing real session
  data — goes in the git-ignored **`_private/`** directory (or a separate private repo),
  **never** in committed history. `_private/` exists for exactly this; use it freely.
- Before committing, sanity-check diffs for absolute home paths, real project names, and
  pasted secrets.

### Commit messages
A commit message is a context handoff to your future self and to future contributors.
Beyond a clear subject line, explain what isn't cheap to recover from the diff: the
problem/motivation, the root-cause reasoning, what you changed and why, and how you
verified it. Purely mechanical changes (rename, format) only need the subject + a short
"what" line.

### Stay in scope
Before adding a feature, check [`docs/philosophy.md`](docs/philosophy.md). The dashboard
is a **read-only cockpit**: "send a message", "spawn a session", "multi-user auth", and
"live push" are explicit non-goals.

---

## 1. Data flow

```
DATA (read-only)
  ~/.claude/projects/*/*.jsonl           (Claude Code)
  ~/.codex/sessions/YYYY/MM/DD/*.jsonl   (Codex)
  ~/.gemini/antigravity/.../*.jsonl      (Antigravity)
    └─► server.py: scan_sessions()       [incremental, by mtime]
        └─► _cache {jsonl_path: meta}
            └─► enriched_sessions()      [meta + state + scope]
                └─► GET /api/sessions
                    └─► frontend: bucket → group → render

STATE (writable)
  POST /api/sessions/:id/{star,archive,note}
    └─► _state[id] updated
        └─► save_state()                 [tmp file + atomic rename + backup rotation]
            └─► ~/.session-logbook/state.json

UI (browser-only)
  localStorage:
    'session-logbook-ui' = { q, sectionOpen/Closed, groupOpen/Closed,
                  cardCollapsed, cardExpanded, recentDays, colLeftPct,
                  hideOneshot, sourceFilter }
```

Each agent's on-disk format is adapted to a common shape by a module under `sources/`
(`codex.py`, `antigravity.py`); Claude Code is read directly in `server.py`.

## 2. Cross-layer contracts

1. **Scope is computed twice.** The backend uses `DUSTY_AFTER_DAYS` for an initial value;
   the frontend's `computeScope()` recomputes with the user's `recentDays` and overrides it.
   **The frontend is the source of truth.**
2. **Priority is consistent.** `archived > starred > mtime`, front and back must agree.
3. **Optimistic UI.** Update `state.items[i]` + render immediately; on a failed POST,
   roll back + toast.
4. **No auth.** Binds `127.0.0.1` only.

## 3. API

| Endpoint | In | Out |
|---|---|---|
| `GET /api/sessions` | — | `[{id, project_path, jsonl_path, mtime, mtime_iso, size, recent_msgs, last_stop_reason, user_turn_count, custom_title, scope, archived, archived_at, starred, starred_at, note}]` |
| `GET /api/search?q=…` | multi-word = AND; session ID matches too | `[{id, snippets:[{text, role, term}]}]` |
| `GET /api/stats` | — | `{total, starred, recent, dusty, archived}` |
| `GET /api/sessions/:id/conversation` | — | `{id, project_path, custom_title, total_lines, turns:[…]}` |
| `GET /api/sessions/:id/anchored` | — | Plain-text transcript with `[L#]` original-line anchors (for agents to read / download) |
| `GET /api/recent-files` / `GET /api/find-files` | Files panel | recent-changed / `fd` name search |
| `POST /api/sessions/:id/star` | `{starred: bool}` | `{id, …entry}` |
| `POST /api/sessions/:id/archive` | `{archived: bool, note?}` | `{id, …entry}` |
| `POST /api/sessions/:id/note` | `{note: string}` | `{id, …entry}` |

A POST body missing `starred` / `archived` defaults to `True`.

## 4. Frontend routes

| URL | Mode | Notes |
|---|---|---|
| `/` | dashboard | list view (default) |
| `/?session=<id>` | standalone | single-session full-screen reader; hides dashboard chrome; larger body text |

## 5. Key constants (top of `server.py`)

| Constant | Default | Purpose |
|---|---|---|
| `PORT` | 47821 | listen port |
| `DUSTY_AFTER_DAYS` | 7 | backend scope cutoff (frontend can override) |
| `TAIL_BUFFER` | 300 KB | tail window for card previews |
| `RECENT_USER_N` / `RECENT_ASSISTANT_N` | 3 / 3 | how many of each to pull for previews |
| `CONV_USER_MAX` / `_ASSISTANT_MAX` / `_TOOL_RESULT_MAX` | 5000 / 10000 / 1500 | conversation-view truncation |
| `SEARCH_SNIPPET_CONTEXT` / `SEARCH_MAX_SNIPPETS` | 60 / 3 | search snippet sizing |

State lives at `~/.session-logbook/state.json`, with rotating backups under
`~/.session-logbook/backups/`.

## 6. Code map

| Location | Responsibility |
|---|---|
| `server.py` `extract_metadata` | card preview (first user + tailed user/assistant) + turn counts + custom title |
| `server.py` `extract_conversation` | conversation view (pairs tool_use/tool_result, filters thinking, detects skill injection) |
| `server.py` `compute_scope` / `_effective_archived` | backend scope (pure function, unit-tested); `_effective_archived` derives archived state (explicit state > Codex file location) |
| `server.py` `load_scan_cache` / `save_scan_cache` / `CACHE_SCHEMA_VERSION` | persistent warm scan cache (`~/.session-logbook/scan-cache.json`): load on start + incremental scan. Bump the schema version on any meta-shape change, or stale caches break |
| `server.py` `search_sessions` / `_rg_prefilter` / `_search_session` | full-text search (ripgrep prefilter → per-session AND match; pure-Python fallback) |
| `server.py` `list_recent_files` / `find_files_by_name` | Files panel backends |
| `sources/codex.py` `is_codex_path` / `CODEX_ARCHIVED_ROOT` | Codex (`~/.codex`) data source; `is_codex_path` is the centralized dual-root predicate (active `sessions` + `archived_sessions`) |
| `sources/antigravity.py` | Antigravity (`~/.gemini/antigravity`) data source |
| `sources/anchored_transcript.py` | anchored-transcript renderer (`render_claude` / `render_codex`); the single source of truth behind the `/anchored` endpoint |
| `index.html` `<style>` | all CSS (custom props in `:root`) |
| `index.html` `stripWorktree` / `projectKey` | path normalization + grouping keys |
| `index.html` `computeScope` | frontend scope override |
| `index.html` `render` / `renderCard` / `renderConv` | list, card, and conversation rendering |
| `index.html` `bindConvNav` | user-message navigation (j/k + goto + scroll state machine) |

## 7. Style

- UI reference: Notion / Linear — light, sans-serif, **readability over decoration**.
- CSS custom properties (`--bg`, `--text-1`, …) defined in `:root`.
- **No build step:** edit `index.html`, refresh the browser.
- **Before changing UI, read [`docs/design-system.md`](docs/design-system.md).** Colors and
  hover semantics are tokenized — pick by action category, not by what looks nice. Don't add
  new color variables without proving the existing tokens can't cover the case.

## 8. Tests

```bash
python3 -m unittest discover -s tests
```

`tests/` uses Python `unittest` with synthetic fixtures. All tests must pass before merge;
CI runs the same command on every push and PR.

## 9. Decision log

Decisions backed by an experiment / comparison / measurement are recorded under
[`docs/decisions/`](docs/decisions/) — see that directory's README for the format.
