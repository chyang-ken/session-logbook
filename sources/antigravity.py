"""Antigravity (Google Gemini IDE) data source.

The interface mirrors the existing Claude / Codex paths in server.py:
- scan_sessions(): yields main-line transcript.jsonl paths
- extract_metadata(jsonl_path): returns a meta dict aligned with Claude/Codex
- extract_conversation(jsonl_path): returns a list of turns
- extract_transcript(jsonl_path): token-optimized markdown export

Storage layout:
- The plaintext, turn-by-turn conversation lives at
  ~/.gemini/antigravity/brain/<conversation_id>/.system_generated/logs/transcript.jsonl
  (JSONL; each line has source(USER_EXPLICIT/MODEL/SYSTEM) + type + content/thinking/tool_calls)
- ~/.gemini/antigravity/conversations/*.pb is an encrypted copy of the same conversation
  (entropy ~8.0, key held inside the app); it can't and shouldn't be read — ignored.
- The title index is at ~/.gemini/antigravity/agyhub_summaries_proto.pb (plaintext protobuf, id→title).

Message mapping:
  USER_INPUT              → user (strips the <USER_REQUEST> wrapper)
  PLANNER_RESPONSE.content→ assistant
  PLANNER_RESPONSE.tool_calls → tool (FIFO-paired with the result lines that follow;
                                      exactly one result per call)
  everything else (non-skipped types) → tool result, including:
    CODE_ACTION(write/replace_file) / RUN_COMMAND / VIEW_FILE / LIST_DIRECTORY /
    GREP_SEARCH / INVOKE_SUBAGENT / SEARCH_WEB / READ_URL_CONTENT / MCP_TOOL /
    GENERIC(schedule/manage_task) / ERROR_MESSAGE(tool error). status==ERROR → is_error
  EPHEMERAL_MESSAGE/CONVERSATION_HISTORY/SYSTEM_MESSAGE/CHECKPOINT → skipped (pure noise)

Dispatch deliberately avoids a result-type whitelist: missing any new type would silently
drop a result and drift the pending FIFO (mis-pairing later results with earlier calls).
Hence the fallback rule: "anything that isn't skip/user/planner is a result."
"""
import json
import os
import re
import urllib.parse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional


AG_ROOT = Path.home() / ".gemini" / "antigravity"
AG_BRAIN = AG_ROOT / "brain"
AG_SUMMARIES = AG_ROOT / "agyhub_summaries_proto.pb"  # plaintext protobuf: id→title index

# Aligned with the top-of-file constants in server.py
RECENT_USER_N = 3
RECENT_ASSISTANT_N = 3
CONV_USER_MAX = 5000
CONV_ASSISTANT_MAX = 10000
CONV_TOOL_RESULT_MAX = 1500
CONV_TOOL_INPUT_MAX = 300
TRANSCRIPT_TOOL_RESULT_MAX = 200

_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
_PATH_RE = re.compile(r'/Users/[^\s":?*<>|\\]+')
_INTERNAL = '/.gemini/antigravity/'  # the agent's internal storage root (brain/steps/tasks, etc.)
# Antigravity's "default project directory" is ~/.gemini/antigravity/scratch; each subdirectory under it
# is a real user project (fanbox / lan_share / …) and must not be discarded as internal noise.
_SCRATCH_RE = re.compile(r'(.*?/\.gemini/antigravity/scratch/[^/\s"\\]+)')
_INTERNAL_BRAIN = '/.gemini/antigravity/brain/'

# Pure system noise: never a tool result, skip directly.
# Note that GENERIC is not in this list —— it was measured to be the result line of schedule /
# manage_task (carrying task status); skipping it as noise would leave the tool_call that issued it
# stuck in pending, causing FIFO mis-pairing.
_SKIP_TYPES = {
    'EPHEMERAL_MESSAGE', 'CONVERSATION_HISTORY', 'SYSTEM_MESSAGE',
    'CHECKPOINT'}
