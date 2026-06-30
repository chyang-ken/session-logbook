"""Codex data source (~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl).

The interface mirrors the existing Claude path in server.py:
- scan_sessions(): incremental scan, auto-filters subagents
- extract_metadata(jsonl_path): returns a meta dict aligned with Claude's
- extract_conversation(jsonl_path): returns a list of turns
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional


CODEX_ROOT = Path.home() / ".codex" / "sessions"

# Archived Codex sessions: after a user archives a session in Codex, its rollout file
# moves into this flat directory (no YYYY/MM/DD nesting), a sibling of CODEX_ROOT.
# Codex path detection must therefore recognize both roots through is_codex_path();
# checking only the CODEX_ROOT prefix would misclassify archived sessions as Claude.
CODEX_ARCHIVED_ROOT = Path.home() / ".codex" / "archived_sessions"

# Codex's self-maintained "session roster": each line is {id, thread_name, updated_at}.
# Its coverage is much higher than thread_name_updated events inside the rollout
# (measured 65% vs 12%), and both sides agree for the same session, so it is used
# as a fallback for custom_title.
SESSION_INDEX_PATH = Path.home() / ".codex" / "session_index.jsonl"

# Aligned with the top-of-file constants in server.py
RECENT_USER_N = 3
RECENT_ASSISTANT_N = 3
CONV_USER_MAX = 5000
CONV_ASSISTANT_MAX = 10000
CONV_TOOL_RESULT_MAX = 1500
CONV_TOOL_INPUT_MAX = 300

# Transcript truncation strategy (export / brief input): user/assistant are not truncated; tool_result is truncated to 200.
TRANSCRIPT_TOOL_RESULT_MAX = 200

# Used by extract_metadata: read head + tail segments instead of doing a full scan.
# A single Codex file can be 100MB+ (with base64 image / long image_url lines), so
# a full scan would stall cold start.
# HEAD 64KB: a single line was measured at up to 56KB (with image_url), and the
# first turn_context may sit around the 43KB mark.
# 8KB is too small and would miss the model. 64KB covers enough turn_context entries
# (even an extreme single 64KB line still reads at least 1 line).
HEAD_BUFFER = 64 * 1024
TAIL_BUFFER = 300 * 1024   # tail 300KB to find recent_msgs / stop_reason / thread_name

_INDEX_CACHE = {"mtime": 0.0, "data": {}}


def _under_root(path, root) -> bool:
    """Whether path is inside root, honoring directory boundaries rather than raw prefixes.

    A plain startswith would misclassify sibling directories; for example,
    `~/.codex/sessions_backup/x` matches the `~/.codex/sessions` prefix. Require either
    an exact root match or `root + separator`.
    """
    s = str(path)
    rs = str(root)
    return s == rs or s.startswith(rs + os.sep)


def is_codex_path(path) -> bool:
    """Whether path belongs to the Codex data source (active sessions or archived_sessions).

    server.py uses this for source detection instead of a fragile single-root startswith:
    the archived directory is a sibling of CODEX_ROOT, so checking only CODEX_ROOT would
    misclassify archived sessions as Claude.
    """
    return _under_root(path, CODEX_ROOT) or _under_root(path, CODEX_ARCHIVED_ROOT)


def _load_session_index() -> dict:
    """Read session_index.jsonl -> {id: thread_name}. Cached by mtime to avoid re-reading on every scan."""
    try:
        st = SESSION_INDEX_PATH.stat()
    except OSError:
        return _INDEX_CACHE["data"]
    if st.st_mtime == _INDEX_CACHE["mtime"]:
        return _INDEX_CACHE["data"]
    data = {}
    try:
        with open(SESSION_INDEX_PATH, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                sid = d.get("id")
                name = d.get("thread_name")
                if sid and name:
                    data[sid] = name
    except OSError:
        return _INDEX_CACHE["data"]
    _INDEX_CACHE["mtime"] = st.st_mtime
    _INDEX_CACHE["data"] = data
    return data


def _read_session_meta(jsonl_path: Path) -> Optional[dict]:
    """Read the first-line session_meta; return None if it's not a valid session_meta."""
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            line = f.readline()
        d = json.loads(line)
        if d.get("type") != "session_meta":
            return None
        return d.get("payload") or {}
    except Exception:
        return None


