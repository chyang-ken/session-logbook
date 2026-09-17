"""Resolve Codex's explicit history boundaries without modifying source logs.

Physical provenance is retained for every record. Ordinals are coordinates within
an explicitly selected ancestry, not globally unique deduplication keys.
"""
import hashlib
import json
from pathlib import Path


def read_segment(path, stop=None):
    path = Path(path).resolve()
    records, issues, offset = [], [], 0
    try:
        with path.open('rb') as stream:
            line = 0
            while stop is None or offset < stop:
                raw = stream.readline(-1 if stop is None else stop - offset)
                if not raw:
                    break
                line += 1
                end = offset + len(raw)
                origin = {'path': str(path), 'line': line, 'start': offset, 'end': end}
                if not raw.endswith(b'\n'):
                    issues.append({**origin, 'reason': 'unfinished_record'})
                    break
                try:
                    row = json.loads(raw)
                    if not isinstance(row, dict):
                        raise ValueError('record is not an object')
                    if row.get('type') in {'session_meta', 'response_item', 'event_msg'} and not isinstance(row.get('payload'), dict):
                        raise ValueError('record payload is not an object')
                except (ValueError, UnicodeError):
                    issues.append({**origin, 'reason': 'invalid_record'})
                    offset = end
                    continue
                records.append({**origin, 'record': row})
                offset = end
    except OSError as exc:
        issues.append({'path': str(path), 'reason': 'unreadable_segment', 'detail': str(exc)})
    return records, issues


def boundary_ordinal(path, byte):
    """Read only the record ending at an explicit byte boundary, never the tail."""
    try:
        with Path(path).open('rb') as stream:
            stream.seek(0, 2)
            if byte <= 0 or byte > stream.tell():
                return None
            stream.seek(byte - 1)
            if stream.read(1) != b'\n':
                return None
            position, chunks = byte - 1, []
            while position:
                size = min(position, 4096)
                position -= size
                stream.seek(position)
                chunk = stream.read(size)
                newline = chunk.rfind(b'\n')
                if newline >= 0:
                    chunks.append(chunk[newline + 1:])
                    break
                chunks.append(chunk)
            row = json.loads(b''.join(reversed(chunks)))
            ordinal = row.get('ordinal') if isinstance(row, dict) else None
            return ordinal if type(ordinal) is int else None
    except (OSError, ValueError, UnicodeError):
        return None


def _meta(records):
    if records and records[0]['record'].get('type') == 'session_meta':
        return records[0]['record'].get('payload') or {}
    return {}


def resolve(path):
    """Resolve a linear ancestry iteratively, indexing each candidate snapshot once."""
    from sources import codex
    snapshots, visited, boundary_indexes = {}, set(), {}
    metadata_index = None

    def read(p, stop=None):
        p = Path(p).resolve()
        key = (p, stop)
        if key not in snapshots:
            snapshots[key] = read_segment(p, stop)
        return snapshots[key]

    def boundaries(sid, byte):
        nonlocal metadata_index
        if metadata_index is None:
            metadata_index = {}
            for root in (codex.CODEX_ROOT, codex.CODEX_ARCHIVED_ROOT):
                if root.exists():
                    for candidate in root.rglob('rollout-*.jsonl'):
                        meta = codex._read_session_meta(candidate) or {}
                        metadata_index.setdefault(meta.get('id'), set()).add(candidate.resolve())
        key = (sid, byte)
        if key not in boundary_indexes:
            index = {}
            for candidate in sorted(metadata_index.get(sid, ())):
                # Probe the explicit byte boundary before loading a candidate prefix.
                ordinal = boundary_ordinal(candidate, byte)
                if ordinal is not None:
                    index.setdefault(ordinal + 1, []).append(candidate)
            boundary_indexes[key] = index
        return boundary_indexes[key]

    layers = []
    p, stop = Path(path).resolve(), None
    while True:
        if p in visited:
            layers.append(([], [{'path': str(p), 'reason': 'history_cycle'}]))
            break
        visited.add(p)
        rows, errors = read(p, stop)
        meta = _meta(rows)
        selected = [r for r in rows if stop is None or r['end'] <= stop]
        errors = [e for e in errors if stop is None or e.get('start', 0) < stop]
        if not meta:
            errors.append({'path': str(p), 'reason': 'missing_session_meta'})
        ordinals = [r['record'].get('ordinal') for r in selected]
        if any(o is not None for o in ordinals):
            if (any(type(o) is not int for o in ordinals) or
                    any(b != a + 1 for a, b in zip(ordinals, ordinals[1:]))):
                errors.append({'path': str(p), 'reason': 'noncontiguous_ordinals'})
        layers.append((selected, errors))
        base = meta.get('history_base')
        if base and not isinstance(base, dict):
            errors.append({'path': str(p), 'reason': 'invalid_history_base'})
            break
        if not base:
            if meta.get('forked_from_id') or (ordinals and type(ordinals[0]) is int and ordinals[0] != 0):
                errors.append({'path': str(p), 'reason': 'missing_history_base',
                               'first_ordinal': ordinals[0] if ordinals else None})
            break
        sid, end, byte = base.get('thread_id'), base.get('end_ordinal_exclusive'), base.get('end_byte_offset')
        issue = {'path': str(p), 'thread_id': sid, 'end_ordinal_exclusive': end, 'end_byte_offset': byte}
        valid = isinstance(sid, str) and type(end) is int and end >= 0 and type(byte) is int and byte > 0
        if not valid:
            errors.append({**issue, 'reason': 'incomplete_history_boundary'})
            break
        if sid != meta.get('id') and not (meta.get('forked_from_id') == sid and meta.get('forked_from_ordinal_exclusive') == end):
            errors.append({**issue, 'reason': 'unverified_parent_history'})
            break
        if rows and rows[0]['record'].get('ordinal') != end:
            errors.append({**issue, 'reason': 'continuation_start_conflict'})
            break
        matches = [candidate for candidate in boundaries(sid, byte).get(end, ())
                   if candidate != p and candidate not in visited]
        # Equivalent active/archive copies have the same records, not merely equal ordinals.
        groups = {}
        for candidate in matches:
            prefix, prefix_errors = read(candidate, byte)
            if prefix_errors or not prefix or prefix[-1]['end'] != byte:
                continue
            digest = hashlib.sha256()
            for entry in prefix:
                digest.update(json.dumps(entry['record'], sort_keys=True).encode())
                digest.update(b'\n')
            groups.setdefault(digest.digest(), []).append(candidate)
        if len(groups) != 1:
            errors.append({**issue, 'reason': 'missing_history_segment' if not groups else 'ambiguous_history_segment'})
            break
        copies = next(iter(groups.values()))
        p = min(copies, key=lambda c: (codex._under_root(c, codex.CODEX_ARCHIVED_ROOT), str(c)))
        stop = byte

    records, issues, segments = [], [], []
    for rows, errors in reversed(layers):
        records.extend(rows)
        issues.extend(errors)
        if rows:
            meta = _meta(rows)
            segments.append({'path': rows[0]['path'], 'session_id': meta.get('id'), 'records': rows})
    return {'records': records, 'segments': segments, 'issues': issues, 'complete': not issues}


