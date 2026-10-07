"""Conservative, read-only relationships from Claude Desktop rewind descriptors.

Copied transcript content and priorCliSessionIds are deliberately insufficient.
Only explicit rewind edges ending at the descriptor's current CLI session count.
"""
import json
from pathlib import Path

DESKTOP_ROOT = Path.home() / 'Library/Application Support/Claude/claude-code-sessions'


def _descriptors(root):
    result = []
    try:
        paths = sorted(Path(root).glob('*/*/local_*.json'))
    except OSError:
        return result
    for path in paths:
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(value, dict):
            result.append(value)
    return result


def _project(value):
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        return None
    return str(Path(value).resolve())


def _identity(value):
    return isinstance(value, str) and bool(value)


def _in_project(value, project):
    """A display directory can move below the Desktop's stable working root."""
    value = _project(value)
    if value is None:
        return False
    try:
        Path(value).relative_to(project)
        return True
    except ValueError:
        return False


def annotate_sessions(items, descriptor_root=None):
    """Return copied cards with verified rewind metadata; never remove cards.

    Current cards receive ``rewind_history`` (newest ancestor first). Older
    cards receive ``rewind_current_session_id``. Every annotated card receives
    ``desktop_session_id``. Consumers decide whether to collapse older cards.
    Ambiguous, unavailable, or inconsistent evidence leaves cards unchanged.
    """
    result = [dict(item) for item in items]
    # Repeated enrichment cannot retain a relationship after its evidence changes.
    for item in result:
        for key in ('rewind_history', 'rewind_current_session_id', 'desktop_session_id'):
            item.pop(key, None)
    by_id = {}
    for item in result:
        if item.get('source', 'claude') == 'claude':
            by_id.setdefault(item.get('id'), []).append(item)
    descriptors = _descriptors(DESKTOP_ROOT if descriptor_root is None else descriptor_root)
    by_desktop = {}
    active = {}
    for descriptor in descriptors:
        desktop = descriptor.get('sessionId')
        current = descriptor.get('cliSessionId')
        if not _identity(desktop) or not _identity(current):
            continue
        by_desktop.setdefault(desktop, []).append(descriptor)
        active.setdefault(current, set()).add(desktop)
    proposals = []
    for desktop, copies in by_desktop.items():
        descriptor = copies[0]
        keys = ('cliSessionId', 'cwd', 'rewindEdges')
        if any(any(copy.get(key) != descriptor.get(key) for key in keys) for copy in copies[1:]):
            continue
        current = descriptor['cliSessionId']
        project = _project(descriptor.get('cwd'))
        edges = descriptor.get('rewindEdges')
        if not project or not isinstance(edges, list) or not edges:
            continue
        parents = {}
        valid = True
        for edge in edges:
            if not isinstance(edge, dict):
                valid = False
                break
            parent, child = edge.get('parent'), edge.get('child')
            if (not _identity(parent) or not _identity(child) or parent == child
                    or not _identity(edge.get('forkPoint'))
                    or _project(edge.get('cwd')) != project
                    or (child in parents and parents[child] != edge)):
                valid = False
                break
            parents[child] = edge
        if not valid:
            continue
        chain = [current]
        chain_edges = []
        while chain[-1] in parents:
            edge = parents[chain[-1]]
            if edge['parent'] in chain:
                valid = False
                break
            chain.append(edge['parent'])
            chain_edges.append(edge)
        if not valid or len(chain) < 2:
            continue
        for sid in chain:
            candidates = by_id.get(sid, [])
            if len(candidates) != 1 or active.get(sid, set()) - {desktop}:
                valid = False
                break
            item = candidates[0]
            path = item.get('jsonl_path')
            if (not _in_project(item.get('project_path'), project) or not isinstance(path, str)
                    or not Path(path).is_file() or Path(path).stem != sid):
                valid = False
                break
        if valid:
            proposals.append((desktop, current, chain, chain_edges))
    # Cross-descriptor ownership conflicts must never silently hide a card.
    owners = {}
    for desktop, current, chain, edges in proposals:
        for sid in chain:
            owners.setdefault(sid, set()).add(desktop)
    for desktop, current, chain, edges in proposals:
        if any(len(owners[sid]) != 1 for sid in chain):
            continue
        history = []
        for sid, edge in zip(chain[1:], edges):
            item = by_id[sid][0]
            item.update(desktop_session_id=desktop, rewind_current_session_id=current)
            history.append({'session_id': sid,
                            'title': item.get('display_title') or item.get('title_override')
                            or item.get('custom_title') or sid,
                            'rewound_at': edge.get('at'), 'fork_point': edge['forkPoint']})
        by_id[current][0].update(desktop_session_id=desktop, rewind_history=history)
    return result