def _is_subagent(meta: dict, jsonl_path: Path) -> bool:
    """Decide whether a session is a subagent (guardian / worker thread_spawn / auto-review fallback)."""
    src = meta.get("source")
    if isinstance(src, dict):
        sub = src.get("subagent")
        if isinstance(sub, dict):
            if sub.get("other") == "guardian":
                return True
            if sub.get("thread_spawn"):
                return True
    # Fallback: scan the first 10 turn_context lines; model == codex-auto-review is treated as guardian-like
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= 10:
                    break
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") == "turn_context":
                    if (d.get("payload") or {}).get("model") == "codex-auto-review":
                        return True
    except Exception:
        pass
    return False


def _normalize_project_path(cwd: str) -> str:
    """Return cwd as-is (only stripping a trailing slash) so frontend projectKey groups uniformly.

    Previously /Users/<user>, /home/<user>, /root, /, ~, and empty cwd were all rewritten
    to the literal "~ (no project)", which split Claude (which keeps the real cwd
    /Users/alice) and Codex into separate frontend groups. Normalization is left to
    frontend projectKey. Empty cwd falls back to "~", treated as equivalent to running
    in home.
    """
    if not cwd:
        return "~"
    cwd = cwd.rstrip("/")
    return cwd or "~"


def _extract_text_from_message_content(content) -> str:
    """response_item.message.content list -> concatenated text.

    Strip obvious system-injected blocks:
    - <environment_context>...     cwd/shell/date injection
    - <permissions instructions>   sandbox / approval injection
    - # AGENTS.md instructions     AGENTS.md content injection (a leading input_text
      inside the user message, alongside the real user input. Not stripping it would
      double user_turn_count and render the card preview as if the user copied AGENTS.)

    Keep the remaining input_text / output_text.
    """
    if not isinstance(content, list):
        return str(content) if content else ""
    parts = []
    for c in content:
        if not isinstance(c, dict):
            continue
        ct = c.get("type")
        if ct in ("input_text", "output_text"):
            t = c.get("text", "")
            if isinstance(t, str):
                stripped = t.strip()
                if (stripped.startswith("<environment_context>")
                        or stripped.startswith("<permissions instructions>")
                        or stripped.startswith("# AGENTS.md instructions")):
                    continue
                parts.append(t)
    return "\n".join(parts).strip()


def _scan_lines_for_metadata(lines, is_tail: bool):
    """Extract metadata fields from a segment of lines.

    When is_tail=True, accumulate recent_msgs with ts so they can later be merged in
    time order; when is_tail=False (head segment), only take model / first_user / early
    thread_name. Returns a partial-field dict for the caller to merge.
    """
    out = {
        "model": None,
        "custom_title": None,
        "last_stop_reason": None,
        "user_count": 0,
        "first_user": None,
        # recent_msgs element: (ts, role, text). In the final merge stage, sort by ts and take the last N of each
        "recent_msgs": [],
    }
    for line in lines:
        try:
            d = json.loads(line)
        except Exception:
            continue
        t = d.get("type")
        p = d.get("payload") or {}
        ts = d.get("timestamp") or ""
        if t == "turn_context":
            m = p.get("model")
            if m:
                out["model"] = m
        elif t == "event_msg":
            et = p.get("type")
            if et == "thread_name_updated":
                nm = p.get("thread_name")
                if nm:
                    out["custom_title"] = nm
            elif et == "task_complete":
                out["last_stop_reason"] = "complete"
            elif et == "turn_aborted":
                out["last_stop_reason"] = "aborted"
            elif et == "error":
                out["last_stop_reason"] = "error"
        elif t == "response_item":
            rt = p.get("type")
            if rt == "message":
                role = p.get("role")
                text = _extract_text_from_message_content(p.get("content"))
                if not text:
                    continue
                if role == "user":
                    out["user_count"] += 1
                    if out["first_user"] is None:
                        out["first_user"] = text
                    if is_tail:
                        out["recent_msgs"].append((ts, "user", text))
                elif role == "assistant" and is_tail:
                    out["recent_msgs"].append((ts, "assistant", text))
    return out


