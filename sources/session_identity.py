"""Shared observed relationships and selection hints; never authorization policy.

This module owns one more thing since 2026-09-20: **conversation identity**. A record is
one transcript file (or one database row) and keeps its own id, path and line anchors
forever. A conversation is the ordered set of records a person would call "the same
conversation" -- a Claude Desktop rewind or resume mints a new record id but does not
start a new conversation. Conversation identity is additive: it never renames, retargets
or hides a record id, and when the evidence is contradictory it fails open, leaving every
record as its own single-record conversation.
"""
from pathlib import Path
from sources.activity import activity_time
from typing import Optional
from sources import codex as codex_source
from sources import kimi as kimi_source

# The complete vocabulary of `conversation_records[].relation`, which is served on the wire.
# The root record's relation is null; every other record carries exactly one of these.
REWIND = 'rewind'                                # an explicit Desktop rewind edge said so
CONTINUATION = 'continuation'                    # a resume: a new record id, no rewind edge
COMPACTION_CONTINUATION = 'compaction-continuation'  # a compaction started a new file

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


def _single_record_fields(record_id):
    return {
        'conversation_id': record_id,
        'conversation_current_id': record_id,
        'conversation_records': [{'id': record_id, 'relation': None, 'is_current': True}],
    }


def conversation_identity(items, memberships=(), compaction_links=()):
    """Stamp conversation_id / conversation_current_id / conversation_records on copies.

    ``memberships`` is what ``claude_desktop.descriptor_memberships()`` returned;
    ``compaction_links`` are ``(child_record_id, parent_record_id)`` pairs the caller has
    already resolved from a ``compact_boundary`` whose ``logicalParentUuid`` lives in a
    different file. Everything else -- Codex, Kimi, Antigravity, Devin, Pi, and any Claude
    record no descriptor covers -- is its own single-record conversation.

    Every record keeps its own id. ``conversation_records`` is ordered oldest to newest and
    names how each record follows its predecessor, so a reader can always see which physical
    file an anchor belongs to. Contradictory evidence dissolves the whole group back into
    single records rather than guessing which half to believe.
    """
    from sources import claude_desktop

    result = [dict(item) for item in items]
    lineage_keys = ('conversation_id', 'conversation_current_id', 'conversation_records',
                    'forked_from_record_id', 'forked_from_conversation_id',
                    'spawned_from_conversation_id')
    for item in result:
        for key in lineage_keys:
            item.pop(key, None)

    cards, duplicated = {}, set()
    for item in result:
        record_id = item.get('id')
        if record_id is None:
            continue
        if record_id in cards:
            duplicated.add(record_id)
        cards[record_id] = item

    # A fork copies its source's records without re-stamping them, so the file head still
    # names the source session. That is lineage, never membership: the two stay separate.
    for item in result:
        head = item.get('head_session_id')
        if item.get('source', 'claude') == 'claude' and head and head != item.get('id'):
            item['forked_from_record_id'] = head

    predecessor, successors, desktop_label, provenance = {}, {}, {}, {}

    def link(child, parent, relation):
        if child in predecessor or successors.get(parent) or child == parent:
            return False
        predecessor[child] = (parent, relation)
        successors.setdefault(parent, set()).add(child)
        return True

    def usable(record_id, project):
        item = cards.get(record_id)
        path = item.get('jsonl_path') if item else None
        return (item is not None and record_id not in duplicated
                and item.get('source', 'claude') == 'claude'
                and isinstance(path, str) and Path(path).stem == record_id
                and Path(path).is_file()
                and claude_desktop._in_project(item.get('project_path'), project))

    for membership in memberships:
        members, project = membership['members'], membership['project']
        present, broken = [], False
        for record_id in members:
            if record_id not in cards:
                continue  # A record this machine no longer holds cannot be mis-grouped.
            if not usable(record_id, project):
                broken = True
                break
            present.append(record_id)
        if broken or membership['current'] not in present:
            continue
        forked = {cards[r].get('forked_from_record_id') for r in present}
        if forked & set(present):
            continue  # A fork and its source must never land in one conversation.
        conversation_id = membership['conversation_id']
        chained = True
        for index, record_id in enumerate(present[1:], start=1):
            if not link(record_id, present[index - 1],
                        membership['relations'].get(record_id, CONTINUATION)):
                chained = False
                break
        if not chained:
            continue
        for record_id in present:
            desktop_label[record_id] = conversation_id
        provenance[conversation_id] = {
            key: membership[key] for key in
            ('forked_from_conversation_id', 'spawned_from_conversation_id')
            if membership.get(key)
        }

    # Decide which compaction links are usable before following any of them, so the answer
    # does not depend on which one happened to be read first. A record that two files claim
    # to continue, or a file claiming to continue two records, is a branch, not a chain.
    links = list(dict.fromkeys(compaction_links))
    children = {child: sum(1 for c, _ in links if c == child) for child, _ in links}
    ancestors = {parent: sum(1 for _, p in links if p == parent) for _, parent in links}
    for child, parent in links:
        if children[child] > 1 or ancestors[parent] > 1:
            continue
        if child not in cards or parent not in cards:
            continue
        if child in duplicated or parent in duplicated:
            continue
        child_label, parent_label = desktop_label.get(child), desktop_label.get(parent)
        if child_label and parent_label and child_label != parent_label:
            continue  # Two Desktop conversations; no compaction record can outrank them.
        if cards[child].get('forked_from_record_id') == parent:
            continue
        link(child, parent, COMPACTION_CONTINUATION)

    components = {}
    seen = set()
    for record_id in cards:
        if record_id in seen:
            continue
        group, queue = set(), [record_id]
        while queue:
            node = queue.pop()
            if node in group:
                continue
            group.add(node)
            parent = predecessor.get(node)
            if parent:
                queue.append(parent[0])
            queue.extend(successors.get(node, ()))
        seen |= group
        for node in group:
            components[node] = group

    resolved = set()
    for record_id, group in components.items():
        if record_id in resolved:
            continue
        resolved |= group
        labels = {desktop_label[node] for node in group if node in desktop_label}
        roots = [node for node in group if node not in predecessor]
        chain, node = [], roots[0] if len(roots) == 1 else None
        while node is not None and node not in chain:
            chain.append(node)
            children = successors.get(node, ())
            node = next(iter(children)) if len(children) == 1 else None
        if len(labels) > 1 or len(roots) != 1 or len(chain) != len(group):
            # A branch, a cycle, or two owners: keep the records apart and say nothing.
            for node in group:
                cards[node].update(_single_record_fields(node))
            continue
        conversation_id = labels.pop() if labels else chain[0]
        records = [{'id': chain[0], 'relation': None, 'is_current': len(chain) == 1}]
        records += [{'id': node, 'relation': predecessor[node][1],
                     'is_current': node == chain[-1]} for node in chain[1:]]
        for node in chain:
            cards[node].update(conversation_id=conversation_id,
                               conversation_current_id=chain[-1],
                               conversation_records=[dict(r) for r in records],
                               **provenance.get(conversation_id, {}))
    return result