def metadata_for_session(session_id, items, descriptor_root=None):
    """Return only relationship fields for a selected card (no implicit scan)."""
    for item in annotate_sessions(items, descriptor_root):
        if item.get('id') == session_id:
            return {key: item[key] for key in
                    ('desktop_session_id', 'rewind_history', 'rewind_current_session_id')
                    if key in item}
    return {}


def _ordered_members(priors, current):
    """Return the descriptor's CLI session ids oldest to newest, current last."""
    members = []
    for value in priors:
        if value != current and value not in members:
            members.append(value)
    members.append(current)
    return members


def descriptor_memberships(descriptor_root=None):
    """Return every conversation a Desktop descriptor claims, without touching cards.

    A membership is the raw, descriptor-level answer to "which CLI session ids belong to
    this Desktop conversation": ``priorCliSessionIds`` in order, then ``cliSessionId``.
    ``relations`` names how each member follows its predecessor, and only an explicit
    ``rewindEdges`` entry can say ``rewind`` -- a prior without an edge is a plain
    ``continuation``. Fork and spawn pointers travel as lineage, never as membership.

    Nothing here inspects cards, so the caller still has to verify that each member
    resolves to exactly one card. Contradictory descriptors are dropped, not guessed at:
    the caller then sees no membership and keeps the records separate.
    """
    by_desktop = {}
    claimed = {}
    for descriptor in _descriptors(DESKTOP_ROOT if descriptor_root is None else descriptor_root):
        desktop = descriptor.get('sessionId')
        current = descriptor.get('cliSessionId')
        if not _identity(desktop) or not _identity(current):
            continue
        by_desktop.setdefault(desktop, []).append(descriptor)
        claimed.setdefault(current, set()).add(desktop)
    result = []
    for desktop, copies in by_desktop.items():
        descriptor = copies[0]
        keys = ('cliSessionId', 'cwd', 'rewindEdges', 'priorCliSessionIds')
        if any(any(copy.get(key) != descriptor.get(key) for key in keys) for copy in copies[1:]):
            continue  # Two stores disagree about the same conversation; trust neither.
        current = descriptor['cliSessionId']
        project = _project(descriptor.get('cwd'))
        if not project:
            continue
        priors = descriptor.get('priorCliSessionIds')
        if priors is None:
            priors = []
        if not isinstance(priors, list) or not all(_identity(value) for value in priors):
            continue  # Unreadable lineage is not a reason to invent one.
        edges = descriptor.get('rewindEdges')
        if edges is None:
            edges = []
        if not isinstance(edges, list):
            continue
        parents = {}
        valid = True
        for edge in edges:
            if not isinstance(edge, dict):
                valid = False
                break
            parent, child = edge.get('parent'), edge.get('child')
            if (not _identity(parent) or not _identity(child) or parent == child
                    or not _identity(edge.get('forkPoint'))
                    or _project(edge.get('cwd')) != project
                    or (child in parents and parents[child] != parent)):
                valid = False
                break
            parents[child] = parent
        if not valid:
            continue
        members = _ordered_members(priors, current)
        relations = {}
        for index, member in enumerate(members[1:], start=1):
            relations[member] = ('rewind' if parents.get(member) == members[index - 1]
                                 else 'continuation')
        for member in members:
            claimed.setdefault(member, set()).add(desktop)
        spawned = descriptor.get('spawnedFrom')
        spawned = spawned.get('sessionId') if isinstance(spawned, dict) else None
        forked = descriptor.get('forkedFromSessionId')
        result.append({
            'conversation_id': desktop,
            'members': members,
            'relations': relations,
            'current': current,
            'project': project,
            'forked_from_conversation_id': forked if _identity(forked) else None,
            'spawned_from_conversation_id': spawned if _identity(spawned) else None,
        })
    # A record two Desktop conversations both claim proves the evidence is wrong somewhere.
    # Dropping both memberships leaves the records separate instead of hiding one behind
    # the other, which is the only failure this layer is allowed to have.
    return [m for m in result if all(len(claimed.get(sid, ())) == 1 for sid in m['members'])]