def extract_metadata(jsonl_path: Path) -> Optional[dict]:
    """Codex jsonl -> meta dict (schema aligned with Claude + extra fields).

    Read head + tail segments to avoid a full scan. A single Codex file can be 1MB+
    (with base64 image), so a cold-start full scan of /api/sessions would stall. Strategy:
    - file <= HEAD+TAIL -> read it all
    - large file -> read the head HEAD_BUFFER (the early turn_context with the model is
      most likely here) + the tail TAIL_BUFFER (recent_msgs / last_stop_reason / the
      last thread_name are here)
    A thread_name change or model switch made in the middle may be missed; spec section 13
    accepts this trade-off.
    """
    meta_raw = _read_session_meta(jsonl_path)
    if meta_raw is None:
        return None

    try:
        stat = jsonl_path.stat()
    except FileNotFoundError:
        return None

    size = stat.st_size
    mtime = stat.st_mtime

    head_lines = []
    tail_lines = []
    try:
        with open(jsonl_path, "rb") as f:
            if size <= HEAD_BUFFER + TAIL_BUFFER:
                # Small file: read it all; the head segment and tail segment use the same data
                buf = f.read()
                text = buf.decode("utf-8", errors="replace")
                all_lines = [l for l in text.split("\n") if l.strip()]
                head_lines = all_lines
                tail_lines = all_lines
            else:
                head_bytes = f.read(HEAD_BUFFER)
                head_text = head_bytes.decode("utf-8", errors="replace")
                head_lines = [l for l in head_text.split("\n") if l.strip()]
                # Drop the possibly-incomplete last line at the end of head
                if head_lines and not head_text.endswith("\n"):
                    head_lines = head_lines[:-1]
                f.seek(-TAIL_BUFFER, os.SEEK_END)
                f.readline()  # Drop the possibly-incomplete first line of tail
                tail_bytes = f.read()
                tail_text = tail_bytes.decode("utf-8", errors="replace")
                tail_lines = [l for l in tail_text.split("\n") if l.strip()]
    except Exception:
        pass

    head_info = _scan_lines_for_metadata(head_lines, is_tail=False)
    tail_info = _scan_lines_for_metadata(tail_lines, is_tail=True)

    # Merge strategy:
    # - model uses the latest value from tail (latest switched model); fallback to head if tail has none
    model = tail_info["model"] or head_info["model"]
    # - custom_title uses the last value from tail; fallback to head for an early rename;
    #   if neither exists, fall back to ~/.codex/session_index.jsonl (Codex roster)
    custom_title = tail_info["custom_title"] or head_info["custom_title"]
    if not custom_title:
        sid = meta_raw.get("id")
        if sid:
            custom_title = _load_session_index().get(sid) or None
    # - last_stop_reason is always at the tail
    last_stop_reason = tail_info["last_stop_reason"]
    # - first_user should be in head unless the file is tiny and head=tail overlaps
    first_user = head_info["first_user"] or tail_info["first_user"]
    # - user_turn_count is exact inside tail; for large files, clamp to at least 2 like Claude
    user_turn_count = tail_info["user_count"]
    if size > HEAD_BUFFER + TAIL_BUFFER:
        user_turn_count = max(user_turn_count, 2)
    # - recent_msgs: take the last N per track (same as Claude), then merge by ts.
    #   Taking the last N user/assistant messages separately keeps tool-heavy sessions from
    #   pushing user input out of the preview. Sorting by ts shows the true conversation order.
    raw = tail_info["recent_msgs"]
    users_tail = [m for m in raw if m[1] == "user"][-RECENT_USER_N:]
    assistants_tail = [m for m in raw if m[1] == "assistant"][-RECENT_ASSISTANT_N:]
    merged = sorted(users_tail + assistants_tail, key=lambda m: m[0])

    cwd = meta_raw.get("cwd") or ""
    project_path = _normalize_project_path(cwd)

    recent_msgs = [{"role": role, "text": text[:500]} for (_, role, text) in merged]

    mtime_iso = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()

    return {
        "id": meta_raw.get("id"),
        "project_path": project_path,
        "jsonl_path": str(jsonl_path),
        "mtime": mtime,
        "mtime_iso": mtime_iso,
        "size": size,
        "source": "codex",
        # Archived status is derived from file location: under archived_sessions/ means
        # archived in Codex. server.enriched_sessions / compute_scope use this to place it
        # in Archived by default. _under_root honors directory boundaries to avoid sibling
        # directories such as archived_sessions_old.
        "codex_archived": _under_root(jsonl_path, CODEX_ARCHIVED_ROOT),
        "model": model,
        "cli_version": meta_raw.get("cli_version"),
        "custom_title": custom_title or "",
        "user_turn_count": user_turn_count,
        "last_stop_reason": last_stop_reason,
        "recent_msgs": recent_msgs,
        "first_user_msg": first_user or "",
    }


