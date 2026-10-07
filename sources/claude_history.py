"""Observed Claude history overlap, not continuation or supervision authority.

The selected file owns its transcript. Related files remain separate evidence;
shared UUIDs never cause a session to resolve to a different ID automatically.
The rebuildable index contains coordinates and hashes, never message bodies.
"""
from contextlib import closing
import hashlib
import os
import json
from pathlib import Path
import sqlite3

from sources import history_index
from sources.claude_text import anchored_user_text, normalize_record

# 6: `user_turn` stopped counting the harness's user-role pseudo-messages (a background-task
# notice, bash output, a teammate report, a slash-command injection). A cache written under 5
# would keep feeding [U#] the old numbering, so the bump forces a rebuild.
# 7: `user_turn` stopped counting the interrupt marker the client writes on Esc, and started
# counting typed text that shares a record with a tool result. Same reason to rebuild.
# 8: `user_turn` stopped counting the summary the client writes at a compaction
# (`isCompactSummary`), so [U#] moves down by one after every compaction in a Session.
SCHEMA = 8
_MEMORY = {}


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def _payload(row):
    # Copies can update token usage and session/parent metadata at compaction.
    # Compare actual rendered content, not transport accounting or identity.
    message = row.get('message') or {}
    if not isinstance(message, dict):
        message = {'content': message}
    return _hash({'type': row.get('type'), 'subtype': row.get('subtype'),
                  'role': message.get('role'), 'content': message.get('content'),
                  'text': row.get('content'), 'attachment': row.get('attachment'),
                  'isMeta': row.get('isMeta'),
                  'compactMetadata': row.get('compactMetadata')})


def _index_path(path):
    if not os.environ.get('SESSION_LOGBOOK_HISTORY_INDEX'):
        try:
            Path(path).relative_to(Path.home() / '.claude/projects')
        except ValueError:
            return None  # Synthetic fixtures and custom roots are memory-only by default.
    return history_index.index_path()


def _load(path):
    cache = _index_path(path)
    if cache is None:
        return None
    try:
        with closing(sqlite3.connect(str(cache), timeout=1)) as connection, connection:
            connection.execute('CREATE TABLE IF NOT EXISTS claude_sources (path TEXT PRIMARY KEY, value TEXT)')
            found = connection.execute('SELECT value FROM claude_sources WHERE path=?', (str(path),)).fetchone()
            return json.loads(found[0]) if found else None
    except (OSError, sqlite3.Error, ValueError):
        return None


def _save(path, value):
    cache = _index_path(path)
    if cache is None:
        return
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(str(cache), timeout=1)) as connection, connection:
            connection.execute('CREATE TABLE IF NOT EXISTS claude_sources (path TEXT PRIMARY KEY, value TEXT)')
            connection.execute('INSERT OR REPLACE INTO claude_sources VALUES (?, ?)',
                               (str(path), json.dumps(value)))
    except (OSError, sqlite3.Error):
        pass  # Source reads work even when the optional cache is unavailable.


def summary(path):
    path = Path(path).resolve()
    signature = history_index.signature(path)
    old = _MEMORY.get(str(path)) or _load(path)
    if old and old.get('schema') == SCHEMA and old.get('signature') == signature:
        _MEMORY[str(path)] = old
        return old
    append = (old and old.get('schema') == SCHEMA and old['signature'][:2] == signature[:2]
              and signature[2] >= old['end']
              and history_index.digest(path, old['end']).hexdigest() == old['digest'])
    value = json.loads(json.dumps(old)) if append else {
        'schema': SCHEMA, 'rows': [], 'compactions': [], 'session_ids': [],
        'end': 0, 'total_lines': 0, 'issues': []}
    with path.open('rb') as stream:
        stream.seek(value['end'])
        while True:
            offset = stream.tell()
            raw = stream.readline()
            if not raw or not raw.endswith(b'\n'):
                break
            value['total_lines'] += 1
            value['end'] = stream.tell()
            line = value['total_lines']
            try:
                row = json.loads(raw)
                if not isinstance(row, dict):
                    raise ValueError('not an object')
            except (ValueError, UnicodeDecodeError):
                value['issues'].append({'reason': 'invalid_record', 'line': line})
                continue
            sid = row.get('sessionId')
            if isinstance(sid, str) and sid not in value['session_ids']:
                value['session_ids'].append(sid)
            uuid = row.get('uuid')
            value['rows'].append({'line': line, 'offset': offset, 'end': stream.tell(),
                                  'uuid': uuid, 'hash': _payload(row),
                                  'parent': row.get('parentUuid'), 'type': normalize_record(row).get('type'),
                                  'leaf': row.get('leafUuid'),
                                  'sidechain': row.get('isSidechain', False),
                                  'user_turn': bool(anchored_user_text(row))})
            if row.get('subtype') == 'compact_boundary':
                compact = row.get('compactMetadata') or {}
                value['compactions'].append({'line': line, 'uuid': uuid,
                    'logical_parent_uuid': row.get('logicalParentUuid'),
                    'trigger': compact.get('trigger'),
                    'preserved_segment': compact.get('preservedSegment'),
                    'preserved_messages': compact.get('preservedMessages')})
    if history_index.signature(path) != signature:
        raise ValueError('Claude source changed during indexing; retry the read')
    value['physical_lines'] = value['total_lines'] + int(signature[2] > value['end'])
    value['signature'] = signature
    value['digest'] = history_index.digest(path, value['end']).hexdigest()
    if history_index.signature(path) != signature:
        raise ValueError('Claude source changed during indexing; retry the read')
    _MEMORY[str(path)] = value
    _save(path, value)
    return value


