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
Before adding a feature, check [`docs/philosophy.md`](docs/philosophy.md). Session Logbook
is a **read-only retrieval layer**, not an agent client or lifecycle manager: "send a message",
"spawn a Session", "manage client Session state", "multi-user auth", and "live push" are
explicit non-goals.

---

## 1. Data flow

```
DATA (read-only)
  ~/.claude/projects/*/*.jsonl           (Claude Code)
  ~/.codex/sessions/YYYY/MM/DD/*.jsonl   (Codex)
  ~/.gemini/antigravity/.../*.jsonl      (Antigravity)
  ~/.kimi-code/sessions/*/*/agents/main/wire.jsonl   (Kimi Code; $KIMI_CODE_HOME overrides the root)
  ~/.pi/agent/sessions/*/*.jsonl         (Pi; PI_CODING_AGENT_DIR / PI_CODING_AGENT_SESSION_DIR supported)
    └─► server.py: scan_sessions()       [incremental, by mtime]
        └─► _cache {jsonl_path: meta}
            └─► enriched_sessions()      [meta + state + scope + conversation identity]
                └─► GET /api/sessions
                    └─► frontend: setItems() indexes by record id AND conversation id
                        └─► one entry per conversation → filter → timeline (default)
                            or project groups → render

STATE (writable)
  POST /api/sessions/:id/{star,archive,note,title,human}
    └─► _state[id] updated
        └─► save_state()                 [tmp file + atomic rename + backup rotation]
            └─► ~/.session-logbook/state.json

UI (browser-only)
  localStorage:
    'session-logbook-ui' = { q, sectionOpen/Closed, groupOpen/Closed,
                  cardCollapsed, cardExpanded, recentDays, colLeftPct,
                  hideOneshot, sourceFilter, viewMode }
    cardCollapsed / cardExpanded hold CONVERSATION ids (`cardKey()`), so a collapse
    choice survives a rewind minting a new record id. Entries an older build wrote are
    record ids; they are still read as a fallback and replaced on the next write, and
    `pruneCardCollapsed()` keeps both kinds alive. Never crash on an old shape.
```

Each agent's on-disk format is adapted to a common shape by a module under `sources/`
(`codex.py`, `antigravity.py`, `kimi.py`, `devin.py`, `pi.py`); Claude Code is read directly in `server.py`.
Every surface that presents a source -- cards, reader, export, search, the `/anchored`
download and the Agent CLI -- goes through its adapter. An Antigravity in-file rewind is
resolved once, in `antigravity.py`, so no surface can show a branch the conversation dropped.

## 2. Cross-layer contracts

1. **Scope is computed twice.** The backend uses `DUSTY_AFTER_DAYS` for an initial value;
   the frontend's `computeScope()` recomputes with the user's `recentDays` and overrides it.
   **The frontend is the source of truth.**
2. **Priority is consistent.** `archived > starred > conversation activity`, front and back must agree.
   `activity_at` / `activity_at_iso` describe the latest parsed user/assistant message;
   when no valid message time is available, they fall back to source modification time.
   Keep `mtime` / `mtime_iso` unchanged for cache invalidation and file diagnostics.
3. **Optimistic UI.** Update `state.items[i]` + render immediately; on a failed POST,
   roll back + toast.
4. **Suspected automation is intentionally simple.** A single-turn Session is suspected
   unless local `human_confirmed` metadata says otherwise. Confirmed sub-agents remain excluded
   by their source adapter; do not merge the two concepts.
5. **Record ID vs conversation ID.** A record is one transcript file and keeps its `id`,
   path and `[L#]` anchors forever; a conversation is the ordered set of records a rewind,
   resume or cross-file compaction produced. `conversation_id` / `conversation_current_id` /
   `conversation_records` are **additive**: no id is ever renamed or retargeted, and
   contradictory evidence dissolves a group back into single records. See
   [`docs/decisions/2026-09-20-conversation-identity.md`](docs/decisions/2026-09-20-conversation-identity.md).
