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
  ~/.local/share/devin/cli/sessions.db   (Devin Local, SQLite; DEVIN_DATA_DIR / XDG_DATA_HOME override)
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

> **Searching the Claude projects directory by hand:** that directory can itself sit inside
> a git repository (people version their `~/.claude` config), and its ignore rules routinely
> exclude the transcripts. A plain recursive `rg` there then walks past almost everything —
> on one maintainer machine a default walk saw 330 files where `-uu` saw 14,652. Pass the
> file paths explicitly, as `_rg_prefilter` and `_rg_matching_lines` do (they batch real
> paths after `--` and never hand rg a directory), or pass `-uu`. A search that quietly
> returns nothing looks exactly like a search that found nothing.

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
7. **A record that is not human speech is never drawn, counted or indexed as one.** Claude
   files carry three kinds that no person said: a queue entry with no delivered copy, a
   background-task notice, and an `api_error` retry run. Each renders as a quiet
   `.conv-system-event` row at its true position, and none of them reaches
   `user_turn_count`, `recent_msgs`, the first-user-message, `j`/`k` navigation, or a `[U#]`
   anchor. Unconfirmed queued text is searchable under the distinct role `queued`;
   notifications and errors are not indexed at all. Classification lives in
   `sources/claude_events.py` and keys only on markers the client wrote. See
   [`docs/decisions/2026-09-20-claude-event-records.md`](docs/decisions/2026-09-20-claude-event-records.md).
   **One rule decides what a human turn is**: `sources/claude_text.anchored_user_text`.
   The reader's `user` turns, `user_turn_count`, the history index's `user_turn`, `[U#]`,
   the card previews and search all ask it, so they count the same records. Not a turn:
   anything the client marks `isMeta`, the summary it marks `isCompactSummary` (a folded
   `summary` block in the reader, a `⚠ EVENT COMPACTION_SUMMARY` in the anchored
   transcript, not indexed), the interrupt marker it writes on Esc, a record holding only
   tool results. A turn: an image with no typed words, and typed text that shares a record
   with a tool result. Never answer "is this a person?" anywhere else, and never from
   message content alone — the content cannot show the record's flags. Previews call
   `anchored_user_text(record)`; search calls `human_turn_words(record)`, the same verdict
   without the `[image]` placeholder. See
   [`docs/decisions/2026-09-20-claude-human-turn-rule.md`](docs/decisions/2026-09-20-claude-human-turn-rule.md) and
   [`docs/decisions/2026-09-21-compaction-summary-and-record-level-previews.md`](docs/decisions/2026-09-21-compaction-summary-and-record-level-previews.md).
8. **No auth.** Binds `127.0.0.1` only.

## 3. API

| Endpoint | In | Out |
|---|---|---|
| `GET /api/sessions` | — | `[{id, project_path, jsonl_path, mtime, mtime_iso, size, recent_msgs, last_stop_reason, user_turn_count, custom_title, title_override, title_override_source_id, display_title, human_confirmed, scope, archived, archived_at, starred, starred_at, note, older_notes, conversation_id, conversation_current_id, conversation_records, forked_from_record_id?, forked_from_conversation_id?, spawned_from_conversation_id?}]`; the conversation-level state view lands on the current record only (see §2.6) |
| `GET /api/session-choices?source=claude` | optional source | Recent primary and single-turn candidates (up to 100 each); shared relationship and selection hints, title and recent user preview. Superseded records are not offered. No authorization changes. |
| `GET /api/search?q=…` | multi-word = AND; session title and ID match too | `[{id, conversation_id, conversation_current_id, snippets:[{text, role, term}]}]`; snippets stay per record so anchors remain traceable. Rows an Antigravity in-file rewind abandoned are not searched, matching what the reader shows |
| `GET /api/stats` | — | `{total, starred, recent, dusty, archived}` counted per **conversation**, so the bar and the list agree |
| `GET /api/sessions/:id/conversation` | optional `?fingerprint=<seen>`; `:id` may be a conversation id | `{id, project_path, custom_title, title_override, display_title, human_confirmed, note, older_notes, conversation_id, conversation_records, resolved_from_conversation_id?, total_lines, fingerprint, turns:[…]}`; when the file's `fingerprint` (mtime + size) still equals `<seen>`, answers `{id, unchanged: true, fingerprint}` without re-parsing (standalone live refresh). Antigravity adds `rewind_abandoned_rows` + `rewinds` — an in-file rewind is read as live history only, and this is how many raw lines it removed; the reader shows that count. Codex adds `source_files`, `context_complete`, `history_issues`, `forked_from` (a user branch) and `spawned_from` (a sub-agent's spawning session — lineage, never a gap) |
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

