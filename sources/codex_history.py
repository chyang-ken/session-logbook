"""Resolve Codex's explicit history boundaries without modifying source logs.

Physical provenance is retained for every record. Ordinals are coordinates within
an explicitly selected ancestry, not globally unique deduplication keys.
"""
import hashlib
import json
from pathlib import Path


def read_segment(path):
    path = Path(path).resolve()
    records, issues, offset = [], [], 0
    try:
        with path.open('rb') as stream:
            for line, raw in enumerate(stream, 1):
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


def _meta(records):
    if records and records[0]['record'].get('type') == 'session_meta':
        return records[0]['record'].get('payload') or {}
    return {}


def resolve(path):
    """Return verified inherited prefixes plus this segment, with explicit gaps."""
    from sources import codex
    snapshots, visited = {}, set()

    def read(p):
        p = Path(p).resolve()
        if p not in snapshots:
            snapshots[p] = read_segment(p)
        return snapshots[p]

    def candidates(sid):
        # The fallback also covers nonstandard filenames; metadata is authoritative.
        found = []
        for root in (codex.CODEX_ROOT, codex.CODEX_ARCHIVED_ROOT):
            if root.exists():
                for p in root.rglob('rollout-*.jsonl'):
                    meta = codex._read_session_meta(p)
                    if meta and meta.get('id') == sid:
                        found.append(p.resolve())
        return sorted(set(found))

    def walk(p, stop=None):
        p = Path(p).resolve()
        if p in visited:
            return [], [{'path': str(p), 'reason': 'history_cycle'}]
        visited.add(p)
        rows, errors = read(p)
        meta = _meta(rows)
        selected = [r for r in rows if stop is None or r['end'] <= stop]
        errors = [e for e in errors if stop is None or e.get('start', 0) < stop]
        if not meta:
            errors = errors + [{'path': str(p), 'reason': 'missing_session_meta'}]
        ordinals = [r['record'].get('ordinal') for r in selected]
        if any(o is not None for o in ordinals):
            if (any(type(o) is not int for o in ordinals) or
                    any(b != a + 1 for a, b in zip(ordinals, ordinals[1:]))):
                errors = errors + [{'path': str(p), 'reason': 'noncontiguous_ordinals'}]
        base = meta.get('history_base')
        inherited = []
        if base and not isinstance(base, dict):
            errors = errors + [{'path': str(p), 'reason': 'invalid_history_base'}]
            base = None
        if base:
            sid = base.get('thread_id')
            end = base.get('end_ordinal_exclusive')
            byte = base.get('end_byte_offset')
            issue = {'path': str(p), 'thread_id': sid, 'end_ordinal_exclusive': end,
                     'end_byte_offset': byte}
            valid = isinstance(sid, str) and type(end) is int and end >= 0 and type(byte) is int and byte > 0
            if not valid:
                errors = errors + [{**issue, 'reason': 'incomplete_history_boundary'}]
            elif sid != meta.get('id') and not (
                    meta.get('forked_from_id') == sid and
                    meta.get('forked_from_ordinal_exclusive') == end):
                errors = errors + [{**issue, 'reason': 'unverified_parent_history'}]
            elif rows and rows[0]['record'].get('ordinal') != end:
                errors = errors + [{**issue, 'reason': 'continuation_start_conflict'}]
            else:
                matches = []
                for candidate in candidates(sid):
                    if candidate == p or candidate in visited:
                        continue
                    cr, ce = read(candidate)
                    prefix = [r for r in cr if r['end'] <= byte]
                    if (prefix and prefix[-1]['end'] == byte and
                            prefix[-1]['record'].get('ordinal') == end - 1 and
                            not any(e.get('start', 0) < byte for e in ce)):
                        matches.append((candidate, prefix))
                # Identical active/archive copies are one source, not an ambiguity.
                groups = {}
                for candidate, prefix in matches:
                    signature = json.dumps([r['record'] for r in prefix], sort_keys=True)
                    groups.setdefault(signature, []).append(candidate)
                if len(groups) != 1:
                    errors = errors + [{**issue, 'reason': 'missing_history_segment' if not groups else 'ambiguous_history_segment'}]
                else:
                    copies = next(iter(groups.values()))
                    candidate = min(copies, key=lambda c: (codex._under_root(c, codex.CODEX_ARCHIVED_ROOT), str(c)))
                    inherited, prior_errors = walk(candidate, byte)
                    errors = prior_errors + errors
        elif meta.get('forked_from_id') or (ordinals and type(ordinals[0]) is int and ordinals[0] != 0):
            errors = errors + [{'path': str(p), 'reason': 'missing_history_base', 'first_ordinal': ordinals[0]}]
        visited.remove(p)
        return inherited + selected, errors

    records, issues = walk(path)
    segments = []
    for row in records:
        if not segments or segments[-1]['path'] != row['path']:
            meta = _meta(read(row['path'])[0])
            segments.append({'path': row['path'], 'session_id': meta.get('id'), 'records': []})
        segments[-1]['records'].append(row)
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