def merge_conversation_state(record_ids, current_id, state):
    """Fold per-record personal state into the conversation's single view.

    ``state.json`` stays keyed by record forever, so this is the only place the per-record
    entries become one answer. The policy (confirmed by the repository owner on 2026-09-20):

    * ``starred`` -- true when **any** record is starred, ``starred_at`` the earliest of
      those. A star set before a rewind must not disappear when the rewind mints a new id.
    * ``archived`` -- **only the current record's** state counts. A union would hide a live
      conversation behind an ancestor the user archived long ago.
    * ``note`` -- the current record's note is the conversation's note. Notes written on
      older records are returned separately as ``older_notes`` so nothing is concatenated
      into storage and nothing is dropped.
    * ``title_override`` -- the current record's, else the newest older record that has one;
      ``title_override_source_id`` says which record it came from.
    * ``human_confirmed`` -- true when any record is confirmed; it only ever reveals.

    ``brief`` is deliberately absent: a cached brief is validated against one file's size and
    mtime, so serving one record's brief for another's transcript would be a lie. The reader
    keeps using the current record's own cache entry.

    ``record_ids`` is ordered oldest to newest. Returns plain data; writes are the caller's
    business and always land on ``current_id`` (except un-star, which has to clear every
    starred member or the star cannot be removed).
    """
    entries = [(record_id, state.get(record_id) or {}) for record_id in record_ids]
    current = state.get(current_id) or {}
    older = [(record_id, entry) for record_id, entry in entries if record_id != current_id]

    starred_at = sorted(str(entry.get('starred_at')) for _, entry in entries
                        if entry.get('starred') and entry.get('starred_at'))
    starred_ids = [record_id for record_id, entry in entries if entry.get('starred')]

    title, title_source = str(current.get('title_override') or ''), None
    if title:
        title_source = current_id
    else:
        for record_id, entry in reversed(older):
            candidate = str(entry.get('title_override') or '')
            if candidate:
                title, title_source = candidate, record_id
                break

    return {
        'starred': bool(starred_ids),
        'starred_at': starred_at[0] if starred_at else None,
        'starred_record_ids': starred_ids,
        'archived_entry': current,
        'archived_at': current.get('archived_at'),
        'note': current.get('note', ''),
        'older_notes': [{'record_id': record_id, 'note': entry.get('note', '')}
                        for record_id, entry in older if str(entry.get('note') or '').strip()],
        'title_override': title,
        'title_override_source_id': title_source,
        'human_confirmed': any(entry.get('human_confirmed') for _, entry in entries),
    }


def session_choices(items, source=None, limit=100):
    """Return recent primary and single-turn candidates, independently capped.

    A single user turn is a reversible presentation hint, not proof of automation.
    Source history, search, and authorization are untouched.
    """
    groups = {"primary": [], "other": []}
    for original in sorted(items, key=activity_time, reverse=True):
        if original.get("archived") or (source and original.get("source", "claude") != source):
            continue
        # A record that a later rewind, resume or compaction superseded is no longer where
        # new work lands. Offering it as a peer candidate invites the caller to attach to a
        # transcript that has stopped growing.
        current = original.get("conversation_current_id")
        if current and current != original.get("id"):
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