def session_id(path, value=None):
    value = summary(path) if value is None else value
    ids = value['session_ids']
    return ids[0] if len(ids) == 1 else None


def _uuids(value):
    result = {}
    for row in value['rows']:
        if row['uuid']:
            result.setdefault(row['uuid'], []).append(row)
    return result


def describe(path):
    path = Path(path).resolve()
    current = summary(path)
    own = _uuids(current)
    sources = [{'relation': 'selected', 'path': str(path),
                'session_id': session_id(path, current), 'first_line': 1,
                'last_line': current['total_lines']}]
    issues = list(current['issues'])
    issues += [{'reason': 'conflicting_record_uuid', 'uuid': uuid}
               for uuid, rows in own.items() if len({r['hash'] for r in rows}) > 1]
    if len(current['session_ids']) != 1:
        issues.append({'reason': 'ambiguous_or_missing_session_id'})
    # Stay inside one Claude project directory; do not scan unrelated projects
    # or subagent trees to infer parentage.
    for other in sorted(path.parent.glob('*.jsonl')):
        if other.resolve() == path:
            continue
        try:
            candidate = summary(other)
        except (OSError, ValueError):
            continue
        shared = own.keys() & _uuids(candidate).keys()
        if not shared:
            continue
        matches = [row for row in candidate['rows'] if row['uuid'] in shared]
        conflicts = sum(not any(r['hash'] == row['hash'] for r in own[row['uuid']]) for row in matches)
        sources.append({'relation': 'shared_history', 'path': str(other.resolve()),
            'session_id': session_id(other, candidate), 'signature': candidate['signature'], 'first_line': 1,
            'last_line': candidate['total_lines'], 'shared_records': len(shared),
            'shared_first_line': matches[0]['line'], 'shared_last_line': matches[-1]['line'],
            'content_conflicts': conflicts,
            'relationship_kind': 'same_id_copy' if session_id(path, current) and
                session_id(path, current) == session_id(other, candidate) else 'undetermined',
            'unshared_records': sum(bool(r['uuid']) and r['uuid'] not in shared for r in candidate['rows'])})
    return {'source_files': sources, 'history_semantics': 'selected_file_only',
            'continuation_status': 'unconfirmed', 'compactions': current['compactions'],
            'rewind': _selection(current)[1],
            'history_issues': issues, 'history_fingerprint': _hash(sources + [current['signature']]),
            'history_note': 'Shared records prove overlap, not continuation versus fork. '
                            'Related files are evidence only; no automatic target migration.'}


def remap_cursor(path, source, line):
    """Map an explicitly selected new target; never choose a successor for callers."""
    path, source = Path(path).resolve(), Path(source).resolve()
    line = int(line)
    if line < 0:
        raise ValueError('cursor must be nonnegative')
    target = summary(path)
    if path == source:
        if line > target['physical_lines']:
            raise ValueError('cursor is beyond current source; reconcile context')
        return line
    previous = summary(source)
    if not session_id(path, target) or not session_id(source, previous):
        raise ValueError('ambiguous Claude identity; reconcile context')
    if source.parent != path.parent and session_id(path, target) != session_id(source, previous):
        raise ValueError('cross-ID Claude cursor sources must belong to the same project directory')
    if line == 0 or line > previous['total_lines']:
        raise ValueError('source change requires a valid nonzero source cursor; read context first')
    # Metadata after a record may share that record's delivery boundary. Never
    # walk backwards past an unshared UUID: that would discard a divergent tail.
    anchor = next((r for r in reversed(previous['rows']) if r['line'] <= line and r['uuid']), None)
    candidates = _uuids(target).get(anchor['uuid'], []) if anchor else []
    if len(candidates) != 1 or candidates[0]['hash'] != anchor['hash']:
        raise ValueError('cursor record is absent, conflicting, or ambiguous in selected history; '
                         'reconcile context; the old branch remains readable at its original path')
    if len(_uuids(previous).get(anchor['uuid'], [])) != 1:
        raise ValueError('source cursor UUID is ambiguous; reconcile context')
    mapped = candidates[0]['line']
    consumed = {(r['uuid'], r['hash']) for r in previous['rows'] if r['line'] <= line and r['uuid']}
    for row in target['rows']:
        if row['line'] >= mapped:
            break
        if row['type'] in {'user', 'assistant', 'system'} and (row['uuid'], row['hash']) not in consumed:
            raise ValueError('selected history contains new or changed context before the mapped cursor; '
                             'reconcile context before continuing')
    return mapped