# Known tool-result types (for documentation/reference only; dispatch does not rely on a whitelist —— see the note below).
# CODE_ACTION(write/replace_file) / RUN_COMMAND / VIEW_FILE / LIST_DIRECTORY /
# GREP_SEARCH / INVOKE_SUBAGENT / SEARCH_WEB / READ_URL_CONTENT / MCP_TOOL /
# GENERIC(schedule/manage_task) / ERROR_MESSAGE(the result when a tool errors).
# Dispatch instead treats "any line that isn't skip/user/planner as a tool result" —— a whitelist that
# misses a new type would silently drop the result + drift the pending FIFO (a historical bug), so the
# fallback replaces enumeration.
_KNOWN_RESULT_TYPES = {
    'CODE_ACTION', 'RUN_COMMAND', 'VIEW_FILE', 'LIST_DIRECTORY',
    'GREP_SEARCH', 'INVOKE_SUBAGENT', 'SEARCH_WEB', 'READ_URL_CONTENT',
    'MCP_TOOL', 'GENERIC', 'ERROR_MESSAGE'}

# Title-index cache (agyhub_summaries_proto.pb is tiny; on mtime invalidation, re-read it wholesale)
_TITLE_CACHE = {"mtime": 0.0, "data": {}}


# ---------- Minimal protobuf decoding: used only for the title index ----------
def _pb_varint(b, i):
    shift = 0
    result = 0
    while True:
        x = b[i]
        i += 1
        result |= (x & 0x7f) << shift
        if not x & 0x80:
            break
        shift += 7
    return result, i


def _pb_fields(b):
    """Decode one level of protobuf fields: [(field_no, wire_type, payload), ...]. Tolerant; stop if it can't parse."""
    i = 0
    n = len(b)
    out = []
    while i < n:
        try:
            tag, i = _pb_varint(b, i)
        except IndexError:
            break
        wt = tag & 7
        fld = tag >> 3
        if wt == 0:
            v, i = _pb_varint(b, i)
            out.append((fld, 0, v))
        elif wt == 2:
            ln, i = _pb_varint(b, i)
            out.append((fld, 2, b[i:i + ln]))
            i += ln
        elif wt == 5:
            out.append((fld, 5, b[i:i + 4]))
            i += 4
        elif wt == 1:
            out.append((fld, 1, b[i:i + 8]))
            i += 8
        else:
            break
    return out


def _pb_strings(msg, depth=0, acc=None):
    """Recursively collect all readable UTF-8 string fields in a protobuf message."""
    if acc is None:
        acc = []
    for fld, wt, p in _pb_fields(msg):
        if wt == 2:
            try:
                s = p.decode('utf-8')
                if s.isprintable() or '\n' in s:
                    acc.append(s)
                    continue
            except Exception:
                pass
            if depth < 3 and len(p) > 1:
                _pb_strings(p, depth + 1, acc)
    return acc


def _load_titles() -> dict:
    """Parse agyhub_summaries_proto.pb → {conversation_id: title}. Cached by mtime. Returns {} on failure."""
    try:
        mtime = AG_SUMMARIES.stat().st_mtime
    except FileNotFoundError:
        _TITLE_CACHE["mtime"] = 0.0
        _TITLE_CACHE["data"] = {}
        return {}
    if mtime == _TITLE_CACHE["mtime"] and _TITLE_CACHE["data"]:
        return _TITLE_CACHE["data"]
    mapping = {}
    try:
        b = AG_SUMMARIES.read_bytes()
        for fld, wt, payload in _pb_fields(b):
            if wt != 2:
                continue
            ss = _pb_strings(payload)
            uid = next((s for s in ss if _UUID_RE.match(s)), None)
            if not uid:
                continue
            title = next(
                (s for s in ss if not _UUID_RE.match(s) and 2 < len(s) < 200), None)
            mapping[uid] = (title or "").strip()
    except Exception:
        mapping = {}
    _TITLE_CACHE["mtime"] = mtime
    _TITLE_CACHE["data"] = mapping
    return mapping


