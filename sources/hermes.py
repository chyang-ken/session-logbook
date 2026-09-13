"""Hermes Agent data source (~/.hermes/state.db — a SQLite store).

Unlike Claude Code / Codex / Antigravity, Hermes does not write one jsonl file per
session: every conversation lives in a single SQLite database with two core tables.

- ``sessions``: one row per session (id, title, cwd, model, timestamps, archived, ...)
- ``messages``: one row per message in chat-completions shape
  (role user/assistant/tool, content, tool_calls JSON, tool_call_id, tool_name)

This module adapts that store to the same interface as sources/codex.py:

- ``list_sessions()``: cheap per-session listing (mtime/size) used by scan gating
- ``extract_metadata(pseudo_path)``: meta dict aligned with Claude / Codex
- ``extract_conversation(pseudo_path)``: conversation-view turns
- ``extract_transcript(pseudo_path)``: token-optimized markdown export
- ``read_rows(pseudo_path)``: normalized message rows for the anchored renderer
- search / evidence / lookup helpers used by server.py and session_logbook_cli.py

Pseudo-path scheme
------------------
The dashboard keys every session by its ``jsonl_path`` string, so a Hermes session
is addressed as ``"<state.db path>#<session id>"``. It is not a real file on disk;
``split_pseudo()`` / ``is_hermes_path()`` translate it back. A Hermes message's
stable source coordinate is its ``messages.id`` (AUTOINCREMENT row id), so anchored
transcripts cite ``[L#]`` = message id and evidence reads rows around that id.

Read discipline
---------------
Every access is strictly read-only (SQLite ``mode=ro`` URI plus ``query_only``), so
the dashboard can never corrupt a live Hermes store. Hermes runs WAL mode, which
lets readers work without blocking the running agent. Column names are probed with
``PRAGMA table_info`` before SQL is built, so a Hermes schema drift degrades to
fewer meta fields instead of crashing the scan.
"""
from __future__ import annotations

import json
import os
import sqlite3
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

# Root of the Hermes install. $HERMES_HOME wins when set (profiles / custom homes);
# tests patch this module constant to point at a synthetic store.
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
STATE_DB_NAME = "state.db"

# Aligned with the top-of-file constants in server.py (same as codex.py).
RECENT_USER_N = 3
RECENT_ASSISTANT_N = 3
CONV_USER_MAX = 5000
CONV_ASSISTANT_MAX = 10000
CONV_TOOL_RESULT_MAX = 1500
CONV_TOOL_INPUT_MAX = 300

# Transcript truncation strategy (export / brief input): user/assistant are not
# truncated; tool_result is truncated to 200, matching the other two sources.
TRANSCRIPT_TOOL_RESULT_MAX = 200

# Listing cache so frequent /api/sessions polls on an idle store cost only a stat().
# The stamp covers state.db AND its -wal sidecar: uncheckpointed WAL writes change
# only the sidecar, and missing that would serve a stale listing.
_LISTING_CACHE = {"stamp": None, "data": []}


# ───────────────────────── Pseudo-path helpers ─────────────────────────

def pseudo_path(db_path, session_id: str) -> str:
    """Build the dashboard's addressing string for one Hermes session."""
    return f"{db_path}#{session_id}"


def split_pseudo(path) -> Optional[tuple[Path, str]]:
    """Parse ``"<state.db path>#<session id>"``; None when it is not that shape."""
    raw = str(path)
    db_str, sep, sid = raw.rpartition("#")
    if not sep or not sid or not db_str.endswith(STATE_DB_NAME):
        return None
    return Path(db_str), sid


def is_hermes_path(path) -> bool:
    """Whether a path string is a Hermes pseudo-path (state.db#session_id)."""
    return split_pseudo(path) is not None