def _selection(value):
    """Select a fully evidenced parent chain; uncertainty always preserves records.

    A last-prompt leaf is an explicit client pointer, unlike physical recency.
    Compaction can rewrite parentage, so it requires a separate format adapter.
    This selects saved history; it does not infer who initiated a rewind.
    """
    info = {'status': 'unchanged', 'evidence': None, 'hidden_record_count': 0,
            'hidden_message_count': 0, 'active_leaf_uuid': None, 'reason': None}

    def uncertain(reason):
        info.update(status='unverified', reason=reason)
        return None, info

    pointers = [row for row in value['rows'] if row['type'] == 'last-prompt']
    if not pointers:
        return uncertain('missing_leaf_pointer')
    if value['compactions']:
        return uncertain('compacted_history')
    if value['issues'] or value['physical_lines'] != value['total_lines']:
        return uncertain('incomplete_or_invalid_records')
    if len(value['session_ids']) != 1:
        return uncertain('ambiguous_session_identity')
    nodes = {}
    for row in value['rows']:
        if row['type'] in {'user', 'assistant'} and not row['uuid']:
            return uncertain('message_without_uuid')
        if not row['uuid']:
            continue
        if row['sidechain']:
            return uncertain('embedded_sidechain')
        existing = nodes.get(row['uuid'])
        if existing and (existing['hash'], existing['parent']) != (row['hash'], row['parent']):
            return uncertain('conflicting_record_uuid')
        nodes.setdefault(row['uuid'], row)
    pointer = pointers[-1]
    leaf = pointer['leaf']
    if not isinstance(leaf, str) or leaf not in nodes:
        return uncertain('missing_leaf_record')
    roots = [uuid for uuid, row in nodes.items() if row['parent'] is None]
    if len(roots) != 1:
        return uncertain('ambiguous_history_roots')
    # Validate every node before hiding anything, including abandoned branches.
    # Missing links or cycles may indicate incomplete copies rather than rewind.
    valid = set()
    for uuid in nodes:
        visiting = set()
        current = uuid
        while current is not None and current not in valid:
            if current not in nodes:
                return uncertain('missing_parent_record')
            if current in visiting:
                return uncertain('cyclic_parent_chain')
            visiting.add(current)
            current = nodes[current]['parent']
        valid.update(visiting)
    # A running turn can append after its last saved pointer. Accept only one
    # contiguous extension of that exact leaf, never "the newest row wins".
    for row in value['rows']:
        if row['line'] <= pointer['line'] or not row['uuid']:
            continue
        if nodes[row['uuid']]['line'] != row['line']:
            continue
        if row['parent'] != leaf:
            return uncertain('ambiguous_records_after_pointer')
        leaf = row['uuid']
    selected = set()
    current = leaf
    while current is not None:
        selected.add(current)
        current = nodes[current]['parent']
    hidden = [row for uuid, row in nodes.items() if uuid not in selected]
    if any(row['parent'] == leaf for row in hidden):
        return uncertain('leaf_has_unselected_descendants')
    info.update(evidence='last_prompt_leaf', active_leaf_uuid=leaf,
                hidden_record_count=len(hidden),
                hidden_message_count=sum(row['type'] in {'user', 'assistant'} for row in hidden))
    if hidden:
        info['status'] = 'selected'
    return selected, info


def rewind_status(path):
    """Return selected-chain facts without scanning sibling files."""
    return _selection(summary(path))[1]


def hidden_lines(path, upto):
    """Physical lines at or before ``upto`` that the selected chain no longer contains.

    Returns ``None`` when the chain cannot be verified: every record is then preserved,
    so nothing can be declared removed. A cursor-holding reader uses the list to retire
    anchors it already consumed, instead of re-reading the branch to find them.
    """
    value = summary(path)
    selected = _selection(value)[0]
    if selected is None:
        return None
    return sorted({row['line'] for row in value['rows']
                   if row['uuid'] and row['uuid'] not in selected and row['line'] <= int(upto)})


def records(path, first=0, include_rewound=False):
    """Read selected history with original physical anchors; opt in to all branches."""
    path = Path(path).resolve()
    value = summary(path)
    if int(first) > value['physical_lines'] or int(first) < 0:
        raise ValueError('cursor is outside current source; reconcile context')
    selected = None if include_rewound else _selection(value)[0]
    seen = set()
    user_turn = 0
    with path.open('rb') as stream:
        for row in value['rows']:
            key = (row['uuid'], row['hash']) if row['uuid'] else None
            duplicate = key is not None and key in seen
            if key:
                seen.add(key)
            if duplicate:
                continue
            if selected is not None and row['uuid'] and row['uuid'] not in selected:
                continue
            user_turn += int(row['user_turn'])
            if row['line'] < int(first):
                continue
            stream.seek(row['offset'])
            raw = stream.read(row['end'] - row['offset'])
            record = normalize_record(json.loads(raw))
            record['_logbook_user_turn'] = user_turn
            yield row['line'], record
    if history_index.signature(path) != value['signature']:
        raise ValueError('Claude source changed during read; retry observation')