6. **`state.json` stays keyed by record and is never re-keyed.** The conversation-level view
   is produced at read time by `session_identity.merge_conversation_state` -- one function,
   one place to change the policy. Starred is a union (earliest `starred_at`); archived is
   the current record's only; the current record's note is the conversation's note and older
   ones come back as `older_notes`; the title falls back to the newest older override and
   reports its source; `human_confirmed` is a union; a cached `brief` is never merged.
7. **No auth.** Binds `127.0.0.1` only.

## 3. API

| Endpoint | In | Out |
|---|---|---|
| `GET /api/sessions` | — | `[{id, project_path, jsonl_path, mtime, mtime_iso, size, recent_msgs, last_stop_reason, user_turn_count, custom_title, title_override, title_override_source_id, display_title, human_confirmed, scope, archived, archived_at, starred, starred_at, note, older_notes, conversation_id, conversation_current_id, conversation_records, forked_from_record_id?, forked_from_conversation_id?, spawned_from_conversation_id?}]`; the conversation-level state view lands on the current record only (see §2.6) |
| `GET /api/session-choices?source=claude` | optional source | Recent primary and single-turn candidates (up to 100 each); shared relationship and selection hints, title and recent user preview. Superseded records are not offered. No authorization changes. |
| `GET /api/search?q=…` | multi-word = AND; session title and ID match too | `[{id, conversation_id, conversation_current_id, snippets:[{text, role, term}]}]`; snippets stay per record so anchors remain traceable. Rows an Antigravity in-file rewind abandoned are not searched, matching what the reader shows |
| `GET /api/stats` | — | `{total, starred, recent, dusty, archived}` counted per **conversation**, so the bar and the list agree |
| `GET /api/sessions/:id/conversation` | optional `?fingerprint=<seen>`; `:id` may be a conversation id | `{id, project_path, custom_title, title_override, display_title, human_confirmed, note, older_notes, conversation_id, conversation_records, resolved_from_conversation_id?, total_lines, fingerprint, turns:[…]}`; when the file's `fingerprint` (mtime + size) still equals `<seen>`, answers `{id, unchanged: true, fingerprint}` without re-parsing (standalone live refresh). Antigravity adds `rewind_abandoned_rows` + `rewinds` — an in-file rewind is read as live history only, and this is how many raw lines it removed; the reader shows that count |
| `GET /api/sessions/:id/anchored` | — | Plain-text transcript with `[L#]` original-line anchors (for agents to read / download) |
| `GET /api/recent-files` / `GET /api/find-files` | Files panel | recent-changed / `fd` name search |
| `POST /api/sessions/:id/star` | `{starred: bool}` | `{id, …entry, addressed_id, conversation_members}` |
| `POST /api/sessions/:id/archive` | `{archived: bool, note?}` | `{id, …entry, addressed_id, conversation_members}` |
| `POST /api/sessions/:id/note` | `{note: string}` | `{id, …entry, addressed_id, conversation_members}` |
| `POST /api/sessions/:id/title` | `{title_override: string}` | `{id, …entry, addressed_id, conversation_members}`; empty clears the personal title |
| `POST /api/sessions/:id/human` | `{human_confirmed: bool}` | `{id, …entry, addressed_id, conversation_members}`; false clears the correction |

A POST body missing `starred` / `archived` defaults to `True`.

Every `:id` above accepts a **record id** or a **conversation id**. A record id always
resolves to exactly that record; a conversation id resolves to the conversation's current
record and the response says so (`resolved_from_conversation_id`). Writes land on the
current record; un-star also clears every starred member. `id` in a write response is the
record actually written, `addressed_id` is what the caller asked for.

## 4. Frontend routes