# ---------- Path / text cleanup ----------
def _conv_id(jsonl_path: Path) -> str:
    """Take <id> from brain/<id>/.system_generated/logs/transcript.jsonl."""
    try:
        return jsonl_path.parents[2].name
    except IndexError:
        return jsonl_path.stem


def _clean_user_input(content) -> str:
    """Strip the <USER_REQUEST> wrapper and system-injected blocks, leaving what the user actually said."""
    if not isinstance(content, str):
        return ""
    m = re.search(r'<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>', content, re.S)
    if m:
        return m.group(1).strip()
    content = re.sub(
        r'<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>', '', content, flags=re.S)
    content = re.sub(
        r'<USER_SETTINGS_CHANGE>.*?</USER_SETTINGS_CHANGE>', '', content, flags=re.S)
    return content.strip()


def _strip_result_header(content) -> str:
    """Remove the 'Created At: ...\\nCompleted At: ...' timestamp header at the start of a tool result."""
    if not isinstance(content, str):
        return ""
    return re.sub(
        r'^Created At:[^\n]*\n(Completed At:[^\n]*\n)?', '', content).strip()


def _is_error(d: dict) -> bool:
    """Whether a tool result failed: type ERROR_MESSAGE, or any result line with status==ERROR (RUN_COMMAND/MCP_TOOL, etc.)."""
    return d.get('type') == 'ERROR_MESSAGE' or d.get('status') == 'ERROR'


def _unquote(s):
    if isinstance(s, str) and len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1]
    return s or ""


def _tool_summary(name, args) -> str:
    """Take a one-line human-readable summary from a tool_call's args."""
    if not isinstance(args, dict):
        return name
    for k in ('toolSummary', 'toolAction'):
        if args.get(k):
            return _unquote(args[k])
    for k in ('DirectoryPath', 'AbsolutePath', 'TargetFile', 'FilePath',
              'Query', 'CommandLine'):
        if args.get(k):
            return _unquote(args[k])
    return name


def _project_root_of(p) -> Optional[str]:
    """Map a file/directory absolute path to its "project root". Returns None = an agent-internal path, to be ignored.

    - inside .system_generated / brain        → None (the agent's own storage)
    - scratch/<subdir>/…                       → scratch/<subdir> (Antigravity's default project area, a real project)
    - bare scratch root / other .gemini/antigravity internals → None
    - ordinary user path                       → returned as-is
    """
    if not isinstance(p, str) or not p:
        return None
    p = p.rstrip('/')
    if not p:
        return None
    if '/.system_generated/' in p or _INTERNAL_BRAIN in p:
        return None
    m = _SCRATCH_RE.match(p)
    if m:
        return m.group(1)
    if _INTERNAL in p:        # bare scratch root, annotations, knowledge, and other internal directories
        return None
    return p


def _workspace_uris(lines) -> Optional[str]:
    """Explicit workspaceUris (the workspace root opened in the IDE) —— the most authoritative; return on hit."""
    for d in lines:
        c = d.get('content')
        if isinstance(c, str) and 'workspaceUris' in c:
            block = c[c.index('workspaceUris'):]
            for m in re.finditer(r'file://(/Users/[^\s"\\]+)', block):
                root = _project_root_of(urllib.parse.unquote(m.group(1)))
                if root:
                    return root
    return None


