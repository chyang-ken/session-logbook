"""Read-only Pi JSONL adapter.

Read the last persisted entry's parent chain, as Pi does when reopening a session.
Keep pre-compaction messages for historical reading; never migrate or rewrite logs.
Format: https://pi.dev/docs/latest/session-format
"""
import json
from sources.activity import activity_fields
import os
from datetime import datetime, timezone
from pathlib import Path


def sessions_root(env=None):
    env = os.environ if env is None else env
    agent = Path(env.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent").expanduser()
    return Path(env.get("PI_CODING_AGENT_SESSION_DIR") or agent / "sessions").expanduser()


PI_SESSIONS_ROOT = sessions_root()


def is_pi_path(path):
    try:
        Path(path).resolve().relative_to(PI_SESSIONS_ROOT.resolve())
        return True
    except (ValueError, OSError):
        return False


def read_session(path):
    """Return header, complete rows, active branch, and original physical line count."""
    rows, header, total = [], {}, 0
    with open(path, encoding="utf-8", errors="replace") as handle:
        for total, raw in enumerate(handle, 1):
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            if row.get("type") == "session":
                header = row
            else:
                rows.append((total, row))
    if not header.get("id"):
        raise ValueError("Missing Pi session header")
    # Legacy v1 files were linear; modern files persist a tree.
    if header.get("version", 1) == 1:
        return header, rows, rows, total
    index = {row["id"]: (line, row) for line, row in rows if row.get("id")}
    current = next((row["id"] for _, row in reversed(rows) if row.get("id")), None)
    branch, seen = [], set()
    while current in index and current not in seen:
        seen.add(current)
        line, row = index[current]
        branch.append((line, row))
        current = row.get("parentId")
    return header, rows, list(reversed(branch)), total


def scan_sessions():
    # Only the documented flat/custom and per-project layouts; do not descend into
    # extension-owned subagent directories with unknown identity semantics.
    for pattern in ("*.jsonl", "*/*.jsonl"):
        yield from PI_SESSIONS_ROOT.glob(pattern)


def find_session(session_id):
    for path in scan_sessions():
        if session_id not in path.stem:
            continue
        try:
            if read_session(path)[0]["id"] == session_id:
                return path
        except (OSError, ValueError):
            continue
    return None


def content_text(content, images=False):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block.get("text", "") if block.get("type") == "text" else
        "[image]" if images and block.get("type") == "image" else ""
        for block in content if isinstance(block, dict)
    ).strip()


def iter_messages(path):
    for line, row in read_session(path)[2]:
        if row.get("type") != "message":
            continue
        msg = row.get("message") or {}
        role = msg.get("role")
        if role in ("user", "assistant"):
            yield line, role, content_text(msg.get("content"))


def _facts(header, rows, branch):
    title, model, stop = "", None, None
    for _, row in rows:
        if row.get("type") == "session_info":
            title = row.get("name") or ""
    for _, row in branch:
        msg = row.get("message") or {}
        if row.get("type") == "model_change":
            model = row.get("modelId")
        if msg.get("role") == "assistant":
            model = msg.get("model") or model
            stop = msg.get("stopReason")
    return {"id": header["id"], "project_path": header.get("cwd") or "",
            "custom_title": title, "model": model, "last_stop_reason": stop}


def extract_metadata(path):
    try:
        header, rows, branch, _ = read_session(path)
        stat = Path(path).stat()
    except (ValueError, OSError):
        return None
    messages = []
    for line, row in branch:
        msg = row.get("message") or {}
        if row.get("type") == "message" and msg.get("role") in ("user", "assistant"):
            text = content_text(msg.get("content"), images=True)
            if text:
                messages.append({"role": msg["role"], "text": text,
                                 "ts": row.get("timestamp", ""), "line": line})
    users = [m for m in messages if m["role"] == "user"]
    recent_lines = {m["line"] for role in ("user", "assistant")
                    for m in [x for x in messages if x["role"] == role][-3:]}
    if users:
        recent_lines.add(users[0]["line"])
    recent = [{**m, "text": m["text"][:500], "is_first": bool(users and m is users[0])}
              for m in messages if m["line"] in recent_lines]
    return {**_facts(header, rows, branch), "jsonl_path": str(path),
            "mtime": stat.st_mtime, "mtime_iso": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            **activity_fields((m["ts"] for m in messages), stat.st_mtime),
            "size": stat.st_size, "source": "pi", "cli_version": None,
            "user_turn_count": len(users), "single_turn": len(users) == 1,
            "recent_msgs": recent, "first_user_msg": users[0]["text"] if users else ""}