| URL | Mode | Notes |
|---|---|---|
| `/` | dashboard | cross-project timeline by default; optional project view; **one entry per conversation** — superseded records are reachable from the entry's record list, not drawn as peers |
| `/?session=<record id>` | standalone | that exact record, always. When it is no longer the conversation's current record the page says so and links the current one; it never redirects. Follows a running session in place (`startConvLive` polls `/conversation?fingerprint=…`, redraws only on change, keeps scroll / open state), and when the current record changes while the page is open it announces the move and offers the new record (`noticeConversationMoved`) |
| `/?session=<conversation id>` | standalone | the conversation's **current** record, so the link stays good across a later rewind. Links the UI generates use this form only when the entry really has several records (`shareId()`); a single-record session keeps its record id exactly as before |

## 5. Agent-facing CLI and Skill

`session_logbook_cli.py` is the stable read-only interface for Agents and does not require the
dashboard server. Its commands are:

0.2.0 additionally includes passive runtime observation. See
[`docs/runtime-observation.md`](docs/runtime-observation.md) for setup, cursors, and
validation status. The collector writes only Logbook-owned event metadata; source
transcripts remain read-only. Semantic judgments belong to the consuming Agent.

| Command | Outcome |
|---|---|
| `locate <target>` | Resolve a Session ID, a conversation ID, an exact JSONL path, or a bounded search query |
| `context <target>` | Emit the standard anchored transcript plus the next line cursor |
| `follow <target> --cursor-line N` | Emit from the previous cursor, repeating line N once to avoid missing a half-written record. Claude returns the full selected branch; `--delta` returns only the cursor onward plus the earlier anchors a rewind removed. Antigravity is cursor-based already and always reports that same `# REMOVED_BEFORE_CURSOR:` field -- the lines at or before the cursor that a later in-file rewind abandoned |
| `status <target>` | Report observed file/session metadata without guessing process liveness |
| `observe <target>` | Return runtime facts and conversation with independent cursors (Devin excluded) |
| `evidence <target> --line N` | Read bounded raw JSONL source around an anchor |
| `search <query>` | Search real User/Assistant messages with source/project/date/role filters |
| `recent` | List recently active Sessions when no target is known yet: `--since 6h`, `--by user`, source/project filters, title, and the dashboard's selection hints (single-turn Sessions and sub-agents are opt-in). Like `/api/session-choices`, it offers each conversation's **current** record only |

Every `<target>` accepts a record ID or a conversation ID. A record ID resolves to exactly
that record, always. A conversation ID resolves to that conversation's current record, and
`locate` / `status` report it as `resolved_from_conversation_id`, while `context` / `follow` /
`evidence` print a `# RESOLVED_FROM_CONVERSATION:` header line. Nothing is ever retargeted
silently. `locate` / `status` / `search` / `recent` also report `conversation_id` and, on `status`, the
per-record `conversation_runtime_observations` (runtime events are unioned at read time and
never re-keyed).

A cursor belongs to one physical record. `follow --delta` against a **conversation** ID whose
conversation has several records therefore needs `--cursor-source-path` to say which record the
cursor came from; without it the command returns the full selected branch and prints
`# DELTA_NOT_APPLIED:` rather than counting a bare line number against a transcript it may not
have come from.

The single packaged Skill is `skills/session-logbook/`. It routes Agent requests to this CLI;
do not add separate find/read/compress Skills or duplicate source parsing in Skill instructions.

## 6. Key constants (top of `server.py`)

| Constant | Default | Purpose |
|---|---|---|
| `PORT` | 47821 | listen port |
| `DUSTY_AFTER_DAYS` | 7 | backend scope cutoff (frontend can override) |
| `TAIL_BUFFER` | 300 KB | tail window for card previews |
| `RECENT_USER_N` / `RECENT_ASSISTANT_N` | 3 / 3 | how many of each to pull for previews |
| `CONV_USER_MAX` / `_ASSISTANT_MAX` / `_TOOL_RESULT_MAX` | 5000 / 10000 / 1500 | conversation-view truncation |
| `SEARCH_SNIPPET_CONTEXT` / `SEARCH_MAX_SNIPPETS` | 60 / 3 | search snippet sizing |

