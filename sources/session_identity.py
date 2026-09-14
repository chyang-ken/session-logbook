"""Shared observed relationships and selection hints; never authorization policy."""
from pathlib import Path
from typing import Optional
from sources import codex as codex_source
from sources import kimi as kimi_source

def relationship(path: Path, source: str) -> tuple[bool, Optional[str]]:
    if source == "devin":
        return False, None
    if source == "claude":
        if path.parent.name == "subagents":
            return True, path.parent.parent.name
        return False, None

    if source == "kimi":
        # <session>/agents/<agent>/wire.jsonl: any agent other than "main" is a sub-agent of that session
        if kimi_source.is_subagent_path(path):
            return True, kimi_source.session_id_for_path(path)
        return False, None

    if source != "codex":
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



def selection_metadata(item):
    """Keep single-turn inference separate from observed child relationships."""
    source = item.get("source", "claude")
    child, parent = relationship(Path(item.get("jsonl_path") or ""), source)
    single = item.get("single_turn")
    if single is None and source == "claude" and item.get("user_turn_count") == 1:
        single = True
    return {
        "is_subagent": child,
        "parent_session_id": parent,
        "selection_group": "subagent" if child else "other" if single else "primary",
        "selection_reason": "child_relationship" if child else "single_user_turn" if single else "no_single_turn_evidence",
        "interaction_kind": "unknown",
    }


def session_choices(items, source=None, limit=100):
    """Return recent primary and single-turn candidates, independently capped.

    A single user turn is a reversible presentation hint, not proof of automation.
    Source history, search, and authorization are untouched.
    """
    groups = {"primary": [], "other": []}
    for original in sorted(items, key=lambda x: x.get("mtime") or 0, reverse=True):
        if original.get("archived") or (source and original.get("source", "claude") != source):
            continue
        meta = selection_metadata(original)
        if meta["is_subagent"]:
            continue
        group = groups[meta["selection_group"]]
        if len(group) >= limit:
            continue
        messages = [m for m in original.get("recent_msgs", []) if m.get("role") == "user" and m.get("text")]
        last = messages[-1] if messages else {}
        opener = next((m for m in messages if m.get("is_first")), messages[0] if messages else {})
        title = original.get("custom_title") or opener.get("text") or Path(original.get("project_path") or "").name or "Untitled session"
        group.append({
            "id": original["id"], "source": original.get("source", "claude"),
            "title": " ".join(title.split())[:100],
            "project": original.get("project_path") or "",
            "modified": original.get("mtime") or 0,
            "preview": " ".join(last.get("text", "").split())[:240],
            "last_user_at": last.get("ts"), **meta,
        })
    return groups["primary"] + groups["other"]
