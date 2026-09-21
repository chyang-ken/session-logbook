#!/usr/bin/env python3
"""Read-only command-line access to local Claude Code, Codex, Kimi Code, Antigravity, Devin Local, and Pi sessions.

This is the stable Agent-facing surface for Session Logbook. It reuses the
dashboard's source adapters and anchored renderer, but does not require the web
server to be running.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import server
from sources.activity import activity_time
from sources import anchored_transcript, session_identity, codex_history, claude_history
from sources import antigravity as ag_source
from sources import claude_events
from sources.claude_text import human_turn_words
from sources import codex as codex_source
from sources import devin as devin_source
from sources import kimi as kimi_source
from sources import pi as pi_source


SUPPORTED_SOURCES = ("claude", "codex", "kimi", "antigravity", "devin", "pi")
# A rendered line's own anchor is always at its start, on its own or behind the user-turn
# banner. Matching `[L#]` anywhere instead would let message text decide where a cursor
# lands: User and Assistant messages are preserved in full, so one that quotes an anchor
# (pasting an anchored transcript into a conversation does exactly that, and real local
# history contains it) would silently drop the rest of that message from a follow.
ANCHOR_RE = re.compile(r"^(?:━+ )?(?:\[U\d+\] )?\[L(\d+)\]")


class SessionLookupError(Exception):
    """Base error for a target that cannot be resolved safely."""


class SessionNotFound(SessionLookupError):
    pass


class SessionAmbiguous(SessionLookupError):
    def __init__(self, target: str, candidates: list[dict]):
        super().__init__(f"multiple sessions match {target!r}")
        self.target = target
        self.candidates = candidates


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def detect_source(path: Path) -> Optional[str]:
    """Identify a supported transcript by root first, then by its first records."""
    if devin_source.is_devin_path(path):
        return "devin"
    if codex_source.is_codex_path(path):
        return "codex"
    if kimi_source.is_kimi_path(path):
        return "kimi"
    if pi_source.is_pi_path(path):
        return "pi"
    if ag_source.is_antigravity_path(path):
        return "antigravity"
    if _under(path, server.PROJECTS_DIR):
        return "claude"

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for _ in range(12):
                raw = handle.readline()
                if not raw:
                    break
                try:
                    row = json.loads(raw)
                except Exception:
                    continue
                kind = row.get("type")
                if kind == "session" and row.get("id") and "cwd" in row:
                    return "pi"
                if kind == "session_meta":
                    return "codex"
                if kind == "metadata" and row.get("protocol_version"):
                    return "kimi"
                # Antigravity rows are the only ones carrying a step slot beside an
                # upper-case row type; this recognizes a transcript copied out of the
                # brain root, where the path predicate alone cannot.
                step = row.get("step_index")
                if (isinstance(step, int) and not isinstance(step, bool)
                        and row.get("source") in {"USER_EXPLICIT", "MODEL", "SYSTEM"}
                        and isinstance(kind, str) and kind.isupper()):
                    return "antigravity"
                if kind in {"user", "assistant", "system", "custom-title"}:
                    return "claude"
    except OSError:
        return None
    return None


def _claude_slug(path: Path) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for index, raw in enumerate(handle):
                if index >= 200:
                    break
                if '"slug"' not in raw:
                    continue
                try:
                    slug = json.loads(raw).get("slug")
                except Exception:
                    continue
                if isinstance(slug, str) and slug.strip():
                    return slug.strip()
    except OSError:
        return None
    return None


_relationship = session_identity.relationship

def session_metadata(path: Path) -> dict:
    source = detect_source(path)
    if source not in SUPPORTED_SOURCES:
        raise SessionLookupError(f"unsupported or unrecognized session transcript: {path}")

    if source == "devin":
        try:
            meta = devin_source.extract_metadata(path)
        except (ValueError, OSError, sqlite3.Error) as exc:
            raise SessionLookupError(str(exc)) from exc
    elif source == "codex":
        meta = codex_source.extract_metadata(path)
    elif source == "kimi":
        meta = kimi_source.extract_metadata(path)
    elif source == "pi":
        meta = pi_source.extract_metadata(path)
    elif source == "antigravity":
        meta = ag_source.extract_metadata(path)
    else:
        meta = server.extract_metadata(path)
    if not meta:
        raise SessionLookupError(f"could not parse session transcript: {path}")

    item = dict(meta)
    item["source"] = source
    item["jsonl_path"] = str(path.resolve())
    is_subagent, parent_id = _relationship(path, source)
    item["is_subagent"] = is_subagent
    item["parent_session_id"] = parent_id
    item.update(session_identity.selection_metadata(item))
    if source == "claude":
        item["id"] = claude_history.session_id(path) or item["id"]
        item["slug"] = _claude_slug(path)
    return item


def iter_session_paths(
    source: Optional[str] = None,
    include_subagents: bool = False,
) -> Iterable[Path]:
    """Yield supported transcript paths without writing dashboard state or caches."""
    if source in (None, "claude") and server.PROJECTS_DIR.exists():
        for project_dir in server.PROJECTS_DIR.iterdir():
            if not project_dir.is_dir():
                continue
            yield from project_dir.glob("*.jsonl")
            if include_subagents:
                yield from project_dir.glob("*/subagents/*.jsonl")

    if source in (None, "pi"):
        yield from pi_source.scan_sessions()

    if source in (None, "antigravity"):
        yield from ag_source.scan_sessions()

    if source in (None, "devin"):
        try:
            yield from devin_source.scan_sessions()
        except (OSError, ValueError, sqlite3.Error) as exc:
            if source == "devin":
                raise SessionLookupError("Devin database unavailable") from exc
            print("[warn] Devin database unavailable; searching other sources", file=sys.stderr)

    if source in (None, "codex"):
        for root in (codex_source.CODEX_ROOT, codex_source.CODEX_ARCHIVED_ROOT):
            if not root.exists():
                continue
            for path in root.rglob("rollout-*.jsonl"):
                meta = codex_source._read_session_meta(path)
                if not meta:
                    continue
                if not include_subagents and codex_source._is_subagent(meta, path):
                    continue
                yield path

    if source in (None, "kimi") and kimi_source.KIMI_SESSIONS_ROOT.exists():
        for wire in kimi_source.scan_sessions():
            yield wire
            if include_subagents:
                agents_dir = wire.parent.parent
                for sub in sorted(agents_dir.glob("*/wire.jsonl")):
                    if kimi_source.is_subagent_path(sub):
                        yield sub


def _message_from_row(row: dict, source: str) -> tuple[Optional[str], str]:
    if source == "kimi":
        return kimi_source.message_role_from_line(row)

    if source == "claude":
        row = server.normalize_record(row)
        if row.get("isMeta"):
            return None, ""
        kind = row.get("type")
        if kind == "user":
            # The record-level rule, not a reading of the content alone: a compaction
            # summary or a harness note is not something the person said.
            return "user", human_turn_words(row)
        if kind == "assistant":
            return "assistant", server._assistant_text((row.get("message") or {}).get("content"))
        return None, ""

    if row.get("type") != "response_item":
        return None, ""
    payload = row.get("payload") or {}
    if payload.get("type") != "message":
        return None, ""
    role = payload.get("role")
    if role not in {"user", "assistant"}:
        return None, ""
    return role, codex_source._extract_text_from_message_content(payload.get("content"))


def iter_messages(path, source):
    if source == "pi":
        yield from pi_source.iter_messages(path)
        return
    if source == "antigravity":
        # Live history only: a rewound branch must not answer a search.
        yield from ag_source.iter_messages(path)
        return
    if source == "devin":
        _, chain = devin_source.read_session(path)
        for node in chain:
            yield node["row_id"], *devin_source.message_text(node)
        return
    # Text a person typed into Claude's queue is searchable, under its own role, but only
    # when nothing in the file shows it being handed over - otherwise the delivered record
    # is the one hit and the queue entry would double it. Whether a delivery exists is a
    # fact about the whole file, so these few entries come out after the ordinary stream.
    queued, delivered = {}, {}
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line_number, raw in enumerate(handle, 1):
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if source == "claude":
                normalized = server.normalize_record(row)
                handed_over = claude_events.delivered_text(normalized)
                if handed_over is not None:
                    delivered[handed_over] = delivered.get(handed_over, 0) + 1
                entry = claude_events.enqueued_text(normalized)
                if entry is not None:
                    if claude_events.is_queued_human_text(entry):
                        queued.setdefault(entry, []).append(line_number)
                    continue
            yield line_number, *_message_from_row(row, source)
    unconfirmed = set(claude_events.confirmed_queue_entries(queued, delivered))
    for text, line_numbers in queued.items():
        for line_number in line_numbers:
            if line_number not in unconfirmed:
                yield line_number, claude_events.QUEUED_INPUT_ROLE, text.strip()


def _parse_since(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    raw = value.strip()
    try:
        if raw[-1:] in ("d", "h") and raw[:-1].isdigit():
            seconds = 86400 if raw.endswith("d") else 3600
            return datetime.now(tz=timezone.utc).timestamp() - int(raw[:-1]) * seconds
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError as exc:
        raise SessionLookupError("--since must be an ISO date/time or a value such as 7d or 6h") from exc


def _candidate_paths(source: Optional[str], include_subagents: bool) -> list[Path]:
    """Collect paths cheaply; parse and deduplicate only files that survive prefiltering."""
    return codex_history.canonical_paths(list(iter_session_paths(source=source, include_subagents=include_subagents)))


def search_sessions(
    query: str,
    *,
    role: str = "any",
    source: Optional[str] = None,
    project: Optional[str] = None,
    since: Optional[str] = None,
    include_subagents: bool = False,
    limit: int = 10,
) -> list[dict]:
    """Search real user/assistant text with AND semantics and useful filters."""
    terms = [term.lower() for term in query.split() if term]
    if not terms:
        raise SessionLookupError("search query must not be empty")
    since_ts = _parse_since(since)
    paths = _candidate_paths(source, include_subagents)

    prefiltered = server._rg_prefilter(terms, paths)
    # Codex titles usually live outside transcript files, so ripgrep cannot see them.
    # Resolve only title-matching Codex IDs here; all other paths retain the cheap transcript
    # prefilter and avoid full metadata parsing.
    codex_title_ids = set()
    if role == "any" and source in (None, "codex"):
        codex_title_ids = {
            session_id
            for session_id, title in codex_source.session_index_titles().items()
            if any(term in title.lower() for term in terms)
        }
    title_prefiltered = set()
    if codex_title_ids:
        for path in paths:
            if not codex_source.is_codex_path(path):
                continue
            session_id = (codex_source._read_session_meta(path) or {}).get("id")
            if session_id in codex_title_ids:
                title_prefiltered.add(str(path))

    # User-defined titles live in the Logbook state file rather than transcripts.
    # Resolve only matching title IDs so the common search path keeps its cheap prefilter.
    server.load_state()
    local_title_ids = {
        session_id
        for session_id, entry in server._state.items()
        if any(term in str(entry.get("title_override") or "").lower() for term in terms)
    }
    local_title_paths = set()
    for session_id in local_title_ids:
        path = server._find_jsonl(session_id)
        if path is not None:
            local_title_paths.add(str(path))

    results = []
    for path in paths:
        inherited = codex_source.is_codex_path(path) and bool((codex_source._read_session_meta(path) or {}).get('history_base'))
        if (not inherited and not devin_source.is_devin_path(path)
                and prefiltered is not None
                and str(path) not in prefiltered
                and str(path) not in title_prefiltered
                and str(path) not in local_title_paths):
            continue
        try:
            item = session_metadata(path)
        except SessionLookupError:
            continue
        if project and project.lower() not in (item.get("project_path") or "").lower():
            continue
        if since_ts is not None and activity_time(item) < since_ts:
            continue

        # Metadata can identify a target during ordinary discovery, but a role-filtered
        # historical search must be satisfied by that role's real messages only.
        metadata_terms = set()
        title_override = str(server._state.get(item.get("id"), {}).get("title_override") or "")
        if role == "any":
            metadata_blob = " ".join(
                str(item.get(key) or "")
                for key in ("id", "slug", "custom_title", "project_path", "jsonl_path")
            ) + " " + title_override
            metadata_blob = metadata_blob.lower()
            metadata_terms = {term for term in terms if term in metadata_blob}

        found = set(metadata_terms)
        snippets = []
        custom_title = str(item.get("custom_title") or "")
        display_title = title_override or custom_title
        if role == "any" and any(term in f"{custom_title} {title_override}".lower() for term in terms):
            snippets.append({"role": "title", "line": None, "text": display_title})
        try:
            messages = (codex_history.iter_messages(path) if item['source'] == 'codex' else
                        ((str(path.resolve()), line, role, text) for line, role, text in iter_messages(path, item['source'])))
            for evidence_path, line_number, message_role, text in messages:
                if not text or message_role is None:
                    continue
                if role != "any" and role != message_role:
                    continue
                text_lower = text.lower()
                line_terms = [term for term in terms if term in text_lower]
                found.update(line_terms)
                if line_terms and len(snippets) < 3:
                    first = min(text_lower.find(term) for term in line_terms)
                    start = max(0, first - 90)
                    end = min(len(text), first + 260)
                    excerpt = re.sub(r"\s+", " ", text[start:end]).strip()
                    snippets.append({
                        "role": message_role, "line": line_number, "source_path": evidence_path,
                        "anchor_kind": "N" if item["source"] == "devin" else "L",
                        "text": ("…" if start else "") + excerpt + ("…" if end < len(text) else ""),
                    })
        except (OSError, ValueError, sqlite3.Error):
            continue

        if len(found) < len(terms):
            continue
        results.append({
            "id": item.get("id"),
            # Each hit keeps its own record id and line anchors; the conversation id only
            # says which conversation that evidence belongs to.
            "conversation_id": (server.conversation_for_record(item.get("id")) or {}
                                ).get("conversation_id", item.get("id")),
            "slug": item.get("slug"),
            "source": item["source"],
            "project_path": item.get("project_path"),
            "jsonl_path": item["jsonl_path"],
            "mtime_iso": item.get("mtime_iso"),
            "activity_at": activity_time(item),
            "activity_at_iso": item.get("activity_at_iso") or item.get("mtime_iso"),
            "size": item.get("size"),
            "is_subagent": item["is_subagent"],
            "parent_session_id": item["parent_session_id"],
            "snippets": snippets,
        })

    # The same Claude session can have a tiny worktree placeholder and a real project copy.
    # Keep the largest matching copy, aligned with the dashboard's card behavior.
    winners = {}
    for entry in results:
        sid = entry.get("id") or entry["jsonl_path"]
        previous = winners.get(sid)
        if (previous is None or
                (entry["source"] == "codex" and
                 codex_source.rollout_rank(Path(entry["jsonl_path"])) >
                 codex_source.rollout_rank(Path(previous["jsonl_path"]))) or
                (entry["source"] != "codex" and
                 (entry.get("size") or 0) > (previous.get("size") or 0))):
            winners[sid] = entry
    deduped = list(winners.values())
    deduped.sort(key=activity_time, reverse=True)
    return deduped[:limit]


def conversation_facts(item: dict, target: Optional[str] = None) -> dict:
    """Report the conversation a resolved record belongs to, and how we got here.

    Identity is additive. The record id in every other field stays exactly what it was, so
    an Agent that stored a Session ID keeps reading the same transcript. When the caller
    passed a conversation id instead, ``resolved_from_conversation_id`` says so explicitly:
    nothing here retargets a supervision cursor behind the caller's back.
    """
    record_id = item.get("id")
    facts = server.conversation_for_record(record_id)
    if facts:
        facts = dict(facts)
        facts["conversation_evidence"] = "scan_cache"
    else:
        # The CLI reads the dashboard's warm scan cache rather than scanning the library
        # itself. When that cache is missing or was written by an older build, the library
        # is unknown, and the only honest answer is that this record is its own
        # conversation as far as we can see -- never a guess at a wider one.
        facts = {"conversation_id": record_id, "conversation_current_id": record_id,
                 "conversation_records": [{"id": record_id, "relation": None,
                                           "is_current": True}],
                 "conversation_evidence": "record_only",
                 "conversation_evidence_reason":
                     "scan cache absent or written by an older schema; start the dashboard "
                     "once to refresh it, then this record's conversation is reported in full"}
    if target and target != record_id and server.conversation_index().get(target) == record_id:
        facts["resolved_from_conversation_id"] = target
        facts["resolution_note"] = (f"conversation {target} -> current record {record_id}")
    return facts


def _timestamp(value) -> float:
    """Parse one message timestamp; unusable values count as never."""
    try:
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            value = parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
        return float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def recent_sessions(
    *,
    since: Optional[str] = "1d",
    by: str = "activity",
    source: Optional[str] = None,
    project: Optional[str] = None,
    include_suspected: bool = False,
    include_subagents: bool = False,
    limit: int = 50,
) -> list[dict]:
    """List recently active Sessions using the dashboard's selection hints.

    Discovery for a consumer that does not yet know which Session to read. The
    single-turn rule is the same reversible presentation hint the dashboard uses:
    it is not proof of automation, and a local human confirmation overrides it.
    """
    since_ts = _parse_since(since)
    server.load_state()
    rows = []
    for path in _candidate_paths(source, include_subagents):
        try:
            # Message times never exceed the file's modification time, so an older file
            # cannot hold newer activity. Database-backed sources have no file to stat.
            if since_ts is not None and path.stat().st_mtime < since_ts:
                continue
        except OSError:
            pass
        try:
            item = session_metadata(path)
        except SessionLookupError:
            continue
        if project and project.lower() not in str(item.get("project_path") or "").lower():
            continue
        if item["is_subagent"] and not include_subagents:
            continue

        # A record that a later rewind, resume or compaction superseded is not where new
        # work lands, so discovery offers the conversation's current record only -- the same
        # rule /api/session-choices follows. Identity stays additive: the row keeps its own
        # record id, and an unknown conversation (cold scan cache) is never assumed away.
        facts = server.conversation_for_record(item.get("id")) or {}
        if (facts.get("conversation_current_id") or item.get("id")) != item.get("id"):
            continue
        # Personal state is stored per record and read per conversation, so a title or a
        # participation confirmation written before a rewind still speaks for it. Without
        # this, the filter above would hide the record that carries the confirmation and
        # the conversation would silently fall back to "suspected".
        merged = session_identity.merge_conversation_state(
            [record["id"] for record in facts.get("conversation_records") or []]
            or [item.get("id")], item.get("id"), server._state)
        group, reason = item["selection_group"], item["selection_reason"]
        if group == "other" and merged["human_confirmed"]:
            group, reason = "primary", "human_confirmed"
        if group == "other" and not include_suspected:
            continue

        users = [m for m in item.get("recent_msgs", []) if m.get("role") == "user" and m.get("text")]
        last_user = max((_timestamp(m.get("ts")) for m in users), default=0.0)
        activity = float(activity_time(item) or 0)
        # Some adapters (Codex) keep preview text without per-message times. Rather than
        # drop those Sessions from a user-ranked list, rank them by conversation activity
        # and report the unknown user time as null.
        moment = (last_user or activity) if by == "user" else activity
        if since_ts is not None and moment < since_ts:
            continue

        opener = next((m for m in users if m.get("is_first")), users[0] if users else {})
        title = (merged["title_override"] or item.get("custom_title") or opener.get("text")
                 or Path(item.get("project_path") or "").name or "Untitled session")
        rows.append({
            "id": item.get("id"),
            # The record id above is what every other command takes. The conversation id
            # only says which conversation this record is the current member of.
            "conversation_id": facts.get("conversation_id", item.get("id")),
            "source": item["source"],
            "title": " ".join(str(title).split())[:100],
            "project_path": item.get("project_path"),
            "jsonl_path": item["jsonl_path"],
            "activity_at_iso": item.get("activity_at_iso"),
            "last_user_at_iso": (datetime.fromtimestamp(last_user, timezone.utc).isoformat()
                                 if last_user else None),
            "selection_group": group,
            "selection_reason": reason,
            "is_subagent": item["is_subagent"],
            "parent_session_id": item["parent_session_id"],
            "_moment": moment,
        })
    rows.sort(key=lambda row: row["_moment"], reverse=True)
    for row in rows:
        del row["_moment"]
    return rows[:limit]


def resolve_target(
    target: str,
    *,
    source: Optional[str] = None,
    project: Optional[str] = None,
    include_subagents: bool = False,
) -> Path:
    candidate = Path(target).expanduser()
    if devin_source.is_devin_path(candidate):
        session_metadata(candidate)
        return candidate.resolve()
    if candidate.is_file():
        session_metadata(candidate)
        return candidate.resolve()
    if ("/" in target or target.endswith(".jsonl")) and not candidate.exists():
        raise SessionNotFound(f"session path does not exist: {candidate}")

    direct = server._find_jsonl(target)
    if direct is not None:
        item = session_metadata(direct)
        if source and item["source"] != source:
            raise SessionNotFound(f"session {target!r} is not from source {source!r}")
        if project and project.lower() not in (item.get("project_path") or "").lower():
            raise SessionNotFound(f"session {target!r} is not in project {project!r}")
        return direct.resolve()

    matches = search_sessions(
        target,
        source=source,
        project=project,
        include_subagents=include_subagents,
        limit=10,
    )
    if not matches:
        raise SessionNotFound(f"no session matches {target!r}")
    if len(matches) > 1:
        raise SessionAmbiguous(target, matches)
    return Path(matches[0]["jsonl_path"])


def _line_count(path: Path) -> int:
    count = 0
    last = b""
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            count += chunk.count(b"\n")
            last = chunk[-1:]
    return count + (1 if last and last != b"\n" else 0)


def _filter_from_cursor_line(body: str, cursor_line: int) -> str:
    """Keep the cursor line once more so a partially written final record is not missed."""
    if cursor_line <= 0:
        return body
    kept = []
    current_anchor = 0
    for line in body.splitlines():
        match = ANCHOR_RE.search(line)
        if match:
            current_anchor = int(match.group(1))
        if current_anchor >= cursor_line:
            kept.append(line)
    return "\n".join(kept).lstrip("\n")


_KIMI_TERMINAL = {
    # Kimi writes `turn.ended.reason` per turn; only "completed" has been observed. Map the
    # obvious spellings onto the Codex vocabulary; anything else stays "unknown".
    "completed": "complete",
    "complete": "complete",
    "cancelled": "aborted",
    "canceled": "aborted",
    "aborted": "aborted",
    "interrupted": "aborted",
    "error": "error",
    "failed": "error",
}


def _observed_terminal(item: dict) -> str:
    reason = item.get("last_stop_reason")
    if item["source"] == "kimi":
        return _KIMI_TERMINAL.get(reason, "unknown") if isinstance(reason, str) else "unknown"
    if item["source"] != "codex":
        return "unknown"
    return reason if reason in {"complete", "aborted", "error"} else "unknown"


def _codex_context(path, after_line=0, cursor_source_path=None, historical_terminal=False,
                   records=None, issues=None, relations=None, first_turn=None, last_turns=None):
    from sources import codex_history
    if records is None and (cursor_source_path or str(after_line).startswith('cx1:')):
        source, line = cursor_source_path, after_line
        if str(line).startswith('cx1:'):
            import base64
            _, encoded, number = str(line).split(':')
            source = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)).decode()
            line = int(number)
        line = int(line)
        if line > 0:
            from sources import history_index
            plan = history_index.plan(path, codex_history.resolve)
            source = str(Path(source).resolve())
            matches = [i for i, s in enumerate(plan['segments']) if s['path'] == source]
            if not matches:
                raise ValueError('cursor source is not in the effective history')
            segment = plan['segments'][matches[0]]
            if line > segment['coordinates'][-1][0]:
                raise ValueError('cursor is in a replaced or truncated history tail; reconcile context before continuing')
            selected = history_index.window(segment, line)
            for later in plan['segments'][matches[0] + 1:]:
                selected.extend(history_index.window(later))
            return _codex_context(path, line, source, historical_terminal, records=selected,
                                  issues=plan['issues'],
                                  relations={s['path']: s.get('relation') for s in plan['segments']},
                                  first_turn=first_turn, last_turns=last_turns)
    history = codex_history.load(path) if records is None else None
    rows = history['records'] if history is not None else records
    issues = history['issues'] if history is not None else (issues or [])
    changed = bool(cursor_source_path and Path(cursor_source_path).resolve() != Path(path).resolve())
    if str(after_line).startswith('cx1:'):
        import base64
        _, encoded, number = str(after_line).split(':')
        cursor_source_path = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)).decode()
        after_line = int(number)
        changed = Path(cursor_source_path).resolve() != Path(path).resolve()
    after_line = int(after_line)
    if records is None and after_line:
        source = cursor_source_path or path
        if cursor_source_path:
            index = codex_history.cursor_position(history, source, after_line)
            rows = rows[index:]
        else:
            # A bare line number cannot identify a previous segment. Report replay.
            _, migrated = codex_source.resume_cursor(path, after_line)
            if not migrated:
                rows = [row for row in rows if row['path'] == str(Path(path).resolve()) and row['line'] >= after_line]
            changed = migrated
    body = anchored_transcript.render_codex(path, records=rows)
    body, turn_notes = anchored_transcript.slice_from_turn(body, first_turn, last_turns)
    cursor_rows = history['records'] if history is not None else records
    last = max((r['line'] for r in cursor_rows if r['path'] == str(Path(path).resolve())), default=0)
    meta = session_metadata(path)
    source_segments = {}
    for row in cursor_rows:
        source_segments.setdefault(row['path'], []).append(row)
    # Carry the relation resolve() assigned; rebuilding it from the manifest alone cannot tell
    # a page linked by history_base from an unlinked same-id page.
    if relations is None:
        relations = {s['path']: s.get('relation') for s in (history or {}).get('segments', [])}
    manifest = {'segments': [
        {'path': source, 'session_id': (codex_source._read_session_meta(Path(source)) or {}).get('id'),
         'relation': relations.get(source), 'records': entries}
        for source, entries in source_segments.items()]}
    header = [anchored_transcript.digest_header(Path(path).resolve(), 'codex',
              source_files=codex_history.references(path, manifest)),
              '# SESSION_ID: ' + str(meta.get('id')), '# PROJECT: ' + str(meta.get('project_path') or ''),
              '# FORKED_FROM: ' + json.dumps(codex_history.fork_lineage(codex_source._read_session_meta(path))),
              '# CONTEXT_COMPLETE: ' + str(not issues).lower(),
              '# CONTEXT_ISSUES: ' + json.dumps(issues),
              '# RETURNED_CONTENT: ' + ('yes' if body.strip() else 'no'),
              '# INPUT_CURSOR: L' + str(after_line), '# SOURCE_CHANGED: ' + str(changed).lower(),
              '# NEXT_CURSOR: L' + str(last), '# NEXT_LINE: L' + str(last),
              '# CURSOR_SOURCE_PATH: ' + str(Path(path).resolve()),
              '# SOURCE_CURSOR: ' + str(codex_source.next_cursor(path, last)),
              *turn_notes,
              '# REPEATED_CURSOR_LINE: ' + ('L' + str(after_line) if after_line else 'none'),
              '# HISTORICAL_TERMINAL_NOT_CURRENT_STATE: ' + _observed_terminal(meta) if historical_terminal else '# EXPLICIT_TERMINAL: ' + _observed_terminal(meta),
              '# Physical line anchors belong to the accompanying source file.',
              '# A quiet file is not proof of liveness or task completion.']
    return '\n'.join(header) + '\n\n' + (body or '[NO NEW RENDERED CONTENT]')


def _observe_codex(path, args):
    from sources import codex_history, runtime_events
    from sources import history_index
    plan = history_index.plan(path, codex_history.resolve)
    codex_history.require_complete(plan)
    sid = session_metadata(path)['id']
    segments = [s for s in plan['segments'] if s['session_id'] == sid]
    source = str(Path(args.cursor_source_path).resolve()) if args.cursor_source_path else None
    def decode(value):
        if not str(value).startswith('cx1:'):
            return None, int(value)
        import base64
        _, encoded, line = str(value).split(':')
        return str(Path(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)).decode()).resolve()), int(line)
    ns, native = decode(args.native_line_cursor)
    cs, conversation = decode(args.cursor_line)
    identities = {value for value in (source, ns, cs) if value is not None}
    if len(identities) > 1:
        raise ValueError('native and conversation cursors name different sources')
    source = next(iter(identities), None)
    if min(native, conversation) < 0 or not 1 <= args.limit <= 200:
        raise ValueError('invalid native cursor or limit')
    unqualified = source is None and bool(native or conversation)
    if source:
        matches = [i for i, s in enumerate(segments) if s['path'] == source]
        if not matches:
            raise ValueError('cursor source is not in the effective same-thread history')
        index = matches[0]
        end = segments[index]['coordinates'][-1][0]
        if native > end or conversation > end:
            raise ValueError('cursor is in a replaced or truncated history tail; reconcile context before continuing')
        if native == end and conversation == end and index + 1 < len(segments):
            index += 1
            native = conversation = 0
    else:
        index = 0
        native = conversation = 0
    segment = segments[index]
    changed = source is not None and source != segment['path']
    page_rows = history_index.window(segment, min(native + 1, max(1, conversation)))
    selected = [row for row in page_rows if row['line'] > native]
    events, last, more = [], native, False
    for row in selected:
        record = row['record']
        event = record.get('payload', {}) if record.get('type') == 'event_msg' else {}
        if event.get('type') in {'task_started', 'task_complete', 'turn_aborted', 'error'}:
            if len(events) >= args.limit:
                more = True
                break
            events.append({'line': row['line'], 'source_path': row['path'],
                'facts': {k: event[k] for k in ('type', 'turn_id', 'agentId', 'reason') if k in event},
                'timestamp': record.get('timestamp')})
        last = row['line']
    body_rows = [row for row in page_rows if row['line'] >= max(1, conversation)]
    # A page advances to this segment's end for conversation, independently of native paging.
    context_rows = body_rows
    if source is None and index == 0:
        inherited = []
        for prior in plan['segments']:
            if prior['path'] == segment['path']:
                break
            inherited.extend(history_index.window(prior))
        context_rows = inherited + context_rows
    text = _codex_context(Path(segment['path']), records=context_rows, historical_terminal=True)
    result = {'source': 'codex', 'session_id': sid, 'transcript_path': segment['path'],
        'context_complete': True, 'history_issues': [],
        'history_cache': plan.get('cache', 'disabled'),
        'hooks': runtime_events.read('codex', sid, args.event_cursor, args.limit),
        'native': {'events': events, 'next_line_cursor': last,
            'source_cursor': codex_source.next_cursor(segment['path'], last),
            'has_more': more or index + 1 < len(segments)}, 'conversation': text,
        'interpretation': 'Only verified effective history is returned. Events do not prove task completion.'}
    if changed:
        result['cursor_reset_reason'] = 'transcript_changed'
    elif unqualified:
        result['cursor_reset_reason'] = 'cursor_source_missing'
    return result


def render_context(path: Path, after_line: int = 0, historical_terminal: bool = False,
                   cursor_source_path=None, target=None, delta: bool = False,
                   first_turn=None, last_turns=None) -> str:
    item = session_metadata(path)
    if item["source"] == "codex":
        return _codex_context(path, after_line, cursor_source_path, historical_terminal,
                              first_turn=first_turn, last_turns=last_turns)
    if item["source"] == "devin":
        return devin_source.render_context(path, int(after_line),
                                           first_turn=first_turn, last_turns=last_turns)
    input_cursor = after_line
    changed = False
    after_line = int(after_line)
    if item["source"] == "claude" and cursor_source_path:
        changed = Path(cursor_source_path).resolve() != path.resolve()
        after_line = claude_history.remap_cursor(path, cursor_source_path, after_line)
    elif cursor_source_path and Path(cursor_source_path).resolve() != path.resolve():
        raise ValueError("cursor source changed without supported continuation evidence")
    claude_selection = claude_history.rewind_status(path) if item['source'] == 'claude' else {}
    full_claude_branch = (item['source'] == 'claude' and
                          claude_selection.get('reason') != 'missing_leaf_pointer')
    facts = conversation_facts(item, target)
    # Opt-in for readers that keep their own cursor: return only the cursor onward and
    # name the earlier anchors a rewind removed, instead of the whole branch to diff.
    delta_requested = bool(delta) and full_claude_branch and after_line > 0
    # A conversation id names the conversation's current record, which is not necessarily
    # the record the caller read last, and a bare line number carries no file identity.
    # Applying it to a different transcript would silently skip lines this reader never saw,
    # so return the full selected branch instead and say why. Naming the cursor's own record
    # with --cursor-source-path maps it explicitly, and delta applies again.
    cursor_unattributed = bool(delta_requested and not cursor_source_path
                               and facts.get("resolved_from_conversation_id")
                               and len(facts.get("conversation_records") or []) > 1)
    delta_mode = delta_requested and not cursor_unattributed
    removed = claude_history.hidden_lines(path, after_line) if delta_mode else None
    if full_claude_branch and not delta_mode:
        after_line = 0
    total_lines = (claude_history.summary(path)['physical_lines'] if item["source"] == "claude"
                   else _line_count(path))
    if after_line > total_lines:
        raise SessionLookupError(
            f"cursor L{after_line} is beyond current end L{total_lines}; restart from line 0"
        )
    # An Antigravity rewind is appended to the same file and retroactively abandons rows a
    # follower may already hold. The cursor still works, because rendered anchors stay the
    # file's own ascending physical lines; what the reader additionally needs is the list of
    # lines at or before its cursor that left the conversation, reported in the same
    # REMOVED_BEFORE_CURSOR vocabulary Claude's delta follow uses. Antigravity's live-history
    # rule is always decidable, so this is never `unknown`.
    ag_removed = (sorted(number for number in ag_source.abandoned_line_numbers(path)
                         if number <= after_line)
                  if item["source"] == "antigravity" and after_line > 0 else None)
    if item["source"] == "kimi":
        body = anchored_transcript.render_kimi(path)
    elif item["source"] == "pi":
        body = anchored_transcript.render_pi(path)
    elif item["source"] == "antigravity":
        body = anchored_transcript.render_antigravity(path)
    else:
        body = anchored_transcript.render_claude(path, claude_history.records(path, after_line))
    # Pi can switch branches inside one file. Return the complete selected branch
    # on follow rather than pretending an append-only cursor preserves its context.
    if item["source"] not in {"pi", "claude"}:
        body = _filter_from_cursor_line(body, after_line)
    # Turn slicing reads the [U#] this body actually printed, so it composes with whatever
    # the cursor already removed instead of second-guessing it, and RETURNED_CONTENT below
    # keeps describing what the caller receives.
    body, turn_notes = anchored_transcript.slice_from_turn(body, first_turn, last_turns)
    header = anchored_transcript.digest_header(path.resolve(), item["source"])
    records = " ".join(
        f"{record['id']}{'*' if record.get('is_current') else ''}"
        f"{'(' + record['relation'] + ')' if record.get('relation') else ''}"
        for record in facts.get("conversation_records") or [])
    observation = "\n".join([
        f"# SESSION_ID: {item.get('id')}",
        f"# CONVERSATION_ID: {facts.get('conversation_id')}",
        f"# CONVERSATION_RECORDS: {records}  (* = current; this transcript is SESSION_ID only)",
        *([f"# RESOLVED_FROM_CONVERSATION: {facts['resolution_note']}"]
          if facts.get("resolution_note") else []),
        f"# PROJECT: {item.get('project_path') or ''}",
        f"# INPUT_CURSOR: {input_cursor if str(input_cursor).startswith('cx1:') else 'L' + str(input_cursor)}",
        f"# SOURCE_CHANGED: {str(changed).lower()}",
        *(['# HISTORY_SWITCH: caller selected this target; shared UUID maps the cursor, not supervision authority.',
           '# Reconcile any prior branch-only context; related files remain available separately.']
          if changed and item['source'] == 'claude' else []),
        *(['# FOLLOW_MODE: full selected branch; reconcile removed entries after rewind'
           if claude_selection.get('evidence') else
           '# FOLLOW_MODE: full saved history; selected branch unverified, reconcile previous context']
          if full_claude_branch and not delta_mode else []),
        *(['# FOLLOW_MODE: delta from cursor; selected branch verified',
           f'# REMOVED_BEFORE_CURSOR: {anchored_transcript.line_ranges(removed)}']
          if delta_mode and removed is not None else []),
        *([f"# FOLLOW_MODE: delta from cursor; selected branch unverified ({claude_selection.get('reason')}), "
           'every saved record is kept, so removed entries cannot be determined',
           '# REMOVED_BEFORE_CURSOR: unknown']
          if delta_mode and removed is None else []),
        *(['# DELTA_NOT_APPLIED: the target was given as a conversation id, the conversation '
           f"has several records and the current one is {facts.get('conversation_current_id')}, "
           'so a bare cursor cannot be attributed to this transcript; the full selected branch '
           'is returned instead. Re-run with --cursor-source-path naming the record the cursor '
           'came from to get the delta.']
          if cursor_unattributed else []),
        *(['# FOLLOW_MODE: full selected branch (Pi can change branches)'] if item["source"] == "pi" else []),
        *(['# FOLLOW_MODE: live history after in-file rewinds; abandoned rows are not rendered']
          if item["source"] == "antigravity" else []),
        *([f"# REMOVED_BEFORE_CURSOR: {anchored_transcript.line_ranges(ag_removed)}"]
          if ag_removed is not None else []),
        *turn_notes,
        f"# REPEATED_CURSOR_LINE: {'L' + str(after_line) if after_line and item['source'] != 'pi' else 'none'}",
        f"# NEXT_CURSOR: L{total_lines}",
        f"# CURSOR_SOURCE_PATH: {path.resolve()}",
        f"# NEXT_LINE: L{total_lines}",
        f"# RETURNED_CONTENT: {'yes' if body.strip() else 'no'}",
        (f"# HISTORICAL_TERMINAL_NOT_CURRENT_STATE: {_observed_terminal(item)}"
         if historical_terminal else f"# EXPLICIT_TERMINAL: {_observed_terminal(item)}"),
        ("# Compare the full selected branch with the previous snapshot, including removed entries."
         if item["source"] == "pi" or full_claude_branch else
         "# The cursor line is returned again on follow; ignore it when its [L#] was already seen."),
        *(['# Retire anything derived from REMOVED_BEFORE_CURSOR anchors: a rewind appended after '
           'your last read can abandon lines you already received.']
          if ag_removed else []),
        "# A quiet file is not proof that its Agent is still running or has finished.",
    ])
    return header + "\n" + observation + "\n\n" + (body or "[NO NEW RENDERED CONTENT]")


def read_evidence(path: Path, line: int, context: int = 1, max_chars: int = 12_000) -> str:
    if devin_source.is_devin_path(path):
        try:
            return devin_source.read_evidence(path, line, context, max_chars)
        except (ValueError, OSError, sqlite3.Error) as exc:
            raise SessionLookupError(str(exc)) from exc
    if line < 1:
        raise SessionLookupError("line must be at least 1")
    start = max(1, line - max(0, context))
    end = line + max(0, context)
    selected = []
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if line_number < start:
                continue
            if line_number > end:
                break
            text = raw.rstrip("\n")
            if max_chars and len(text) > max_chars:
                text = text[:max_chars] + f" …[{len(text) - max_chars} chars omitted]"
            selected.append(f"[L{line_number}] {text}")
    if not selected:
        raise SessionLookupError(f"line L{line} does not exist in {path}")
    return "\n".join(selected)


def _antigravity_rewind_facts(path: Path) -> dict:
    """What an in-file rewind removed from this transcript, named by physical line.

    `total_lines` beside it stays the raw file length, so the two together say how much of
    the file the reading covers, and the abandoned lines remain readable with `evidence`.
    """
    _live, report, _total = ag_source.live_history(path)
    return {
        'history_semantics': 'live_history_after_in_file_rewind',
        'rewind_abandoned_rows': report['abandoned_rows'],
        'rewind_abandoned_lines': anchored_transcript.line_ranges(report['abandoned_lines']),
        'rewinds': report['rewinds'],
    }


def status_for(path: Path) -> dict:
    from sources import runtime_events
    item = session_metadata(path)
    facts = conversation_facts(item)
    if item["source"] == "devin":
        _, chain = devin_source.read_session(path)
        return {**item, **facts, "total_nodes": len(chain), "anchor_kind": "N",
                "next_cursor": f"N{chain[-1]['row_id'] if chain else 0}",
                "follow_mode": "full-snapshot", "liveness": "unknown",
                "explicit_terminal": "unknown"}
    total_lines = _line_count(path)
    return {
        "id": item.get("id"),
        "slug": item.get("slug"),
        "source": item["source"],
        "project_path": item.get("project_path"),
        "jsonl_path": item["jsonl_path"],
        **({'source_files': codex_history.references(path),
            'forked_from': codex_history.fork_lineage(codex_source._read_session_meta(path)),
            'spawned_from': codex_history.spawn_lineage(codex_source._read_session_meta(path))}
           if item['source'] == 'codex' else
           claude_history.describe(path) if item['source'] == 'claude' else
           _antigravity_rewind_facts(path) if item['source'] == 'antigravity' else {}),
        "mtime_iso": item.get("mtime_iso"),
        "size": item.get("size"),
        "total_lines": total_lines,
        "next_cursor": f"L{total_lines}",
        "last_recorded_stop_reason": item.get("last_stop_reason"),
        "explicit_terminal": _observed_terminal(item),
        "is_subagent": item["is_subagent"],
        "parent_session_id": item["parent_session_id"],
        "liveness": "unknown",
        **facts,
        "runtime_observations": runtime_events.read(item["source"], item.get("id")),
        # Runtime events are written by the clients themselves, under ids Logbook does not
        # mint. They are unioned at read time and never re-keyed, so each superseded record
        # still reports its own observations under its own id.
        **({"conversation_runtime_observations":
            {record["id"]: runtime_events.read(item["source"], record["id"])
             for record in facts["conversation_records"] if record["id"] != item.get("id")}}
           if len(facts.get("conversation_records") or []) > 1 else {}),
    }


def _add_target_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("target", help="session ID, source reference, or search query")
    parser.add_argument("--source", choices=SUPPORTED_SOURCES)
    parser.add_argument("--project", help="project-path substring")
    parser.add_argument("--include-subagents", action="store_true")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Read, hand off, follow, audit, and search local Agent sessions."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    locate = sub.add_parser("locate", help="resolve a target to one transcript")
    _add_target_filters(locate)

    context = sub.add_parser("context", help="render an anchored context snapshot")
    _add_target_filters(context)
    context.add_argument("--after-line", default=0)
    context.add_argument("--cursor-source-path")
    turns = context.add_mutually_exclusive_group()
    turns.add_argument("--from-turn", metavar="U13",
                       help="start at this human turn, spelled as the transcript prints it (U13 or 13)")
    turns.add_argument("--last-turns", type=int, metavar="N",
                       help="keep only the last N human turns and everything after them")

    follow = sub.add_parser("follow", help="render from a previous cursor, repeating its line once")
    _add_target_filters(follow)
    follow.add_argument(
        "--cursor-line", "--after-line", dest="after_line", required=True,
        help="numeric NEXT_CURSOR line or Codex SOURCE_CURSOR token; Devin returns a full snapshot",
    )

    follow.add_argument("--cursor-source-path")
    follow.add_argument(
        "--delta", action="store_true",
        help="Claude only: return the cursor onward plus the earlier anchors a rewind removed, "
             "instead of the full selected branch",
    )

    status = sub.add_parser("status", help="report observed transcript state")
    _add_target_filters(status)

    observe = sub.add_parser("observe", help="read runtime facts and incremental conversation")
    _add_target_filters(observe)
    observe.add_argument("--event-cursor", type=int, default=0)
    observe.add_argument("--native-line-cursor", default=0)
    observe.add_argument("--cursor-line", default=0)
    observe.add_argument("--limit", type=int, default=50)
    observe.add_argument("--cursor-source-path")

    evidence = sub.add_parser("evidence", help="read source lines or Devin database nodes around an anchor")
    _add_target_filters(evidence)
    evidence.add_argument("--line", type=int, required=True)
    evidence.add_argument("--context", type=int, default=1)
    evidence.add_argument("--max-chars", type=int, default=12_000)

    recent = sub.add_parser("recent", help="list recently active sessions")
    recent.add_argument("--since", default="1d", help="ISO date/time, or relative such as 6h or 7d")
    recent.add_argument("--by", choices=("activity", "user"), default="activity",
                        help="rank and filter by the latest message, or by the latest user message")
    recent.add_argument("--source", choices=SUPPORTED_SOURCES)
    recent.add_argument("--project", help="project-path substring")
    recent.add_argument("--include-suspected", action="store_true",
                        help="also list single-turn sessions the dashboard hides by default")
    recent.add_argument("--include-subagents", action="store_true")
    recent.add_argument("--limit", type=int, default=50)

    search = sub.add_parser("search", help="search session message text")
    search.add_argument("query")
    search.add_argument("--role", choices=("any", "user", "assistant"), default="any")
    search.add_argument("--source", choices=SUPPORTED_SOURCES)
    search.add_argument("--project", help="project-path substring")
    search.add_argument("--since", help="ISO date/time or relative days such as 7d")
    search.add_argument("--include-subagents", action="store_true")
    search.add_argument("--limit", type=int, default=10)
    return parser.parse_args(argv)


def _resolve_from_args(args) -> Path:
    return resolve_target(
        args.target,
        source=args.source,
        project=args.project,
        include_subagents=args.include_subagents,
    )


def _print_json(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "search":
            _print_json(search_sessions(
                args.query,
                role=args.role,
                source=args.source,
                project=args.project,
                since=args.since,
                include_subagents=args.include_subagents,
                limit=max(1, args.limit),
            ))
            return 0

        if args.command == "recent":
            _print_json(recent_sessions(
                since=args.since,
                by=args.by,
                source=args.source,
                project=args.project,
                include_suspected=args.include_suspected,
                include_subagents=args.include_subagents,
                limit=max(1, args.limit),
            ))
            return 0

        path = _resolve_from_args(args)
        target = getattr(args, "target", None)
        if args.command == "locate":
            metadata = session_metadata(path)
            metadata.update(conversation_facts(metadata, target))
            if metadata['source'] == 'codex':
                history = codex_history.load(path)
                metadata.update(source_files=codex_history.references(path, history),
                                forked_from=history.get('forked_from'),
                                spawned_from=history.get('spawned_from'),
                                context_complete=history['complete'], history_issues=history['issues'])
            if metadata['source'] == 'claude':
                metadata.update(claude_history.describe(path))
            _print_json(metadata)
        elif args.command in {"context", "follow"}:
            # `follow` shares this branch but takes no turn options: its cursor is a
            # continuation point, and a turn number is not one (a rewind renumbers).
            from_turn = getattr(args, "from_turn", None)
            print(render_context(path,
                                 first_turn=(anchored_transcript.parse_turn_ref(from_turn)
                                             if from_turn else None),
                                 last_turns=getattr(args, "last_turns", None),
                                 after_line=args.after_line,
                                 cursor_source_path=args.cursor_source_path,
                                 target=target,
                                 delta=getattr(args, "delta", False)))
        elif args.command == "status":
            _print_json(status_for(path))
        elif args.command == "observe":
            from sources import runtime_events
            item = session_metadata(path)
            if item["source"] not in runtime_events.SOURCES:
                raise ValueError("runtime observation is not supported for this source")
            if item["source"] == "codex":
                _print_json(_observe_codex(path, args))
                return 0
            native_cursor, conversation_cursor = int(args.native_line_cursor), int(args.cursor_line)
            source_changed = bool(args.cursor_source_path and
                                  Path(args.cursor_source_path).resolve() != path.resolve())
            session_changed = False
            if source_changed:
                if item['source'] != 'claude':
                    raise ValueError("cursor source changed without supported continuation evidence")
                conversation_cursor = claude_history.remap_cursor(path, args.cursor_source_path, conversation_cursor)
                session_changed = (claude_history.session_id(path) !=
                                   claude_history.session_id(args.cursor_source_path))
                # Native and Hook consumers have independent cursor namespaces.
                # Claude has no native event stream; do not transplant its old line.
                native_cursor = 0

            native = runtime_events.native_events(path, item["source"], native_cursor, args.limit)
            result = {
                "source": item["source"], "session_id": item.get("id"),
                "transcript_path": str(path.resolve()),
                "hooks": runtime_events.read(item["source"], item.get("id"),
                                             0 if session_changed else args.event_cursor, args.limit),
                "native": native,
                "conversation": render_context(path, after_line=conversation_cursor,
                                               historical_terminal=True),
                "interpretation": "Events are observations, not proof of task success. "
                                  "Use turn identities and conversation evidence. Missing events "
                                  "do not establish liveness or completion.",
            }
            if item['source'] == 'claude':
                result.update(claude_history.describe(path))
                if result.get('rewind', {}).get('reason') != 'missing_leaf_pointer':
                    result['conversation_follow_mode'] = (
                        'full_selected_branch' if result['rewind'].get('evidence')
                        else 'full_saved_history_unverified')
                    result['conversation_reconciliation_required'] = True
                result['source_changed'] = source_changed
                result['session_changed'] = session_changed
                result['conversation_cursor_mapped'] = conversation_cursor
                if session_changed:
                    result['cursor_reset_reason'] = 'explicit_session_change'
                    result['cursor_reset_scope'] = ['hooks', 'native', 'turn_identity']
                    result['previous_session_id'] = claude_history.session_id(args.cursor_source_path)
            _print_json(result)
        elif args.command == "evidence":
            facts = conversation_facts(session_metadata(path), target)
            if facts.get("resolution_note"):
                print(f"# RESOLVED_FROM_CONVERSATION: {facts['resolution_note']}")
            print(read_evidence(
                path,
                line=args.line,
                context=max(0, args.context),
                max_chars=max(0, args.max_chars),
            ))
        return 0
    except SessionAmbiguous as exc:
        print(json.dumps({
            "error": "ambiguous",
            "target": exc.target,
            "candidates": exc.candidates,
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except (SessionLookupError, ValueError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