def _truncate(text: str, n: int) -> str:
    if not text:
        return ""
    if len(text) <= n:
        return text
    return text[:n] + f"\n…(+{len(text)-n} chars)"


def _normalize_tool_output(out) -> str:
    """Normalize the varied output field from function/custom tool outputs.

    - Usually it is a string (exec_command stdout, etc.)
    - view_image and similar tools return list[dict] with base64 image_url, which cannot .strip()
    - Occasionally it may be a dict or another type

    Normalize everything to string so downstream .strip() / len() / truncation is safe.
    input_image entries in lists become the placeholder `[image]`; other entries are JSON-dumped.
    """
    if out is None or out == "":
        return ""
    if isinstance(out, str):
        return out
    if isinstance(out, list):
        parts = []
        for item in out:
            if isinstance(item, dict):
                if item.get("type") == "input_image":
                    parts.append("[image]")
                elif "text" in item:
                    parts.append(str(item.get("text", "")))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False)[:200])
            else:
                parts.append(str(item)[:200])
        return "\n".join(parts)
    if isinstance(out, dict):
        return json.dumps(out, ensure_ascii=False)[:1000]
    return str(out)


def extract_conversation(jsonl_path: Path) -> Optional[dict]:
    """Codex jsonl -> conversation-view dict, schema aligned with Claude.

    Turn types: 'user' / 'assistant' / 'tool' / 'subagent_spawn'.
    Skips: developer message / reasoning / event_msg duplicates (user_message / agent_message) /
    turn_context / session_meta / compacted / token_count / other event_msg.
    """
    meta_raw = _read_session_meta(jsonl_path)
    if meta_raw is None:
        return None

    # First pass: collect function_call_output / custom_tool_call_output for pairing
    fc_outputs = {}  # call_id -> output text
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "response_item":
                    continue
                p = d.get("payload") or {}
                if p.get("type") in ("function_call_output", "custom_tool_call_output"):
                    cid = p.get("call_id")
                    if cid:
                        fc_outputs[cid] = _normalize_tool_output(p.get("output"))
    except Exception:
        pass

    turns = []
    custom_title = None
    total_lines = 0
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                total_lines += 1
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                t = d.get("type")
                p = d.get("payload") or {}
                ts = d.get("timestamp")

                if t == "event_msg":
                    et = p.get("type")
                    if et == "thread_name_updated":
                        nm = p.get("thread_name")
                        if nm:
                            custom_title = nm
                    elif et == "collab_agent_spawn_end":
                        # Actual field names have the new_ prefix (new_agent_nickname/new_agent_role/new_thread_id)
                        turns.append({
                            "type": "subagent_spawn",
                            "name": (
                                p.get("new_agent_nickname")
                                or p.get("new_agent_role")
                                or p.get("agent_nickname")
                                or p.get("agent_role")
                                or "worker"
                            ),
                            "description": _truncate(p.get("prompt", "") or "", 500),
                            "ts": ts,
                        })
                    # Other event_msg values (including user_message/agent_message duplicates, token_count,
                    # exec_command_end, etc.) are skipped for now
                    continue

                if t != "response_item":
                    # Skip session_meta / turn_context / compacted
                    continue

                rt = p.get("type")

                if rt == "message":
                    role = p.get("role")
                    if role == "developer":
                        continue  # system injection
                    text = _extract_text_from_message_content(p.get("content"))
                    if not text:
                        continue
                    if role == "user":
                        turns.append({
                            "type": "user",
                            "text": _truncate(text, CONV_USER_MAX),
                            "ts": ts,
                        })
                    elif role == "assistant":
                        turns.append({
                            "type": "assistant",
                            "text": _truncate(text, CONV_ASSISTANT_MAX),
                            "ts": ts,
                        })
                elif rt == "reasoning":
                    continue  # filtered to match Claude thinking handling
                elif rt == "function_call":
                    name = p.get("name", "?")
                    # spawn_agent is Codex's low-level hook for launching a subagent and is
                    # paired with event_msg.collab_agent_spawn_end. The latter has friendlier
                    # fields (for example new_agent_nickname like Averroes/Hypatia), so the UI
                    # uses that representation and this low-level call is skipped.
                    if name == "spawn_agent":
                        continue
                    args = p.get("arguments", "")
                    cid = p.get("call_id")
                    output = fc_outputs.get(cid, "") if cid else ""
                    summary = f"{name}({_truncate(args, CONV_TOOL_INPUT_MAX)})"
                    turns.append({
                        "type": "tool",
                        "name": name,
                        "summary": summary,
                        "result": _truncate(output, CONV_TOOL_RESULT_MAX),
                        "ts": ts,
                    })
                elif rt == "custom_tool_call":
                    name = p.get("name", "?")
                    inp = p.get("input", "")
                    cid = p.get("call_id")
                    output = fc_outputs.get(cid, "") if cid else ""
                    summary = f"{name}({_truncate(inp, CONV_TOOL_INPUT_MAX)})"
                    turns.append({
                        "type": "tool",
                        "name": name,
                        "summary": summary,
                        "result": _truncate(output, CONV_TOOL_RESULT_MAX),
                        "ts": ts,
                    })
                elif rt == "web_search_call":
                    action = p.get("action", {}) or {}
                    queries = action.get("queries") or [action.get("query", "")]
                    queries = [q for q in queries if q]
                    summary = "web_search(" + " | ".join(q[:80] for q in queries) + ")"
                    turns.append({
                        "type": "tool",
                        "name": "web_search",
                        "summary": _truncate(summary, CONV_TOOL_INPUT_MAX + 100),
                        "result": "",
                        "ts": ts,
                    })
                # Skip function_call_output / custom_tool_call_output / other types
    except Exception:
        pass

    cwd = meta_raw.get("cwd") or ""
    if not custom_title:
        sid = meta_raw.get("id")
        if sid:
            custom_title = _load_session_index().get(sid) or None
    return {
        "id": meta_raw.get("id"),
        "project_path": _normalize_project_path(cwd),
        "custom_title": custom_title or "",
        "total_lines": total_lines,
        "source": "codex",
        "turns": turns,
    }