def collect_turns(branch):
    """Normalize once for the reader, Markdown export, and anchored renderer."""
    from sources.anchored_transcript import _tool_input_summary
    turns, pending = [], {}
    for line, row in branch:
        kind = row.get("type")
        ts = row.get("timestamp") or ""
        if kind in ("compaction", "branch_summary"):
            turns.append({"type": "system_notification", "text": kind + ": " + (row.get("summary") or ""),
                          "ts": ts, "line": line})
            continue
        if kind == "custom_message":
            if row.get("display"):
                turns.append({"type": "system_notification", "text": content_text(row.get("content"), True),
                              "ts": ts, "line": line})
            continue
        if kind != "message":
            continue
        msg = row.get("message") or {}
        role = msg.get("role")
        text = content_text(msg.get("content"), images=True)
        if role in ("user", "assistant"):
            if text:
                turns.append({"type": role, "text": text, "ts": ts, "line": line})
            if role == "assistant":
                for block in msg.get("content") or []:
                    if not isinstance(block, dict) or block.get("type") != "toolCall":
                        continue
                    name = block.get("name") or "tool"
                    turn = {"type": "tool", "name": name,
                            "summary": _tool_input_summary(name, block.get("arguments") or {}),
                            "result": "", "is_error": False, "ts": ts, "line": line}
                    turns.append(turn)
                    pending[block.get("id")] = turn
                if msg.get("errorMessage"):
                    turns.append({"type": "system_notification", "text": msg["errorMessage"], "ts": ts, "line": line})
        elif role == "toolResult":
            turn = pending.pop(msg.get("toolCallId"), None)
            if turn is None:
                turn = {"type": "tool", "name": msg.get("toolName") or "tool",
                        "summary": "", "ts": ts, "line": line}
                turns.append(turn)
            turn.update(result=text, is_error=bool(msg.get("isError")), result_line=line)
        elif role == "bashExecution":
            turns.append({"type": "tool", "name": "bash", "summary": msg.get("command") or "",
                          "result": msg.get("output") or "", "is_error": bool(msg.get("cancelled") or msg.get("exitCode")),
                          "ts": ts, "line": line, "result_line": line})
    return turns


def extract_conversation(path):
    from sources.anchored_transcript import trunc
    header, rows, branch, total = read_session(path)
    turns = collect_turns(branch)
    for turn in turns:
        if turn["type"] == "tool":
            turn["summary"] = trunc(turn["summary"], 300)
            turn["result"] = trunc(turn["result"], 1500)
        else:
            turn["text"] = trunc(turn["text"], 5000 if turn["type"] == "user" else 10000)
    return {**_facts(header, rows, branch), "source": "pi", "total_lines": total, "turns": turns}


def extract_transcript(path):
    from sources.anchored_transcript import trunc
    blocks = []
    for turn in collect_turns(read_session(path)[2]):
        if turn["type"] == "tool":
            blocks.append("[tool: " + turn["name"] + "(" + trunc(turn["summary"], 300) + ")]\n"
                          + ("ERROR: " if turn["is_error"] else "") + trunc(turn["result"], 200))
        else:
            blocks.append("## " + turn["type"].upper() + "\n" + turn["text"])
    return "\n\n".join(blocks)


def search(path, terms, meta=None, title_override=""):
    """Search the selected branch only; metadata and messages share AND semantics."""
    meta = meta or extract_metadata(path) or {}
    titles = str(meta.get("custom_title") or "") + " " + title_override
    found = {term for term in terms if term in titles.lower()}
    snippets = [{"text": "Session title: " + (title_override or meta.get("custom_title", "")),
                 "role": "", "term": ""}] if found else []
    for _, role, text in iter_messages(path):
        for term in terms:
            pos = text.lower().find(term)
            if pos < 0:
                continue
            found.add(term)
            if len(snippets) < 3:
                snippets.append({"text": text[max(0, pos-60):pos+len(term)+60], "role": role, "term": term})
    if any(term in str(meta.get("id", "")).lower() for term in terms):
        return snippets or [{"text": "Session ID: " + meta["id"], "role": "", "term": ""}]
    return snippets if len(found) == len(terms) else []
