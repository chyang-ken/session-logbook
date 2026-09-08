#!/usr/bin/env python3
"""Read-only command-line access to local Claude Code, Codex, and Devin Local sessions.

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
from sources import anchored_transcript
from sources import codex as codex_source
from sources import devin as devin_source


SUPPORTED_SOURCES = ("claude", "codex", "devin")
ANCHOR_RE = re.compile(r"\[L(\d+)\]")


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
                if kind == "session_meta":
                    return "codex"
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


def _relationship(path: Path, source: str) -> tuple[bool, Optional[str]]:
    if source == "devin":
        return False, None
    if source == "claude":
        if path.parent.name == "subagents":
            return True, path.parent.parent.name
        return False, None

    meta = codex_source._read_session_meta(path) or {}
    parent = meta.get("parent_thread_id")
    source_info = meta.get("source")
    is_subagent = codex_source._is_subagent(meta, path)
    if isinstance(source_info, dict):
        subagent = source_info.get("subagent")
        if isinstance(subagent, dict):
            spawn = subagent.get("thread_spawn")
            if isinstance(spawn, dict):
                parent = parent or spawn.get("parent_thread_id")
    return is_subagent, parent


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
    if source == "claude":
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


def _message_from_row(row: dict, source: str) -> tuple[Optional[str], str]:
    if source == "claude":
        if row.get("isMeta"):
            return None, ""
        kind = row.get("type")
        if kind == "user":
            return "user", server._user_text((row.get("message") or {}).get("content"))
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
    if source == "devin":
        _, chain = devin_source.read_session(path)
        for node in chain:
            yield node["row_id"], *devin_source.message_text(node)
        return
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line_number, raw in enumerate(handle, 1):
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            yield line_number, *_message_from_row(row, source)


def _parse_since(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    raw = value.strip()
    try:
        if raw.endswith("d") and raw[:-1].isdigit():
            return datetime.now(tz=timezone.utc).timestamp() - int(raw[:-1]) * 86400
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError as exc:
        raise SessionLookupError("--since must be an ISO date/time or a value such as 7d") from exc


def _candidate_paths(source: Optional[str], include_subagents: bool) -> list[Path]:
    """Collect paths cheaply; parse and deduplicate only files that survive prefiltering."""
    return list(iter_session_paths(source=source, include_subagents=include_subagents))


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
    results = []
    for path in paths:
        if not devin_source.is_devin_path(path) and prefiltered is not None and str(path) not in prefiltered:
            continue
        try:
            item = session_metadata(path)
        except SessionLookupError:
            continue
        if project and project.lower() not in (item.get("project_path") or "").lower():
            continue
        if since_ts is not None and item.get("mtime", 0) < since_ts:
            continue

        # Metadata can identify a target during ordinary discovery, but a role-filtered
        # historical search must be satisfied by that role's real messages only.
        metadata_terms = set()
        if role == "any":
            metadata_blob = " ".join(
                str(item.get(key) or "")
                for key in ("id", "slug", "custom_title", "project_path", "jsonl_path")
            ).lower()
            metadata_terms = {term for term in terms if term in metadata_blob}

        found = set(metadata_terms)
        snippets = []
        try:
            for line_number, message_role, text in iter_messages(path, item["source"]):
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
                        "role": message_role, "line": line_number,
                        "anchor_kind": "N" if item["source"] == "devin" else "L",
                        "text": ("…" if start else "") + excerpt + ("…" if end < len(text) else ""),
                    })
        except (OSError, ValueError, sqlite3.Error):
            continue

        if len(found) < len(terms):
            continue
        results.append({
            "id": item.get("id"),
            "slug": item.get("slug"),
            "source": item["source"],
            "project_path": item.get("project_path"),
            "jsonl_path": item["jsonl_path"],
            "mtime_iso": item.get("mtime_iso"),
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
        if previous is None or (entry.get("size") or 0) > (previous.get("size") or 0):
            winners[sid] = entry
    deduped = list(winners.values())
    deduped.sort(key=lambda entry: entry.get("mtime_iso") or "", reverse=True)
    return deduped[:limit]


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


def _observed_terminal(item: dict) -> str:
    if item["source"] != "codex":
        return "unknown"
    reason = item.get("last_stop_reason")
    return reason if reason in {"complete", "aborted", "error"} else "unknown"


def render_context(path: Path, after_line: int = 0) -> str:
    item = session_metadata(path)
    if item["source"] == "devin":
        return devin_source.render_context(path, after_line)
    total_lines = _line_count(path)
    if after_line > total_lines:
        raise SessionLookupError(
            f"cursor L{after_line} is beyond current end L{total_lines}; restart from line 0"
        )
    if item["source"] == "codex":
        body = anchored_transcript.render_codex(path)
    else:
        body = anchored_transcript.render_claude(path)
    body = _filter_from_cursor_line(body, after_line)
    header = anchored_transcript.digest_header(path.resolve(), item["source"])
    observation = "\n".join([
        f"# SESSION_ID: {item.get('id')}",
        f"# PROJECT: {item.get('project_path') or ''}",
        f"# INPUT_CURSOR: L{after_line}",
        f"# REPEATED_CURSOR_LINE: {'L' + str(after_line) if after_line else 'none'}",
        f"# NEXT_CURSOR: L{total_lines}",
        f"# RETURNED_CONTENT: {'yes' if body.strip() else 'no'}",
        f"# EXPLICIT_TERMINAL: {_observed_terminal(item)}",
        "# The cursor line is returned again on follow; ignore it when its [L#] was already seen.",
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


def status_for(path: Path) -> dict:
    item = session_metadata(path)
    if item["source"] == "devin":
        _, chain = devin_source.read_session(path)
        return {**item, "total_nodes": len(chain), "anchor_kind": "N",
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
        "mtime_iso": item.get("mtime_iso"),
        "size": item.get("size"),
        "total_lines": total_lines,
        "next_cursor": f"L{total_lines}",
        "last_recorded_stop_reason": item.get("last_stop_reason"),
        "explicit_terminal": _observed_terminal(item),
        "is_subagent": item["is_subagent"],
        "parent_session_id": item["parent_session_id"],
        "liveness": "unknown",
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
    context.add_argument("--after-line", type=int, default=0)

    follow = sub.add_parser("follow", help="render from a previous cursor, repeating its line once")
    _add_target_filters(follow)
    follow.add_argument(
        "--cursor-line", "--after-line", dest="after_line", type=int, required=True,
        help="numeric part of NEXT_CURSOR; Devin returns a full snapshot",
    )

    status = sub.add_parser("status", help="report observed transcript state")
    _add_target_filters(status)

    evidence = sub.add_parser("evidence", help="read source lines or Devin database nodes around an anchor")
    _add_target_filters(evidence)
    evidence.add_argument("--line", type=int, required=True)
    evidence.add_argument("--context", type=int, default=1)
    evidence.add_argument("--max-chars", type=int, default=12_000)

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

        path = _resolve_from_args(args)
        if args.command == "locate":
            _print_json(session_metadata(path))
        elif args.command in {"context", "follow"}:
            print(render_context(path, after_line=max(0, args.after_line)))
        elif args.command == "status":
            _print_json(status_for(path))
        elif args.command == "evidence":
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