def extract_transcript(jsonl_path: Path) -> str:
    """Token-optimized export using the same markdown shape as Claude extract_transcript.

    user / assistant are not truncated because messages are the core signal in export;
    tool_result is truncated to 200 characters. The key difference from extract_conversation
    is that export keeps messages untruncated and writes subagent_spawn as a
    ## SUBAGENT_SPAWN block rather than a turn type.
    """
    meta_raw = _read_session_meta(jsonl_path)
    if meta_raw is None:
        return ""

    # Pair function_call_output / custom_tool_call_output
    fc_outputs = {}
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "response_item":
                    continue
                p = d.get("payload") or {}
                if p.get("type") in ("function_call_output", "custom_tool_call_output"):
                    cid = p.get("call_id")
                    if cid:
                        fc_outputs[cid] = _normalize_tool_output(p.get("output"))
    except Exception:
        pass

    out_blocks = []
    total_lines = 0
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                total_lines += 1
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                t = d.get("type")
                p = d.get("payload") or {}

                if t == "event_msg":
                    et = p.get("type")
                    if et == "collab_agent_spawn_end":
                        name = (
                            p.get("new_agent_nickname")
                            or p.get("new_agent_role")
                            or "worker"
                        )
                        prompt = p.get("prompt", "") or ""
                        block = f"## SUBAGENT_SPAWN\nSpawned subagent: {name}"
                        if prompt:
                            block += f"\n{prompt}"
                        out_blocks.append(block + "\n")
                    continue

                if t != "response_item":
                    continue

                rt = p.get("type")
                if rt == "message":
                    role = p.get("role")
                    if role == "developer":
                        continue
                    text = _extract_text_from_message_content(p.get("content"))
                    if not text:
                        continue
                    if role == "user":
                        out_blocks.append(f"## USER\n{text}\n")
                    elif role == "assistant":
                        out_blocks.append(f"## ASSISTANT\n{text}\n")
                elif rt == "reasoning":
                    continue
                elif rt == "function_call":
                    name = p.get("name", "?")
                    if name == "spawn_agent":
                        continue  # Paired with collab_agent_spawn_end; the UI only shows the latter
                    args = p.get("arguments", "")
                    cid = p.get("call_id")
                    output = fc_outputs.get(cid, "") if cid else ""
                    summary = (args or "").strip()
                    if len(summary) > CONV_TOOL_INPUT_MAX:
                        summary = summary[:CONV_TOOL_INPUT_MAX] + "…"
                    result = (output or "").strip()
                    if len(result) > TRANSCRIPT_TOOL_RESULT_MAX:
                        n = TRANSCRIPT_TOOL_RESULT_MAX
                        lines = result.count("\n") + 1
                        result = result[:n].rstrip() + f" …[+{len(result)-n} chars, ~{lines} lines]"
                    head = f"[tool: {name}({summary})]"
                    out_blocks.append(head + ("\n" + result + "\n" if result else "\n"))
                elif rt == "custom_tool_call":
                    name = p.get("name", "?")
                    inp = p.get("input", "")
                    cid = p.get("call_id")
                    output = fc_outputs.get(cid, "") if cid else ""
                    summary = (inp or "").strip()
                    if len(summary) > CONV_TOOL_INPUT_MAX:
                        summary = summary[:CONV_TOOL_INPUT_MAX] + "…"
                    result = (output or "").strip()
                    if len(result) > TRANSCRIPT_TOOL_RESULT_MAX:
                        n = TRANSCRIPT_TOOL_RESULT_MAX
                        lines = result.count("\n") + 1
                        result = result[:n].rstrip() + f" …[+{len(result)-n} chars, ~{lines} lines]"
                    head = f"[tool: {name}({summary})]"
                    out_blocks.append(head + ("\n" + result + "\n" if result else "\n"))
                elif rt == "web_search_call":
                    action = p.get("action", {}) or {}
                    queries = action.get("queries") or [action.get("query", "")]
                    queries = [q for q in queries if q]
                    summary = " | ".join(q[:80] for q in queries)
                    out_blocks.append(f"[tool: web_search({summary})]\n")
    except Exception:
        pass

    cwd = meta_raw.get("cwd") or ""
    project_path = _normalize_project_path(cwd)
    # Codex filenames are rollout-<date>-<uuid>.jsonl, so stem is not the plain UUID.
    # Use session_meta.id to stay aligned with the card/API layer id.
    sid = meta_raw.get("id") or jsonl_path.stem
    header = (f"# Session {sid}\n"
              f"Project: {project_path}\n"
              f"Raw lines: {total_lines}\n\n")
    return header + "\n".join(out_blocks)