`note` and `older_notes` come back from `/conversation` for **every** member of a
conversation, including a superseded one. The note belongs to the conversation and a write
lands on the current record whichever member is addressed, so a deep link to an earlier
record has to show the same note the card shows — otherwise it reads as though the note had
been lost, behind an editor that silently writes somewhere else. `title_override` and
`human_confirmed` stay per-record on an earlier record, as before.

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
| `context <target>` | Emit the standard anchored transcript plus the next line cursor. `--from-turn U13` / `--last-turns N` cut it down to the human turns asked for, addressed by the very `[U#]` the transcript prints; the reply carries `TURN_SLICE` / `OMITTED_BEFORE_SLICE` so a reader always knows it holds a slice. An out-of-range turn is an error, never a silently complete transcript. `follow` takes neither: a turn number is not a continuation cursor, because each record of a conversation numbers from `U1` |
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

**`[L#]` is the durable anchor; `[U#]` is not.** A `[L#]` is a physical line in one file and
never moves. A `[U#]` is a position in the human-turn sequence, and that sequence is a product
of the current human-turn rule: the 2026-09-20/21 changes stopped counting harness
pseudo-messages, the interrupt marker and the compaction summary, so `[U#]` numbering shifted
on Sessions containing those records and will shift again if the rule changes. Anything an
Agent writes down, cites or hands to another Agent must be a source path plus a `[L#]`. Any
change to what counts as a turn has to say so in the changelog and bump
`claude_history.SCHEMA` and `CACHE_SCHEMA_VERSION`.

The single packaged Skill is `skills/session-logbook/`. It routes Agent requests to this CLI;
do not add separate find/read/compress Skills or duplicate source parsing in Skill instructions.

## 6. Key constants (top of `server.py`)

| Constant | Default | Purpose |
|---|---|---|
| `PORT` | 47821 | listen port |
| `DUSTY_AFTER_DAYS` | 7 | backend scope cutoff (frontend can override) |
| `TAIL_BUFFER` | 300 KB | tail window for card previews |
| `RECENT_USER_N` / `RECENT_ASSISTANT_N` | 3 / 3 | how many of each to pull for previews |
| `CONV_USER_MAX` / `_ASSISTANT_MAX` / `_TOOL_RESULT_MAX` | 200000 / 200000 / 1500 | conversation-view truncation |
| `SEARCH_SNIPPET_CONTEXT` / `SEARCH_MAX_SNIPPETS` | 60 / 3 | search snippet sizing |

State lives at `~/.session-logbook/state.json`, with rotating backups under
`~/.session-logbook/backups/`. `save_state` reads the file back after writing and reports a
mismatch on stderr, and the backup follows `STATE_FILE` when it is rebound (a smoke-test
launcher gets its own `backups/` next to its temp state file rather than no backup at all).

## 7. Code map