def _tool_roots(lines):
    """Collect project-root candidates from tool-call arguments, in three reliability tiers:
      Cwd (run_command's actual working directory) > DirectoryPath (list_dir) > the directory of a file path.
    Returns three Counters. Cwd is the real cwd that Antigravity forces to land inside the workspace, so it's the most trustworthy.
    """
    cwd_roots, dir_roots, file_roots = Counter(), Counter(), Counter()
    for d in lines:
        if d.get('type') != 'PLANNER_RESPONSE':
            continue
        for tc in (d.get('tool_calls') or []):
            if not isinstance(tc, dict):
                continue
            nm = tc.get('name')
            a = tc.get('args')
            if not isinstance(a, dict):
                continue
            if nm == 'run_command':
                r = _project_root_of(_unquote(a.get('Cwd')))
                if r:
                    cwd_roots[r] += 1
            elif nm == 'list_dir':
                r = _project_root_of(_unquote(a.get('DirectoryPath')))
                if r:
                    dir_roots[r] += 1
            else:
                for k in ('AbsolutePath', 'TargetFile', 'FilePath'):
                    v = a.get(k)
                    if v:
                        r = _project_root_of(os.path.dirname(_unquote(v)))
                        if r:
                            file_roots[r] += 1
    return cwd_roots, dir_roots, file_roots


def _common_prefix_root(lines) -> str:
    """Take the common prefix of /Users paths scattered in the body text (fallback). First filter out agent internals, then commonpath, capped.
    Only reached when tool arguments have no path clue at all (the case of a path pasted into pure chat)."""
    dirs = []
    for d in lines:
        c = d.get('content')
        if not isinstance(c, str):
            continue
        for raw in _PATH_RE.findall(c):
            r = _project_root_of(os.path.dirname(urllib.parse.unquote(raw)))
            if r:
                dirs.append(r)
    if not dirs:
        return ""
    try:
        common = os.path.commonpath(dirs) if len(dirs) > 1 else dirs[0]
    except ValueError:
        common = dirs[0]
    # commonpath may collapse to a meaningless home-directory level like /Users/<user> —— treat as no project
    parts = common.split('/')
    if len(parts) <= 3:   # ['', 'Users', '<user>'] or shallower
        return ""
    if len(parts) > 5:    # converge to at most 4 directory levels
        common = '/'.join(parts[:5])
    return common


def _project_path(lines) -> str:
    """Infer the project root. Truth-source priority: explicit workspaceUris > the tool's actual landing point (Cwd/list_dir/file) > body-text path fallback.

    The old implementation only scanned body-text paths for a common prefix —— pure-chat sessions had no
    path → empty, cross-subtree sessions collapsed to the home directory, and it discarded real scratch
    projects as internal, causing many sessions to be misfiled under (unknown) or /Users/<user>.
    """
    ws = _workspace_uris(lines)
    if ws:
        return ws
    cwd_roots, dir_roots, file_roots = _tool_roots(lines)
    for c in (cwd_roots, dir_roots, file_roots):
        if c:
            return c.most_common(1)[0][0]
    return _common_prefix_root(lines)