def find_rollout_by_session_id(session_id: str, root: Path = None):
    """Locate a rollout file by card ID. Return (path, forked_child_id).

    A card ID can mean one of two things:
    1. The `session_meta.id` of a rollout: direct hit, forked_child_id=None.
    2. The `parent_thread_id` of a child session forked by Codex Desktop: the parent thread
       root has no rollout file of its own, because forked sessions are written as child
       rollout files named with the child uuid and timestamp. Follow the lineage downward
       and match `parent_thread_id == card ID`. In that case forked_child_id is the selected
       child's real id so the UI can explain that the forked child was located. When multiple
       children exist, pick the newest one (largest mtime) as the likely main line.

    Return (None, None) when nothing is found. Never fall back to constructing a guaranteed
    nonexistent path like `rollout-<date>-<id>.jsonl`.

    Search active sessions plus archived_sessions. Archived sessions move to the latter, so
    conversation/export endpoints would otherwise 404. Active sessions are searched first,
    so a direct duplicate id prefers the active copy. The `root` parameter is for tests and
    single-root injection; when provided, only that root is searched.

    Two-phase lookup for performance: direct hit is the cold-start path for standalone tabs
    with an empty cache. Codex filenames look like rollout-<date>-<uuid>.jsonl, and uuid is
    session_meta.id in observed data. First prefilter by filename substring and open only
    files whose names contain the id, avoiding a full sessions tree scan for one lookup.
    The second phase performs a full scan only as a fallback: it handles unexpected filenames
    that do not contain id and parent_thread_id fork lineage, which is not present in filenames.
    """
    if root is not None:  # test/config single-root override
        roots = [root]
    else:  # read both roots at call time, so tests/config can override them
        roots = [CODEX_ROOT, CODEX_ARCHIVED_ROOT]
    existing = [r for r in roots if r.exists()]

    # Phase 1: direct hit, with filename prefilter (uuid is glob-safe hex plus dashes).
    # Most cold starts return here and open only one file. Root order is active, archived,
    # so duplicate ids prefer the active copy.
    for r in existing:
        for p in r.rglob(f"rollout-*{session_id}*.jsonl"):
            meta = _read_session_meta(p)
            if meta and meta.get("id") == session_id:
                return p, None

    # Phase 2: full fallback only after direct hit fails (unknown id or fork). Check both id
    # (so correctness does not depend on filename conventions) and parent_thread_id.
    children = []  # (is_active, mtime, path_str, path, child_id)
    for r in existing:
        is_active = (r == CODEX_ROOT)
        for p in r.rglob("rollout-*.jsonl"):
            meta = _read_session_meta(p)
            if not meta:
                continue
            if meta.get("id") == session_id:
                return p, None  # direct-hit fallback
            if meta.get("parent_thread_id") == session_id:
                try:
                    mt = p.stat().st_mtime
                except OSError:
                    mt = 0.0
                children.append((is_active, mt, str(p), p, meta.get("id")))
    if children:
        # active first > newest mtime > path name; archived child sessions do not steal the main line
        children.sort(key=lambda c: (c[0], c[1], c[2]), reverse=True)
        _is_active, _mt, _ps, p, child_id = children[0]
        return p, child_id
    return None, None


def scan_sessions(root: Path = None) -> Iterator[Path]:
    """Yield mainline jsonl paths, filtering out subagents.

    By default this scans two roots:
    - active: CODEX_ROOT/YYYY/MM/DD/rollout-*.jsonl
    - archived: CODEX_ARCHIVED_ROOT/rollout-*.jsonl (flat, no date nesting)
    The `root` parameter is for tests and single-root injection; when provided, only that
    root is scanned.
    """
    roots = [root] if root is not None else [CODEX_ROOT, CODEX_ARCHIVED_ROOT]
    for r in roots:
        if not r.exists():
            continue
        for jsonl_path in r.rglob("rollout-*.jsonl"):
            meta = _read_session_meta(jsonl_path)
            if meta is None:
                continue
            if _is_subagent(meta, jsonl_path):
                continue
            yield jsonl_path