def fingerprint(path):
    history = resolve(path)
    # Include inherited records and gaps: repairing a missing ancestor must refresh UI.
    payload = [(r['path'], r['line'], r['record']) for r in history['records']]
    return hashlib.sha256(json.dumps([payload, history['issues']], sort_keys=True).encode()).hexdigest()


def require_complete(history):
    if not history['complete']:
        raise ValueError('context_incomplete: ' + json.dumps(history['issues'], sort_keys=True))


def cursor_position(history, source, line):
    """Locate a physical cursor; reject cursors in replaced or unrelated branches."""
    source = str(Path(source).resolve())
    own = [r for r in history['records'] if r['path'] == source]
    if not own:
        raise ValueError('cursor source is not in the effective history')
    if line < 0:
        raise ValueError('cursor must be nonnegative')
    if line > own[-1]['line']:
        raise ValueError('cursor is in a replaced or truncated history tail; reconcile context before continuing')
    return next((i for i, r in enumerate(history['records']) if r['path'] == source and r['line'] >= max(1, line)), len(history['records']))


def references(path, history=None):
    """Describe physical sources separately from the selected task's stable ID."""
    history = resolve(path) if history is None else history
    current = str(Path(path).resolve())
    from sources import codex
    sid = (codex._read_session_meta(path) or {}).get('id')
    return [{
        'path': segment['path'], 'session_id': segment['session_id'],
        'relation': 'inherited' if segment['session_id'] != sid else
                    'current' if segment['path'] == current else 'continuation',
        'first_line': segment['records'][0]['line'],
        'last_line': segment['records'][-1]['line'],
    } for segment in history['segments']]


def iter_messages(path):
    """Yield effective message text and its original physical evidence reference."""
    from sources import codex
    history = resolve(path)
    for entry in history['records']:
        record = entry['record']
        payload = record.get('payload') or {}
        if record.get('type') != 'response_item' or payload.get('type') != 'message':
            continue
        role = payload.get('role')
        if role not in {'user', 'assistant'}:
            continue
        text = codex._extract_text_from_message_content(payload.get('content'))
        if text:
            yield entry['path'], entry['line'], role, text


def canonical_paths(paths):
    """Search each known Codex ID's newest segment, never its replaced branch tails."""
    from sources import codex
    winners, others = {}, []
    for path in paths:
        if not codex.is_codex_path(path):
            others.append(path)
            continue
        meta = codex._read_session_meta(path) or {}
        sid = meta.get('id')
        if not sid:
            others.append(path)
            continue
        prev = winners.get(sid)
        if prev is None or codex.rollout_rank(path, meta) > codex.rollout_rank(prev):
            winners[sid] = path
    return others + list(winners.values())