def _read_lines(jsonl_path: Path):
    out = []
    try:
        with open(jsonl_path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                if line.strip():
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue
    except Exception:
        pass
    return out


def _truncate(text: str, n: int) -> str:
    if not text:
        return ""
    if len(text) <= n:
        return text
    return text[:n] + f"\n…(+{len(text)-n} chars)"


def search_text_from_line(d: dict) -> str:
    """Reused by server._search_session: extract searchable text from a line (user/assistant)."""
    t = d.get('type')
    if t == 'USER_INPUT':
        return _clean_user_input(d.get('content', ''))
    if t == 'PLANNER_RESPONSE':
        return (d.get('content') or '').strip()
    return ""


# ---------- Main interface ----------
def scan_sessions(root: Path = AG_BRAIN) -> Iterator[Path]:
    """Yield each conversation's transcript.jsonl path.

    Directory layout: brain/<conversation_id>/.system_generated/logs/transcript.jsonl
    """
    if not root.exists():
        return
    for conv_dir in root.iterdir():
        if not conv_dir.is_dir():
            continue
        tp = conv_dir / ".system_generated" / "logs" / "transcript.jsonl"
        if tp.exists():
            yield tp


def extract_metadata(jsonl_path: Path) -> Optional[dict]:
    """Antigravity transcript → meta dict (schema aligned with Claude/Codex)."""
    try:
        stat = jsonl_path.stat()
    except FileNotFoundError:
        return None
    cid = _conv_id(jsonl_path)
    lines = _read_lines(jsonl_path)

    users = [d for d in lines if d.get('type') == 'USER_INPUT']
    asts = [d for d in lines
            if d.get('type') == 'PLANNER_RESPONSE' and (d.get('content') or '').strip()]

    # recent_msgs: take the last N of user / assistant each, merged by ts ascending (same as Claude/Codex)
    raw = []
    for d in users[-RECENT_USER_N:]:
        raw.append((d.get('created_at') or '', 'user',
                    _clean_user_input(d.get('content', ''))))
    for d in asts[-RECENT_ASSISTANT_N:]:
        raw.append((d.get('created_at') or '', 'assistant',
                    (d.get('content') or '').strip()))
    raw.sort(key=lambda m: m[0])
    recent_msgs = [{"role": role, "text": text[:500]} for (_, role, text) in raw if text]

    first_user = _clean_user_input(users[0].get('content', '')) if users else ""

    # model: take the model switched to in the last USER_SETTINGS_CHANGE (best-effort)
    model = None
    for d in users:
        c = d.get('content')
        if isinstance(c, str):
            m = re.search(
                r'`?Model Selection`?\s*from\s*.*?\s+to\s+(.+?)\.(?=\s|$)', c)
            if m:
                model = m.group(1).strip()

    return {
        "id": cid,
        "project_path": _project_path(lines),
        "jsonl_path": str(jsonl_path),
        "mtime": stat.st_mtime,
        "mtime_iso": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        "size": stat.st_size,
        "source": "antigravity",
        "model": model,
        "cli_version": None,
        "custom_title": _load_titles().get(cid, ""),
        "user_turn_count": len(users),
        "last_stop_reason": None,
        "recent_msgs": recent_msgs,
        "first_user_msg": first_user or "",
    }


def extract_conversation(jsonl_path: Path) -> Optional[dict]:
    """Antigravity transcript → conversation-view dict, schema aligned with Claude/Codex.

    turns types: 'user' / 'assistant' / 'tool'
    """
    try:
        jsonl_path.stat()
    except FileNotFoundError:
        return None
    cid = _conv_id(jsonl_path)
    turns = []
    pending = []   # tool calls awaiting their result to be paired (FIFO)
    total_lines = 0
    all_lines = []

    with open(jsonl_path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            if not line.strip():
                continue
            total_lines += 1
            try:
                d = json.loads(line)
            except Exception:
                continue
            all_lines.append(d)
            t = d.get('type')
            ts = d.get('created_at', '')
            if t in _SKIP_TYPES:
                continue
            if t == 'USER_INPUT':
                txt = _clean_user_input(d.get('content', ''))
                if txt:
                    turns.append({
                        "type": "user",
                        "text": _truncate(txt, CONV_USER_MAX),
                        "ts": ts,
                    })
            elif t == 'PLANNER_RESPONSE':
                content = (d.get('content') or '').strip()
                if content:
                    turns.append({
                        "type": "assistant",
                        "text": _truncate(content, CONV_ASSISTANT_MAX),
                        "ts": ts,
                    })
                for tc in (d.get('tool_calls') or []):
                    nm = tc.get('name', '?')
                    pending.append({
                        "type": "tool",
                        "name": nm,
                        "summary": _truncate(
                            _tool_summary(nm, tc.get('args')), CONV_TOOL_INPUT_MAX),
                        "ts": ts,
                    })
            else:
                # Anything that isn't skip/user/planner is a tool result (including ERROR_MESSAGE /
                # GENERIC / SEARCH_WEB / MCP_TOOL / future new types). FIFO-pair with the pending
                # tool_call — measured to be exactly 1 result line per tool_call, no double results,
                # no concurrency.
                is_err = _is_error(d)
                res = d.get('error') or _strip_result_header(d.get('content', ''))
                if pending:
                    tc = pending.pop(0)
                    tc["result"] = _truncate(res, CONV_TOOL_RESULT_MAX)
                    tc["is_error"] = is_err
                    turns.append(tc)
                else:
                    # No tool_call awaiting pairing (e.g. a planner-level error) → a standalone result turn
                    turns.append({
                        "type": "tool",
                        "name": "error" if t == 'ERROR_MESSAGE' else t.lower(),
                        "summary": "",
                        "result": _truncate(res, CONV_TOOL_RESULT_MAX),
                        "is_error": is_err, "ts": ts,
                    })
    turns.extend(pending)  # append any unpaired tool calls (cut off by truncation) as-is

    return {
        "id": cid,
        "project_path": _project_path(all_lines),
        "custom_title": _load_titles().get(cid, ""),
        "total_lines": total_lines,
        "source": "antigravity",
        "turns": turns,
    }


def extract_transcript(jsonl_path: Path) -> str:
    """Token-optimized export: the same markdown structure as Claude/Codex.

    user / assistant are not truncated (in the export scenario the messages are the core signal);
    tool_result is truncated to 200 chars.
    """
    try:
        jsonl_path.stat()
    except FileNotFoundError:
        return ""
    cid = _conv_id(jsonl_path)
    out_blocks = []
    pending = []
    total_lines = 0
    all_lines = []

    def trunc_result(result: str) -> str:
        result = (result or "").strip()
        if len(result) > TRANSCRIPT_TOOL_RESULT_MAX:
            n = TRANSCRIPT_TOOL_RESULT_MAX
            nlines = result.count("\n") + 1
            result = result[:n].rstrip() + f" …[+{len(result)-n} chars, ~{nlines} lines]"
        return result

    def flush_tool(tc, result):
        summary = (tc.get("summary") or "").strip()
        if len(summary) > CONV_TOOL_INPUT_MAX:
            summary = summary[:CONV_TOOL_INPUT_MAX] + "…"
        head = f"[tool: {tc.get('name','?')}({summary})]"
        r = trunc_result(result)
        out_blocks.append(head + ("\n" + r + "\n" if r else "\n"))

    with open(jsonl_path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            if not line.strip():
                continue
            total_lines += 1
            try:
                d = json.loads(line)
            except Exception:
                continue
            all_lines.append(d)
            t = d.get('type')
            if t in _SKIP_TYPES:
                continue
            if t == 'USER_INPUT':
                txt = _clean_user_input(d.get('content', ''))
                if txt:
                    out_blocks.append(f"## USER\n{txt}\n")
            elif t == 'PLANNER_RESPONSE':
                content = (d.get('content') or '').strip()
                if content:
                    out_blocks.append(f"## ASSISTANT\n{content}\n")
                for tc in (d.get('tool_calls') or []):
                    nm = tc.get('name', '?')
                    pending.append({
                        "name": nm,
                        "summary": _tool_summary(nm, tc.get('args')),
                    })
            else:
                # Same as extract_conversation: anything that isn't skip/user/planner is treated as a
                # tool result, FIFO-paired with pending; ERROR_MESSAGE / GENERIC / SEARCH_WEB etc. all go here.
                res = d.get('error') or _strip_result_header(d.get('content', ''))
                if pending:
                    flush_tool(pending.pop(0), res)
                else:
                    name = "error" if t == 'ERROR_MESSAGE' else t.lower()
                    out_blocks.append(f"[tool: {name}]\n{trunc_result(res)}\n")
    for tc in pending:
        flush_tool(tc, "")

    project_path = _project_path(all_lines)
    header = (f"# Session {cid}\n"
              f"Project: {project_path}\n"
              f"Raw lines: {total_lines}\n\n")
    return header + "\n".join(out_blocks)