def state_db_paths() -> list[Path]:
    """Existing Hermes stores: the default home first, then ``profiles/*/state.db``."""
    paths: list[Path] = []
    try:
        root_db = HERMES_HOME / STATE_DB_NAME
        if root_db.is_file():
            paths.append(root_db)
        profiles_dir = HERMES_HOME / "profiles"
        if profiles_dir.is_dir():
            for child in sorted(profiles_dir.iterdir()):
                db = child / STATE_DB_NAME
                if child.is_dir() and db.is_file():
                    paths.append(db)
    except OSError:
        pass
    return paths


# ───────────────────────── SQLite access (read-only) ─────────────────────────

def _connect_ro(db_path: Path) -> Optional[sqlite3.Connection]:
    """Open one state.db strictly read-only; None when missing or unopenable."""
    uri = "file:" + urllib.parse.quote(str(db_path), safe="/:") + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
    except sqlite3.Error:
        return None
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Column names of one table; empty set when the table does not exist."""
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {str(r["name"]) for r in rows}


def _file_stamp(path: Path):
    try:
        st = path.stat()
        return (st.st_mtime, st.st_size)
    except OSError:
        return None


def _db_stamp(db_path: Path):
    return (
        str(db_path),
        _file_stamp(db_path),
        _file_stamp(Path(str(db_path) + "-wal")),
    )


def _column_or(columns: set[str], name: str, expr: str, alias: Optional[str] = None) -> str:
    """SELECT entry for one column, falling back to a literal when it is missing."""
    alias = alias or name
    if name in columns:
        return f"s.{name} AS {alias}"
    return f"{expr} AS {alias}"


def _session_select_sql(conn: sqlite3.Connection) -> str:
    """Sessions listing query with optional-column fallbacks.

    mtime semantics (shared by list_sessions and extract_metadata so scan gating
    never disagrees with the produced meta): the newest message timestamp, falling
    back to last_activity_at / ended_at / started_at.
    """
    s_cols = _table_columns(conn, "sessions")
    m_cols = _table_columns(conn, "messages")
    active_clause = "WHERE COALESCE(active, 1) = 1" if "active" in m_cols else ""
    ts_expr = "MAX(timestamp)" if "timestamp" in m_cols else "NULL"
    user_expr = (
        "SUM(CASE WHEN role = 'user' AND COALESCE(content, '') <> '' THEN 1 ELSE 0 END)"
        if "content" in m_cols
        else "SUM(CASE WHEN role = 'user' THEN 1 ELSE 0 END)"
    )
    size_expr = (
        "SUM(LENGTH(COALESCE(content, '')) + LENGTH(COALESCE(tool_calls, '')))"
        if "tool_calls" in m_cols
        else "SUM(LENGTH(COALESCE(content, '')))"
    )
    return (
        "SELECT "
        + ", ".join([
            "s.id AS id",
            _column_or(s_cols, "title", "NULL"),
            _column_or(s_cols, "cwd", "NULL"),
            _column_or(s_cols, "git_repo_root", "NULL"),
            _column_or(s_cols, "model", "NULL"),
            _column_or(s_cols, "archived", "0"),
            _column_or(s_cols, "hidden", "0"),
            _column_or(s_cols, "started_at", "0"),
            _column_or(s_cols, "ended_at", "NULL"),
            _column_or(s_cols, "last_activity_at", "NULL"),
            "m.max_ts AS max_ts",
            "COALESCE(m.n_user, 0) AS n_user",
            "COALESCE(m.byte_size, 0) AS byte_size",
        ])
        + " FROM sessions AS s LEFT JOIN ("
        + "SELECT session_id, "
        + f"{ts_expr} AS max_ts, {user_expr} AS n_user, {size_expr} AS byte_size "
        + "FROM messages " + active_clause + " GROUP BY session_id"
        + ") AS m ON m.session_id = s.id"
    )


def _session_row(conn: sqlite3.Connection, session_id: str) -> Optional[sqlite3.Row]:
    sql = _session_select_sql(conn) + " WHERE s.id = ?"
    row = conn.execute(sql, (session_id,)).fetchone()
    return row


def _session_mtime(row) -> float:
    for key in ("max_ts", "last_activity_at", "ended_at", "started_at"):
        value = row[key]
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return 0.0


def _mtime_iso(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()


def _normalize_project_path(cwd: str) -> str:
    """Return cwd as-is (only stripping a trailing slash); empty falls back to "~".

    Kept identical to codex._normalize_project_path: the frontend projectKey does
    the real grouping, and the two adapters must not diverge or the same repo would
    split into two groups depending on which agent ran there.
    """
    if not cwd:
        return "~"
    cwd = str(cwd).rstrip("/")
    return cwd or "~"


def _truncate(text: str, n: int) -> str:
    if not text:
        return ""
    if len(text) <= n:
        return text
    return text[:n] + f"\n…(+{len(text)-n} chars)"


def _ts_iso(ts) -> Optional[str]:
    """Epoch seconds (REAL) -> ISO string, matching the other sources' ts field."""
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _like_param(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# ───────────────────────── Scan: session listing ─────────────────────────

def list_sessions() -> list[dict]:
    """Cheap listing of every visible (non-hidden) session across all state DBs.

    Returns one dict per session carrying the pseudo ``jsonl_path``, ``mtime`` and
    ``size`` the scan uses to decide whether a cached meta is stale, plus the cheap
    column values extract_metadata reuses. Results are cached per untouched store
    stamp so repeated polls on an idle store cost a stat() per database.
    """
    db_paths = state_db_paths()
    stamp = tuple(_db_stamp(p) for p in db_paths)
    if stamp and stamp == _LISTING_CACHE["stamp"]:
        return _LISTING_CACHE["data"]

    listings: list[dict] = []
    ok = True
    for db_path in db_paths:
        conn = _connect_ro(db_path)
        if conn is None:
            ok = False
            continue
        try:
            if not _table_columns(conn, "sessions"):
                continue
            sql = _session_select_sql(conn)
            if "hidden" in _table_columns(conn, "sessions"):
                sql += " WHERE COALESCE(s.hidden, 0) = 0"
            for row in conn.execute(sql):
                mtime = _session_mtime(row)
                listings.append({
                    "db": str(db_path),
                    "id": row["id"],
                    "jsonl_path": pseudo_path(db_path, row["id"]),
                    "mtime": mtime,
                    "mtime_iso": _mtime_iso(mtime),
                    "size": int(row["byte_size"] or 0),
                    "n_user": int(row["n_user"] or 0),
                    "title": row["title"] or "",
                    "cwd": row["cwd"] or "",
                    "git_repo_root": row["git_repo_root"] or "",
                    "model": row["model"],
                    "archived": bool(row["archived"]),
                })
        except sqlite3.Error as e:
            print(f"[warn] hermes listing failed for {db_path}: {e}")
            ok = False
        finally:
            conn.close()
    if not ok:
        # Do not pin a partial/failed listing into the cache: the caller falls back
        # to whatever the last good scan produced, and the next scan retries.
        return listings
    _LISTING_CACHE["stamp"] = stamp
    _LISTING_CACHE["data"] = listings
    return listings


# ───────────────────────── Metadata (card preview) ─────────────────────────

def _recent_texts(conn, session_id: str, role: str, limit: int) -> list[tuple]:
    """Last ``limit`` non-empty messages of one role -> [(timestamp, id, role, text)]."""
    cols = _table_columns(conn, "messages")
    if "content" not in cols:
        return []
    rows = conn.execute(
        "SELECT id, content, timestamp FROM messages "
        "WHERE session_id = ? AND role = ? AND COALESCE(active, 1) = 1 "
        "AND COALESCE(content, '') <> '' ORDER BY id DESC LIMIT ?",
        (session_id, role, limit),
    ).fetchall()
    out = []
    for row in reversed(rows):
        out.append((row["timestamp"] or 0, row["id"], role, row["content"]))
    return out


def extract_metadata(pseudo) -> Optional[dict]:
    """Hermes session -> meta dict aligned with Claude / Codex.

    Card preview takes the last RECENT_USER_N user + RECENT_ASSISTANT_N assistant
    messages (same shape as the other adapters), merged in true time order.
    """
    parsed = split_pseudo(pseudo)
    if not parsed:
        return None
    db_path, session_id = parsed
    conn = _connect_ro(db_path)
    if conn is None:
        return None
    try:
        row = _session_row(conn, session_id)
        if row is None:
            return None

        users = _recent_texts(conn, session_id, "user", RECENT_USER_N)
        assistants = _recent_texts(conn, session_id, "assistant", RECENT_ASSISTANT_N)
        merged = sorted(users + assistants, key=lambda m: (m[0] or 0, m[1]))

        first_user = ""
        first = conn.execute(
            "SELECT content FROM messages WHERE session_id = ? AND role = 'user' "
            "AND COALESCE(active, 1) = 1 AND COALESCE(content, '') <> '' "
            "ORDER BY id ASC LIMIT 1",
            (session_id,),
        ).fetchone()
        if first is not None:
            first_user = first["content"]

        finish = conn.execute(
            "SELECT finish_reason FROM messages WHERE session_id = ? AND role = 'assistant' "
            "AND COALESCE(active, 1) = 1 AND COALESCE(finish_reason, '') <> '' "
            "ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()

        mtime = _session_mtime(row)
        return {
            "id": session_id,
            "project_path": _normalize_project_path(row["cwd"] or row["git_repo_root"] or ""),
            "jsonl_path": str(pseudo),
            "mtime": mtime,
            "mtime_iso": _mtime_iso(mtime),
            "size": int(row["byte_size"] or 0),
            "source": "hermes",
            # Archived state mirrors Hermes' own archive flag, exactly like
            # codex_archived derives from the file location: dashboard state, when
            # the user has touched it, still wins in _effective_archived.
            "hermes_archived": bool(row["archived"]),
            "model": row["model"],
            "custom_title": row["title"] or "",
            "user_turn_count": int(row["n_user"] or 0),
            "last_stop_reason": (finish["finish_reason"] if finish is not None else None),
            "recent_msgs": [
                {"role": role, "text": str(text)[:500]} for (_, _, role, text) in merged
            ],
            "first_user_msg": first_user,
        }
    except sqlite3.Error as e:
        print(f"[warn] hermes extract failed for {pseudo}: {e}")
        return None
    finally:
        conn.close()


# ───────────────────────── Conversation view ─────────────────────────

def _display_select(conn) -> str:
    cols = _table_columns(conn, "messages")
    entries = []
    for name, fallback in (
        ("id", "rowid"),
        ("role", "''"),
        ("content", "NULL"),
        ("tool_call_id", "NULL"),
        ("tool_calls", "NULL"),
        ("tool_name", "NULL"),
        ("timestamp", "NULL"),
        ("finish_reason", "NULL"),
        ("reasoning", "NULL"),
        ("active", "1"),
        ("compacted", "0"),
    ):
        entries.append(name if name in cols else f"{fallback} AS {name}")
    return "SELECT " + ", ".join(entries) + " FROM messages"


def _display_rows(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    """Active + compaction-preserved rows, deduped like Hermes' own display reads.

    In-place context compaction copies the protected tail into each new generation,
    so the same logical message can exist as several rows with different active
    flags and ids. Hermes' own transcript read keeps each message exactly once,
    preferring the live row then the newest generation; mirror that here so the
    dashboard transcript matches what Hermes shows.
    """
    cols = _table_columns(conn, "messages")
    if "compacted" in cols:
        scope = "(COALESCE(active, 1) = 1 OR COALESCE(compacted, 0) = 1)"
    else:
        scope = "COALESCE(active, 1) = 1"
    rows = [
        dict(r)
        for r in conn.execute(
            f"{_display_select(conn)} WHERE session_id = ? AND {scope} ORDER BY id ASC",
            (session_id,),
        )
    ]
    seen: dict[tuple, dict] = {}
    for row in rows:
        key = (row.get("role"), row.get("content"), row.get("timestamp"),
               row.get("tool_call_id"), row.get("tool_calls"), row.get("tool_name"))
        current = seen.get(key)
        norm = 0 if row.get("active") == 0 else 1
        if current is None:
            seen[key] = row
        else:
            cur_norm = 0 if current.get("active") == 0 else 1
            if (norm, row["id"]) > (cur_norm, current["id"]):
                seen[key] = row
    return sorted(seen.values(), key=lambda r: r["id"])


def _parse_tool_calls(raw) -> list[dict]:
    """tool_calls JSON (chat-completions shape) -> [{id, name, arguments}]."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    calls = []
    for item in data:
        if not isinstance(item, dict):
            continue
        fn = item.get("function")
        if not isinstance(fn, dict):
            fn = item
        calls.append({
            "id": item.get("id") or item.get("call_id") or "",
            "name": fn.get("name") or item.get("name") or "?",
            "arguments": fn.get("arguments", ""),
        })
    return calls


def _tool_input_summary(name: str, args_raw) -> str:
    """One-line tool intent; keeps the target path / command prefix like other sources."""
    try:
        args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
    except (json.JSONDecodeError, TypeError):
        return _truncate(str(args_raw), CONV_TOOL_INPUT_MAX)
    if not isinstance(args, dict):
        return _truncate(str(args), CONV_TOOL_INPUT_MAX)
    if name == "terminal":
        return _truncate(args.get("command", ""), 300)
    if name in ("read_file", "write_file", "patch"):
        return str(args.get("path", ""))
    if name == "search_files":
        return f"pattern={args.get('pattern', '')!r} path={args.get('path', '')}"
    if name == "skill_view":
        return f"[{args.get('name', '')}] {args.get('file_path', '') or ''}".strip()
    if name == "delegate_task":
        return _truncate(args.get("goal", ""), 150)
    if name in ("web_extract",):
        return _truncate(str(args.get("urls", "")), 150)
    if name in ("web_search",):
        return _truncate(args.get("query", ""), 150)
    keys = list(args.keys())
    return _truncate(
        "{" + ", ".join(f"{k}={_truncate(str(args[k]), 60)!r}" for k in keys[:5]) + "}",
        250,
    )


def _normalize_tool_content(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def _result_is_error(content) -> bool:
    """Structured failure detection only (no prose guessing), like the Codex source."""
    if not isinstance(content, str):
        return False
    try:
        value = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(value, dict):
        return False
    if value.get("is_error") is True or value.get("isError") is True:
        return True
    if value.get("success") is False or value.get("ok") is False:
        return True
    error = value.get("error")
    if error not in (None, "", [], {}):
        return True
    exit_code = value.get("exit_code")
    return isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0


def _turns_from_rows(rows: list[dict]) -> list[dict]:
    """Message rows -> conversation turns (user / assistant / tool), pairing calls."""
    turns: list[dict] = []
    pending: dict[str, dict] = {}
    for row in rows:
        role = row.get("role")
        ts = _ts_iso(row.get("timestamp"))
        content = row.get("content") or ""
        if role == "assistant":
            text = _normalize_tool_content(content).strip()
            if text:
                turns.append({
                    "type": "assistant",
                    "text": _truncate(text, CONV_ASSISTANT_MAX),
                    "ts": ts,
                })
            for call in _parse_tool_calls(row.get("tool_calls")):
                summary = _truncate(_tool_input_summary(call["name"], call["arguments"]),
                                    CONV_TOOL_INPUT_MAX)
                pending[call["id"]] = {"name": call["name"], "summary": summary}
        elif role == "tool":
            call_id = row.get("tool_call_id") or ""
            tc = pending.pop(call_id, None)
            name = (tc or {}).get("name") or row.get("tool_name") or "?"
            summary = (tc or {}).get("summary") or f"{name}()"
            turns.append({
                "type": "tool",
                "name": name,
                "summary": summary,
                "result": _truncate(_normalize_tool_content(content), CONV_TOOL_RESULT_MAX),
                "is_error": _result_is_error(content),
                "ts": ts,
            })
        elif role == "user":
            text = _normalize_tool_content(content).strip()
            if text:
                turns.append({
                    "type": "user",
                    "text": _truncate(text, CONV_USER_MAX),
                    "ts": ts,
                })
    return turns


def extract_conversation(pseudo) -> Optional[dict]:
    """Hermes session -> conversation-view dict, schema aligned with Claude / Codex."""
    parsed = split_pseudo(pseudo)
    if not parsed:
        return None
    db_path, session_id = parsed
    conn = _connect_ro(db_path)
    if conn is None:
        return None
    try:
        row = _session_row(conn, session_id)
        if row is None:
            return None
        rows = _display_rows(conn, session_id)
        return {
            "id": session_id,
            "project_path": _normalize_project_path(row["cwd"] or row["git_repo_root"] or ""),
            "custom_title": row["title"] or "",
            # "raw lines" analog for the modal meta line: the store counts messages,
            # not physical lines.
            "total_lines": len(rows),
            "source": "hermes",
            "turns": _turns_from_rows(rows),
        }
    except sqlite3.Error as e:
        print(f"[warn] hermes conversation failed for {pseudo}: {e}")
        return None
    finally:
        conn.close()


def read_rows(pseudo) -> Optional[list[dict]]:
    """Normalized message rows for one session (used by the anchored renderer)."""
    parsed = split_pseudo(pseudo)
    if not parsed:
        return None
    db_path, session_id = parsed
    conn = _connect_ro(db_path)
    if conn is None:
        return None
    try:
        if _session_row(conn, session_id) is None:
            return None
        return _display_rows(conn, session_id)
    except sqlite3.Error as e:
        print(f"[warn] hermes read failed for {pseudo}: {e}")
        return None
    finally:
        conn.close()


# ───────────────────────── Transcript export ─────────────────────────

def extract_transcript(pseudo) -> str:
    """Token-optimized markdown export using the same shape as Claude / Codex.

    user / assistant are not truncated; tool results truncate to
    TRANSCRIPT_TOOL_RESULT_MAX with the shared "…[+N chars, ~M lines]" suffix.
    """
    parsed = split_pseudo(pseudo)
    if not parsed:
        return ""
    db_path, session_id = parsed
    conn = _connect_ro(db_path)
    if conn is None:
        return ""
    try:
        row = _session_row(conn, session_id)
        if row is None:
            return ""
        rows = _display_rows(conn, session_id)
        pending: dict[str, dict] = {}
        out_blocks: list[str] = []
        for r in rows:
            role = r.get("role")
            content = r.get("content") or ""
            if role == "assistant":
                text = _normalize_tool_content(content).strip()
                if text:
                    out_blocks.append(f"## ASSISTANT\n{text}\n")
                for call in _parse_tool_calls(r.get("tool_calls")):
                    pending[call["id"]] = {
                        "name": call["name"],
                        "summary": _truncate(
                            _tool_input_summary(call["name"], call["arguments"]),
                            CONV_TOOL_INPUT_MAX,
                        ),
                    }
            elif role == "tool":
                tc = pending.pop(r.get("tool_call_id") or "", None)
                name = (tc or {}).get("name") or r.get("tool_name") or "?"
                summary = (tc or {}).get("summary") or ""
                result = _normalize_tool_content(content).strip()
                if len(result) > TRANSCRIPT_TOOL_RESULT_MAX:
                    n = TRANSCRIPT_TOOL_RESULT_MAX
                    lines = result.count("\n") + 1
                    result = result[:n].rstrip() + f" …[+{len(result)-n} chars, ~{lines} lines]"
                err_tag = " [ERROR]" if _result_is_error(content) else ""
                head = f"[tool: {name}({summary})]{err_tag}"
                out_blocks.append(head + ("\n" + result + "\n" if result else "\n"))
            elif role == "user":
                text = _normalize_tool_content(content).strip()
                if text:
                    out_blocks.append(f"## USER\n{text}\n")
        header = (
            f"# Session {session_id}\n"
            f"Project: {_normalize_project_path(row['cwd'] or row['git_repo_root'] or '')}\n"
            f"Raw lines: {len(rows)}\n\n"
        )
        return header + "\n".join(out_blocks)
    except sqlite3.Error as e:
        print(f"[warn] hermes transcript failed for {pseudo}: {e}")
        return ""
    finally:
        conn.close()


# ───────────────────────── Lookup / cursors / evidence ─────────────────────────

def pseudo_exists(pseudo) -> bool:
    """Whether the addressed session still exists in its store."""
    parsed = split_pseudo(pseudo)
    if not parsed:
        return False
    db_path, session_id = parsed
    conn = _connect_ro(db_path)
    if conn is None:
        return False
    try:
        return conn.execute(
            "SELECT 1 FROM sessions WHERE id = ? LIMIT 1", (session_id,)
        ).fetchone() is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def find_pseudo_by_id(session_id: str) -> Optional[str]:
    """Cold-start lookup by session id across all stores (standalone tab / CLI)."""
    for db_path in state_db_paths():
        conn = _connect_ro(db_path)
        if conn is None:
            continue
        try:
            hit = conn.execute(
                "SELECT 1 FROM sessions WHERE id = ? LIMIT 1", (session_id,)
            ).fetchone()
        except sqlite3.Error:
            hit = None
        finally:
            conn.close()
        if hit is not None:
            return pseudo_path(db_path, session_id)
    return None


def max_anchor(pseudo) -> int:
    """Newest message id in one session (the cursor space for follow / status)."""
    parsed = split_pseudo(pseudo)
    if not parsed:
        return 0
    db_path, session_id = parsed
    conn = _connect_ro(db_path)
    if conn is None:
        return 0
    try:
        row = conn.execute(
            "SELECT MAX(id) AS top FROM messages WHERE session_id = ?", (session_id,)
        ).fetchone()
        return int(row["top"] or 0) if row is not None else 0
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def evidence_lines(pseudo, anchor: int, context: int = 1) -> list[str]:
    """Raw store rows around one [L#] anchor (message id), one "[L#] {json}" per row.

    This is the Hermes analog of reading raw jsonl lines: the database row is the
    authoritative source, and its id is the anchor coordinate.
    """
    parsed = split_pseudo(pseudo)
    if not parsed:
        return []
    db_path, session_id = parsed
    conn = _connect_ro(db_path)
    if conn is None:
        return []
    try:
        low = max(1, anchor - max(0, context))
        high = anchor + max(0, context)
        rows = conn.execute(
            f"{_display_select(conn)} WHERE session_id = ? AND id BETWEEN ? AND ? "
            "ORDER BY id ASC",
            (session_id, low, high),
        ).fetchall()
        lines = []
        for row in rows:
            payload = {}
            for key in ("id", "role", "content", "tool_name", "tool_call_id",
                        "tool_calls", "timestamp", "finish_reason", "reasoning",
                        "active", "compacted"):
                value = dict(row).get(key)
                if isinstance(value, str) and len(value) > 2000:
                    value = value[:2000] + f" …[+{len(value)-2000} chars]"
                payload[key] = value
            lines.append(f"[L{row['id']}] " + json.dumps(payload, ensure_ascii=False))
        return lines
    except sqlite3.Error as e:
        print(f"[warn] hermes evidence failed for {pseudo}: {e}")
        return []
    finally:
        conn.close()


# ───────────────────────── Search ─────────────────────────

def search_session(pseudo, terms: list[str], role: str = "any") -> dict:
    """AND-search one session's user/assistant text; returns a result dict.

    Keys:
    - ``snippets``: up to 3 hits ({role, line=message id, text, term}); mirrors
      server._search_session sizing (±60 chars, whitespace collapsed). For an
      all-terms match (or a session-id match with ``role="any"``) this is the
      deliverable; partial matches come back empty.
    - ``found_terms``: every term observed in message text, so callers can apply
      their own AND gate or metadata fallback without rescanning.
    - ``id_hit``: the query matches the session id itself.

    Terms are prefiltered in SQL with LIKE for ASCII terms; non-ASCII terms are
    verified in Python only, avoiding SQLite's ASCII-only case folding.
    """
    empty = {"snippets": [], "found_terms": [], "id_hit": False}
    parsed = split_pseudo(pseudo)
    if not parsed:
        return empty
    db_path, session_id = parsed

    id_hit = any(term in session_id.lower() for term in terms)
    role_filter = [role] if role in ("user", "assistant") else ["user", "assistant"]

    conn = _connect_ro(db_path)
    if conn is None:
        return empty
    try:
        sql_terms = [t for t in terms if t.isascii()]
        where = [
            "session_id = ?",
            "COALESCE(active, 1) = 1",
            "role IN (%s)" % ", ".join("?" for _ in role_filter),
            "COALESCE(content, '') <> ''",
        ]
        params: list = [session_id, *role_filter]
        if sql_terms:
            # Prefilter rows matching ANY term (a superset). AND semantics are
            # session-level — the terms may live in different messages — so they are
            # enforced in Python below, never by narrowing one row with all terms.
            where.append(
                "(" + " OR ".join("content LIKE ? ESCAPE '\\'" for _ in sql_terms) + ")"
            )
            params.extend(_like_param(term) for term in sql_terms)
        rows = conn.execute(
            "SELECT id, role, content FROM messages WHERE " + " AND ".join(where)
            + " ORDER BY id ASC",
            params,
        )
        snippets: list[dict] = []
        term_found: set[str] = set()
        for row in rows:
            text = row["content"] or ""
            text_lower = text.lower()
            row_terms = [t for t in terms if t in text_lower]
            if not row_terms:
                continue
            term_found.update(row_terms)
            if len(snippets) < 3:
                first = min(text_lower.find(t) for t in row_terms)
                start = max(0, first - 60)
                end = min(len(text), first + max(len(t) for t in row_terms) + 60)
                snippet = " ".join(text[start:end].split())
                snippets.append({
                    "role": row["role"],
                    "line": row["id"],
                    "text": ("…" if start > 0 else "") + snippet
                            + ("…" if end < len(text) else ""),
                    "term": row_terms[0],
                })
            if len(term_found) == len(terms) and len(snippets) >= 3:
                break
    except sqlite3.Error as e:
        print(f"[warn] hermes search failed for {pseudo}: {e}")
        return empty
    finally:
        conn.close()

    if id_hit and not snippets and role == "any":
        snippets = [{"role": "", "line": 0,
                     "text": f"Session ID: {session_id}", "term": ""}]
    if not id_hit and len(term_found) < len(terms):
        return {"snippets": [], "found_terms": sorted(term_found), "id_hit": id_hit}
    return {"snippets": snippets, "found_terms": sorted(term_found), "id_hit": id_hit}


def iter_pseudo_paths() -> Iterator[str]:
    """Yield pseudo-paths for every visible session (CLI candidate enumeration)."""
    for listing in list_sessions():
        yield listing["jsonl_path"]
