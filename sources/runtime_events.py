"""Local observation journal. Records facts without interpreting task success."""

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


SOURCES = ("claude", "codex", "kimi", "pi")
SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL, session_id TEXT NOT NULL,
    observed_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS session_events ON events(source, session_id, seq);
"""


def journal_path():
    return Path(os.environ.get("SESSION_LOGBOOK_EVENTS", str(
        Path.home() / ".session-logbook" / "runtime-events.sqlite3")))


def record(source, payload, path=None):
    if source not in SOURCES:
        raise ValueError("unsupported runtime source")
    session = payload.get("session_id")
    event = payload.get("hook_event_name")
    if not isinstance(session, str) or not session or not isinstance(event, str) or not event:
        raise ValueError("session_id and hook_event_name are required")
    # Keep identity and event facts only. Conversation text stays in its source.
    fields = ("hook_event_name", "session_id", "turn_id", "turnId", "agent_id",
              "parent_session_id", "transcript_path", "timestamp", "reason",
              "stop_hook_active", "tool_name", "tool_use_id", "run_id", "stop_reasons")
    facts = {key: payload[key] for key in fields if key in payload}
    encoded = json.dumps(facts, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > 65536:
        raise ValueError("event metadata exceeds 64 KiB")
    path = Path(path or journal_path())
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(str(path), timeout=2)) as db, db:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO events(source,session_id,observed_at,payload) VALUES(?,?,?,?)",
                   (source, session, datetime.now(timezone.utc).isoformat(), encoded))


def read(source, session_id, after=0, limit=50, path=None):
    if after < 0 or not 1 <= limit <= 200:
        raise ValueError("invalid event cursor or limit")
    path = Path(path or journal_path())
    result = {"events": [], "next_event_cursor": after, "has_more": False,
              "collection": "no_events_observed", "liveness": "unknown"}
    if not path.exists():
        return result
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            known = db.execute("SELECT 1 FROM events WHERE source=? AND session_id=? LIMIT 1",
                               (source, session_id)).fetchone()
            rows = db.execute(
                "SELECT seq,observed_at,payload FROM events WHERE source=? AND session_id=? "
                "AND seq>? ORDER BY seq LIMIT ?", (source, session_id, after, limit + 1)).fetchall()
    except sqlite3.Error as exc:
        # A failed/initializing journal must not hide independently readable logs.
        result.update(collection="unavailable", error=type(exc).__name__)
        return result
    result["collection"] = "events_observed" if known else "no_events_observed"
    result["has_more"] = len(rows) > limit
    for seq, observed, payload in rows[:limit]:
        result["events"].append({"cursor": seq, "observed_at": observed, "facts": json.loads(payload)})
        result["next_event_cursor"] = seq
    return result


def native_events(path, source, after=0, limit=50):
    """Read complete physical records. Unfinished final lines are retried next time."""
    if after < 0 or not 1 <= limit <= 200:
        raise ValueError("invalid native cursor or limit")
    names = {"codex": {"task_started", "task_complete", "turn_aborted", "error"},
             "kimi": {"turn.prompt", "turn.started", "turn.ended", "turn.cancel"}}
    result = {"events": [], "next_line_cursor": after, "has_more": False}
    if source not in names:
        return result
    with Path(path).open("rb") as stream:
        for line, raw in enumerate(stream, 1):
            if line <= after:
                continue
            if not raw.endswith(b"\n"):
                break
            try:
                row = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                result["next_line_cursor"] = line
                continue
            if not isinstance(row, dict):
                result["next_line_cursor"] = line
                continue
            event = row.get("payload", {}) if source == "codex" else row
            if source == "codex" and row.get("type") != "event_msg":
                event = {}
            if isinstance(event, dict) and event.get("type") in names[source]:
                if len(result["events"]) == limit:
                    result["has_more"] = True
                    break
                keys = ("type", "turn_id", "turnId", "agentId", "reason", "timestamp", "time")
                result["events"].append({"line": line, "facts": {
                    key: event[key] for key in keys if key in event},
                    "timestamp": row.get("timestamp", row.get("time"))})
            result["next_line_cursor"] = line
    return result