| Location | Responsibility |
|---|---|
| `server.py` `extract_metadata` / `_scan_title_and_compactions` | card preview (first user + tailed user/assistant) + turn counts + custom title + the two pieces of identity evidence: `head_session_id` (a fork keeps the source's id in the file head) and `compaction_parent_uuids` (a compaction boundary whose parent record is not above it in this file) |
| `server.py` `extract_conversation` | conversation view (pairs tool_use/tool_result, filters thinking, detects skill injection, emits the three Claude event rows) |
| `sources/claude_events.py` | the single home for Claude records that are events, not speech: an unconfirmed queue entry, a background-task notice, an `api_error` run. Every predicate keys on an explicit client marker (record type, `commandMode`, `origin.kind`, XML wrapper) — never on what the text reads like, because machine records contain prose in every language. `confirmed_queue_entries` is the shared "was this queued text ever handed over" rule; `ApiErrorRun` folds consecutive retries into one row. See `docs/decisions/2026-09-20-claude-event-records.md` |
| `sources/claude_text.py` `is_system_user_string` / `anchored_user_text` / `human_turn_words` / `normalize_record` | what counts as human text in a Claude record. `anchored_user_text` is the one rule for "is this a human turn": the reader (`server._claude_text_kind`), the card count (`server._selection_user_turn`), the card previews, the history index's `user_turn` and the anchored `[U#]` all call it; dashboard search and the CLI's message stream call `human_turn_words`, which drops the `[image]` placeholder. The prefix list and the interrupt-marker pattern live here, not in `server.py`, because a second copy is how those counters came to disagree. A change to what it returns moves cached values: bump `claude_history.SCHEMA` and `CACHE_SCHEMA_VERSION` |
| `server.py` `annotate_conversations` / `conversation_index` / `conversation_for_record` / `conversation_state_targets` / `compaction_links` | conversation identity for the server: resolves cross-file compaction parents (bounded to sibling transcripts, memoized), stamps identity on cards, resolves a conversation ID to its current record for `_find_jsonl`, and picks the record a write lands on |
| `sources/session_identity.py` `conversation_identity` / `merge_conversation_state` | the single home for conversation identity and for folding per-record personal state into the conversation's view. Change the merge policy here and nowhere else |
| `sources/claude_desktop.py` `descriptor_memberships` | the raw, descriptor-level membership answer (prior CLI sessions + current, with rewind vs continuation), alongside the existing `annotate_sessions` rewind display fields |
| `server.py` `compute_scope` / `_effective_archived` | backend scope (pure function, unit-tested); `_effective_archived` derives archived state (explicit state > Codex file location) |
| `server.py` `load_scan_cache` / `save_scan_cache` / `CACHE_SCHEMA_VERSION` | persistent warm scan cache (`~/.session-logbook/scan-cache.json`): load on start + incremental scan. Bump the schema version on any meta-shape change **and on any fix that changes the values `extract_metadata` produces for already-scanned sessions** — a warm entry is only refreshed when the file's mtime changes, so an unbumped build serves the old value forever. Two rules about the number itself. (1) When two branches each bump N → N+1 for different shape changes, git merges them into one N+1 with no conflict and neither cache is right for the other build: the integrator must take N+2. That happened at 12, which is why the running build is at 13 and above; the comment at the constant records what each number covers. (2) Do not bump when the output is byte-identical — a bump forces a full cold rescan on the next start, which is the long health wait §10 explains |
| `server.py` `search_sessions` / `_rg_prefilter` / `_rg_matching_lines` / `_search_session` | full-text search (ripgrep file prefilter → ripgrep streams only lines containing a term, JSON keys excluded → per-session AND match; whole-file Python fallback). See `docs/decisions/2026-09-19-search-matching-lines.md` |
| `server.py` `list_recent_files` / `find_files_by_name` | Files panel backends |
| `sources/codex_history.py` `load` | the single entry point for a Codex session's effective history (continued pages and forks stitched across rollout files, served from the history index when available). Readers must call `load`, never `resolve`; `tests/test_history_entry_point.py` enforces this. If another client starts splitting sessions across files, give its source module the same kind of single entry point and route its readers through it, rather than generalizing Codex's format |
| `sources/codex.py` `is_codex_path` / `CODEX_ARCHIVED_ROOT` | Codex (`~/.codex`) data source; `is_codex_path` is the centralized dual-root predicate (active `sessions` + `archived_sessions`) |
| `sources/antigravity.py` | Antigravity (`~/.gemini/antigravity`) data source; `is_antigravity_path` is the centralized directory-boundary-safe predicate; `rewind_plan` / `live_records` / `live_history` select the live history of an in-file rewind (a `step_index` drop counts only when that step slot is already held by a live row) and every reader here goes through them; `collect_turns` / `iter_messages` are the normalized feeds the anchored renderer and the Agent CLI consume, so neither restates this parsing |
| `sources/pi.py` | Pi JSONL selected-branch reader, metadata, search, and exports; no source writes. CLI follow returns the full branch; raw evidence retains physical line numbers. |
| `sources/kimi.py` `is_kimi_path` / `find_wire_by_session_id` | Kimi Code (`$KIMI_CODE_HOME`, default `~/.kimi-code`) data source; scans `sessions/*/*/agents/main/wire.jsonl` only (other agents are sub-agents); `is_kimi_path` is the directory-boundary-safe predicate |
| `sources/codex_history.py` `resolve` / `load` / `fork_lineage` / `spawn_lineage` | Codex session identity: one `session_meta.id` is one conversation, spread over one or more rollout files. `resolve` walks `history_base`, then adds any same-id page that link never reaches (a restart after an aborted turn writes one), placing it by record timestamps or reporting `unlinked_same_id_segment`. Each segment is labelled `current` / `continuation` / `inherited` / `unlinked`; `fork_lineage` reports a user fork's parent and never mistakes a sub-agent for one. `spawn_lineage` is the one place that recognizes a sub-agent rollout from its native markers: a spawned thread owns its whole record, so its `forked_from_id` is lineage rather than a missing prefix, and it is reported `complete` with `spawned_from` set (see `docs/decisions/2026-09-20-codex-subagent-completeness.md`) |
| `sources/anchored_transcript.py` | anchored-transcript renderer (`render_claude` / `render_codex` / `render_kimi` / `render_pi` / `render_antigravity`); the single source of truth behind the `/anchored` endpoint. `digest_header` names the source, the Codex fork and spawn lineage, and, for Antigravity, how many raw lines a rewind abandoned; `line_ranges` is the one spelling for a bulk list of anchors; `USER_TURN_RE` / `turn_anchors` / `slice_from_turn` are the one place a `[U#]` is read back as an address, covering every shape these renderers print (the `━━` banner, Pi's bare line, Devin's `##` heading), so a new renderer that invents a fourth shape must register it here rather than become unsliceable. `[U#]` counts only real human turns — a background-task notice, bash output, a teammate report, a slash-command injection, an unconfirmed queue entry or an `api_error` retry run becomes a `⚠ EVENT` marker line keeping its own `[L#]` |
| `session_logbook_cli.py` | read-only Agent access: resolve, search, anchored handoff, incremental follow, status, and evidence expansion |
| `skills/session-logbook/` | the single Agent-facing Skill; thin routing layer over `session_logbook_cli.py` |
| `index.html` `<style>` | all CSS (custom props in `:root`) |
| `index.html` `stripWorktree` / `projectKey` | path normalization + grouping keys |
| `index.html` `computeScope` | frontend scope override |
| `index.html` `setItems` / `findItem` / `currentItemFor` | the **only** id resolver: `setItems` is the one place `state.items` is replaced and it rebuilds the record and conversation indexes; `findItem` answers for either kind of id with the record always tried first; `currentItemFor` gives the entry a write applies to. Never reintroduce a linear `state.items.find(x => x.id === …)` — there were twelve, each free to drift |
| `index.html` `conversationItems` / `shareId` / `cardKey` | entries the list draws; the id a generated link uses; the localStorage collapse key |
| `index.html` `applySearchHits` | folds `/api/search` hits onto the entry that is drawn, tagging a hit found in a superseded record |
| `index.html` `render` / `renderCard` / `renderConv` / `renderConvTurn` | list, card, and conversation rendering. `renderConv` keeps `meta` (this record: size, time, source) and `stateMeta` (this conversation: star, archive, note, title) apart, and reads conversation facts from the response before the card so the modal and the standalone page behave identically. It also draws the conversation's note and the notes left on superseded records; `editSessionNote` reuses the card's dialog and write path, so one edit updates both surfaces and rolls both back |
| `index.html` `bindConvNav` | user-message navigation (j/k + goto + scroll state machine). Its targets are `.conv-user` only, which is why an event row can never be navigated to or counted |

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

**A test that touches the CLI or `scan_sessions()` must repoint every source root, not the
one it cares about.** Every root is a module-level constant read at call time today, so
`mock.patch.object` on the constant is enough — but a root left unpatched is silently the
developer's real library, and the test still passes. The full set:
`server.PROJECTS_DIR` (Claude Code), `codex.CODEX_ROOT` + `codex.CODEX_ARCHIVED_ROOT` +
`codex.SESSION_INDEX_PATH`, `antigravity.AG_ROOT` + `antigravity.AG_BRAIN`,
`kimi.KIMI_HOME` + `kimi.KIMI_SESSIONS_ROOT`, `pi.PI_SESSIONS_ROOT`, `devin.DEVIN_ROOT`,
and `claude_desktop.DESKTOP_ROOT` when descriptors are in play. `pi` and `devin` resolve
their environment variables at import, so patching the constant — not the environment — is
what works. `tests/test_search_contract.py` and `tests/test_session_logbook_cli.py` carry
the current lists; copy from one of them rather than assembling a new one.

CI's floor is Python 3.9 and its runners have no git identity. Before pushing, run the suite once
under a 3.9 interpreter too (on macOS, `/usr/bin/python3` is 3.9), and never let a test rely on
the developer's global git config — the `Z` timezone suffix and `git tag -a` without an identity
have both bitten here.

**Checking a change against the real library has three traps; each has cost a wrong number here.**

- *The library moves while you measure.* Other agents write Session files all day - a batch
  job added 1,500 in an hour during one audit. To compare two builds, freeze the file list
  once and give it to both runs (`scripts/audit_claude_human_turns.py --files-from LIST`);
  the list and any per-Session output belong in the main checkout's `_private/`.
- *Consumers that agree can be wrong together.* An audit that compares consumers with each
  other sees only disagreement. Where one needs a reference reading, write it in the audit
  without calling the rule under test (`_has_typed_words` in the audit script is one), and
  measure a replaced helper against its replacement in both directions - the direction the
  brief did not name was the larger one for `_user_text`.
- *A deliberate break must run the broken code.* When a script mutates a source file, runs
  the tests and restores the file within the same second, Python reuses the stale bytecode:
  same size, same mtime. The next mutation then reports the wrong test as its catcher. Run
  mutation checks with `python3 -B` and no `__pycache__`.

What a change does to the cards is measured, not derived from the rule: dump `extract_metadata`
for the frozen list from both trees and diff field by field. `user_turn_count` is counted over
the last `TAIL_BUFFER` of a file and clamped to at least 2 for a larger one, so a rule change
that "must" move the count often moves it on no Session at all.

To smoke a pre-merge build against real local session data, start it through a launcher that
rebinds `server.STATE_FILE`, `server.SCAN_CACHE_FILE` and `server.SCAN_CACHE_BACKUP_DIR` to a temp
directory first; otherwise the unreleased build writes into the production `~/.session-logbook/`
state and cache.

Those three are no longer enough. Two more files live in the same state directory and are
**not** module-level constants, so patching a `server.*` name does nothing for them: the
history index (`~/.session-logbook/history-index.sqlite3`, resolved per call by
`history_index.index_path()`, which holds both the Codex segment index and the Claude
per-file rows) and the runtime-events journal
(`~/.session-logbook/runtime-events.sqlite3`, resolved by `runtime_events.journal_path()`).
Each reads an environment variable, so the launcher must **set them in the environment
before the process starts**: `SESSION_LOGBOOK_HISTORY_INDEX` (a path, or the literal `off`
to disable it) and `SESSION_LOGBOOK_EVENTS`. This matters because a pre-merge build usually
carries a different `history_index.SCHEMA` or `claude_history.SCHEMA` from the resident
service; sharing one file means each build finds the other's rows stale and rewrites them,
so both keep rebuilding and neither result can be trusted. `scripts/search_compare.py` and
`scripts/audit_claude_human_turns.py` are the two working examples.

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
  From a worktree, name the resident checkout: `python3 scripts/release_flow.py --repo <checkout> deploy`
  (`--repo` goes before the subcommand). Afterwards read the live service back yourself as well -
  the scan-cache schema on disk, and one real Session through `/api/sessions/<id>/conversation` -
  because the script's health check proves only that something answers.
  The read-back waits up to `HEALTH_TIMEOUT_S` (180 s), which `_private/deploy.json` can raise or
  lower with an optional `health_timeout_s`. It is that long because a deploy that bumps
  `CACHE_SCHEMA_VERSION` makes the restarted service re-read every session file before it answers;
  the wait ends as soon as the service replies, so a warm restart is not slowed. If it still times
  out, nothing is tagged and running `deploy` again is safe — the second run meets a warm cache.
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
[`docs/decisions/`](docs/decisions/). That directory's
[README](docs/decisions/README.md) holds the format **and an index of every record**,
grouped so a newcomer can see which one answers their question. Start there rather than
with this file when the question is *why is it like this* — in particular, the nine records
dated 2026-09-19 to 2026-09-21 are one body of work on session identity (what counts as one
conversation, and what counts as one human turn) and are meant to be read together.