State lives at `~/.session-logbook/state.json`, with rotating backups under
`~/.session-logbook/backups/`. `save_state` reads the file back after writing and reports a
mismatch on stderr, and the backup follows `STATE_FILE` when it is rebound (a smoke-test
launcher gets its own `backups/` next to its temp state file rather than no backup at all).

## 7. Code map

| Location | Responsibility |
|---|---|
| `server.py` `extract_metadata` / `_scan_title_and_compactions` | card preview (first user + tailed user/assistant) + turn counts + custom title + the two pieces of identity evidence: `head_session_id` (a fork keeps the source's id in the file head) and `compaction_parent_uuids` (a compaction boundary whose parent record is not above it in this file) |
| `server.py` `extract_conversation` | conversation view (pairs tool_use/tool_result, filters thinking, detects skill injection) |
| `server.py` `annotate_conversations` / `conversation_index` / `conversation_for_record` / `conversation_state_targets` / `compaction_links` | conversation identity for the server: resolves cross-file compaction parents (bounded to sibling transcripts, memoized), stamps identity on cards, resolves a conversation ID to its current record for `_find_jsonl`, and picks the record a write lands on |
| `sources/session_identity.py` `conversation_identity` / `merge_conversation_state` | the single home for conversation identity and for folding per-record personal state into the conversation's view. Change the merge policy here and nowhere else |
| `sources/claude_desktop.py` `descriptor_memberships` | the raw, descriptor-level membership answer (prior CLI sessions + current, with rewind vs continuation), alongside the existing `annotate_sessions` rewind display fields |
| `server.py` `compute_scope` / `_effective_archived` | backend scope (pure function, unit-tested); `_effective_archived` derives archived state (explicit state > Codex file location) |
| `server.py` `load_scan_cache` / `save_scan_cache` / `CACHE_SCHEMA_VERSION` | persistent warm scan cache (`~/.session-logbook/scan-cache.json`): load on start + incremental scan. Bump the schema version on any meta-shape change, or stale caches break |
| `server.py` `search_sessions` / `_rg_prefilter` / `_rg_matching_lines` / `_search_session` | full-text search (ripgrep file prefilter → ripgrep streams only lines containing a term, JSON keys excluded → per-session AND match; whole-file Python fallback). See `docs/decisions/2026-09-19-search-matching-lines.md` |
| `server.py` `list_recent_files` / `find_files_by_name` | Files panel backends |
| `sources/codex_history.py` `load` | the single entry point for a Codex session's effective history (continued pages and forks stitched across rollout files, served from the history index when available). Readers must call `load`, never `resolve`; `tests/test_history_entry_point.py` enforces this. If another client starts splitting sessions across files, give its source module the same kind of single entry point and route its readers through it, rather than generalizing Codex's format |
| `sources/codex.py` `is_codex_path` / `CODEX_ARCHIVED_ROOT` | Codex (`~/.codex`) data source; `is_codex_path` is the centralized dual-root predicate (active `sessions` + `archived_sessions`) |
| `sources/antigravity.py` | Antigravity (`~/.gemini/antigravity`) data source; `is_antigravity_path` is the centralized directory-boundary-safe predicate; `rewind_plan` / `live_records` / `live_history` select the live history of an in-file rewind (a `step_index` drop counts only when that step slot is already held by a live row) and every reader here goes through them; `collect_turns` / `iter_messages` are the normalized feeds the anchored renderer and the Agent CLI consume, so neither restates this parsing |
| `sources/pi.py` | Pi JSONL selected-branch reader, metadata, search, and exports; no source writes. CLI follow returns the full branch; raw evidence retains physical line numbers. |
| `sources/kimi.py` `is_kimi_path` / `find_wire_by_session_id` | Kimi Code (`$KIMI_CODE_HOME`, default `~/.kimi-code`) data source; scans `sessions/*/*/agents/main/wire.jsonl` only (other agents are sub-agents); `is_kimi_path` is the directory-boundary-safe predicate |
| `sources/codex_history.py` `resolve` / `load` / `fork_lineage` | Codex session identity: one `session_meta.id` is one conversation, spread over one or more rollout files. `resolve` walks `history_base`, then adds any same-id page that link never reaches (a restart after an aborted turn writes one), placing it by record timestamps or reporting `unlinked_same_id_segment`. Each segment is labelled `current` / `continuation` / `inherited` / `unlinked`; `fork_lineage` reports a user fork's parent and never mistakes a sub-agent for one |
| `sources/anchored_transcript.py` | anchored-transcript renderer (`render_claude` / `render_codex` / `render_kimi` / `render_pi` / `render_antigravity`); the single source of truth behind the `/anchored` endpoint. `digest_header` names the source and, for Antigravity, how many raw lines a rewind abandoned; `line_ranges` is the one spelling for a bulk list of anchors |
| `session_logbook_cli.py` | read-only Agent access: resolve, search, anchored handoff, incremental follow, status, and evidence expansion |
| `skills/session-logbook/` | the single Agent-facing Skill; thin routing layer over `session_logbook_cli.py` |
| `index.html` `<style>` | all CSS (custom props in `:root`) |
| `index.html` `stripWorktree` / `projectKey` | path normalization + grouping keys |
| `index.html` `computeScope` | frontend scope override |
| `index.html` `setItems` / `findItem` / `currentItemFor` | the **only** id resolver: `setItems` is the one place `state.items` is replaced and it rebuilds the record and conversation indexes; `findItem` answers for either kind of id with the record always tried first; `currentItemFor` gives the entry a write applies to. Never reintroduce a linear `state.items.find(x => x.id === …)` — there were twelve, each free to drift |
| `index.html` `conversationItems` / `shareId` / `cardKey` | entries the list draws; the id a generated link uses; the localStorage collapse key |
| `index.html` `applySearchHits` | folds `/api/search` hits onto the entry that is drawn, tagging a hit found in a superseded record |
| `index.html` `render` / `renderCard` / `renderConv` | list, card, and conversation rendering. `renderConv` keeps `meta` (this record: size, time, source) and `stateMeta` (this conversation: star, archive, note, title) apart, and reads conversation facts from the response before the card so the modal and the standalone page behave identically |
| `index.html` `bindConvNav` | user-message navigation (j/k + goto + scroll state machine) |

## 8. Style

- UI reference: Notion / Linear — light, sans-serif, **readability over decoration**.
- CSS custom properties (`--bg`, `--text-1`, …) defined in `:root`.
- **No build step:** edit `index.html`, refresh the browser.
- **Before changing UI, read [`docs/design-system.md`](docs/design-system.md).** Colors and
  hover semantics are tokenized — pick by action category, not by what looks nice. Don't add
  new color variables without proving the existing tokens can't cover the case.

## 9. Tests

```bash
python3 -m unittest discover -s tests
python3 scripts/check_no_cjk.py
```

`tests/` uses Python `unittest` with synthetic fixtures. All tests must pass before merge;
CI runs the same command on every push and PR.

**Frontend behaviour is testable without a browser.** `tests/conversation_live_test.js` and
`tests/conversation_identity_frontend_test.js` read the real functions out of `index.html`
with `vm` and run them against stubs, so they test what ships rather than a copy. The
first one renders every reader case **twice — once as the modal, once as the full page** —
because a feature that works in only one of the two has shipped here before. Their Python
wrappers skip when Node is absent; `tests/test_conversation_identity_frontend.py` also
carries static guards that hold with no Node at all.

**Search is the primary capability; changing it has two extra gates.**

- `tests/test_search_contract.py` is the behavioural contract: one synthetic corpus with
  every source, a table of queries and expected sessions, and a check that the fast
  (ripgrep line-streaming) path and the whole-file path agree and that the fast path
  really ran. New search behaviour gets a row there.
- For any change meant to keep results identical, also run
  `python3 scripts/search_compare.py --baseline origin/staging --queries <file>` against
  the maintainer's real history before merging. It isolates state, cache and index, and
  writes results to `_private/` because they contain real session data.
- A fallback that silently takes over hides fast-path bugs, because both paths return
  correct results. Tests must assert which path ran, and a deliberate break of each
  safeguard should fail them.
- Source adapters that bind a data root as a default argument (for example
  `antigravity.scan_sessions`) ignore patched module constants; tests must pass the
  root explicitly, or they read the developer's real history.

CI's floor is Python 3.9 and its runners have no git identity. Before pushing, run the suite once
under a 3.9 interpreter too (on macOS, `/usr/bin/python3` is 3.9), and never let a test rely on
the developer's global git config — the `Z` timezone suffix and `git tag -a` without an identity
have both bitten here.

To smoke a pre-merge build against real local session data, start it through a launcher that
rebinds `server.STATE_FILE`, `server.SCAN_CACHE_FILE` and `server.SCAN_CACHE_BACKUP_DIR` to a temp
directory first; otherwise the unreleased build writes into the production `~/.session-logbook/`
state and cache.

`scripts/check_no_cjk.py` enforces the English-first rule over every tracked file and runs as
its own CI job. Run it before you commit — a local pre-commit hook is optional and easy to
bypass, so CI is the gate that actually holds.

## 10. Branch model and release flow

`main` is what the public gets. It is never edited directly; it only ever moves *forward along
`staging`* to a commit that has already been in daily use for a soak period.

```
feature branch ──PR──► staging ──deploy──► maintainer's machine ──14 days──► main ──(optional)──► vX.Y.Z release
```

- **`staging` is a one-way river.** Every change, hotfixes included, enters through a pull request
  into `staging`. CI runs on the PR and again on the `staging` push. Its history is never rewritten
  (no rebase, no force-push): the soak arithmetic below depends on commit order.
- **Deploying = recording.** On the machine that hosts the resident dashboard,
  `python3 scripts/release_flow.py deploy` fast-forwards the checkout to `staging`, restarts the
  service, reads it back over HTTP, and only then pushes an annotated `deployed/<date>-<sha>` tag.
  That tag date is when the soak clock starts for that commit and everything before it. The
  restart command and health URL are machine-specific and live in git-ignored `_private/deploy.json`
  (shape documented at the top of the script).
- **Checking = the session-start hook.** `.claude/settings.json` runs
  `scripts/release_flow.py check --quiet` whenever an agent session starts here, so "is anything
  ready for `main`?" is asked automatically when work resumes. It prints only when something is
  actionable: undeployed `staging` commits, a soaked commit `main` is behind, or a broken river.
- **Releasing to `main` = a pull request, merged by a human.** `python3 scripts/release_flow.py release`
  creates `release/<date>` at the newest deploy that has soaked ≥ 14 days and opens a PR into
  `main`. Commits deployed later stay on `staging` and keep soaking. The script never merges;
  `main` is branch-protected and merging it is a separate decision.
- **Versioned GitHub releases** (`vX.Y.Z`, see CONTRIBUTING "Maintainer releases") remain a
  separate, optional step taken from `main` after a promotion.
- **Planning is a local human-decision overlay.** Before proposing or taking a release action,
  read `_private/RELEASE-PLAN.md` when it exists. It records only the current intent, exclusions,
  temporary exceptions, and next human gate. Git refs, deployed tags, pull requests, and
  `release_flow.py` remain the sources for live facts. A missing or stale plan grants no authority,
  and updating the plan never authorizes a release action.

Rollback is per layer: the local machine goes back by checking out an earlier `deployed/*` tag and
restarting; a bad feature on `staging` is reverted with a new commit through a PR (never by
rewriting history); nothing on `main` is ever force-moved.

The pieces are deliberately split into a portable part (this section, the script, the hook — all
git-only) and a per-repository part (soak days, restart command, health URL), so another project
can adopt the same flow by copying the former and filling in the latter.

## 11. Decision log

Decisions backed by an experiment / comparison / measurement are recorded under
[`docs/decisions/`](docs/decisions/) — see that directory's README for the format.
