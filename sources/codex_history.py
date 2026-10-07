"""Resolve Codex's explicit history boundaries without modifying source logs.

Physical provenance is retained for every record. Ordinals are coordinates within
an explicitly selected ancestry, not globally unique deduplication keys.

A Codex conversation is keyed by session_meta.id; the *file* is what multiplies. One id
can span several rollout files: page rollovers linked by history_base, and — observed on
a restart after an aborted first turn — a same-id page that carries no history_base at
all. A user fork is the opposite case: a new id whose history_base points into another
session's file, so its inherited prefix belongs to that other session and is labelled
as such rather than presented as the fork's own.

A sub-agent is a third case and not a branch at all: Codex spawns it as a thread of its
own, with its own id, its own ordinals from 0, and its whole record in its own file. Its
lineage names the thread that started it; it never points into another file.
"""
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

# Rollout filenames observed so far always contain their session_meta.id, which makes the
# name a cheap candidate filter for same-id siblings. It is only a filter: identity is
# always confirmed by reading session_meta, never decided by the filename.
_SAFE_GLOB_ID = re.compile(r'[A-Za-z0-9._-]+')


def read_segment(path, stop=None, start=0, first_line=1):
    path = Path(path).resolve()
    records, issues, offset = [], [], start
    try:
        with path.open('rb') as stream:
            stream.seek(start)
            line = first_line - 1
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


def segment_alias(path, meta):
    """Recognize an observed physical-page alias without changing logical identity."""
    sid = meta.get('id')
    if not sid or meta.get('session_id') != sid or meta.get('history_mode') != 'paginated':
        return None
    parts = Path(path).stem.rsplit('_', 1)
    if (len(parts) == 2 and parts[0].endswith('-' + sid) and
            re.fullmatch(r'[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}', parts[1])):
        return parts[1]
    return None


def spawn_lineage(meta):
    """Describe a sub-agent rollout: a thread Codex started on behalf of another thread.

    Codex marks a spawned thread in the thread's own session_meta — source.subagent, and on
    newer clients parent_thread_id — and puts the root thread in session_id. Such a thread
    owns its whole record: it keeps its own id, its ordinals start at 0, and whatever context
    it was handed at spawn is copied into its own file (as the parent's session_meta followed
    by the parent's records, or as a compaction snapshot). It therefore never carries a
    history_base or a forked_from_ordinal_exclusive, and its forked_from_id — when a client
    writes one at all — repeats the parent or root id instead of marking a branch point.

    So the lineage of a spawned thread is a pointer to the thread that started it, never a
    boundary into another file. This is the one place that recognizes the native markers;
    fork_lineage() and resolve() both read it rather than re-deriving them.
    """
    if not isinstance(meta, dict):
        return None
    origin = meta.get('source')
    if not (meta.get('parent_thread_id') or (isinstance(origin, dict) and origin.get('subagent'))):
        return None

    def named(value):
        return value if isinstance(value, str) and value and value != meta.get('id') else None

    # parent_thread_id is the direct spawner; the oldest guardian rollouts predate it and
    # record only forked_from_id. session_id names the root thread of the whole spawn tree.
    return {'parent_session_id': (named(meta.get('parent_thread_id')) or
                                  named(meta.get('forked_from_id')) or
                                  named(meta.get('session_id'))),
            'root_session_id': named(meta.get('session_id'))}


def fork_lineage(meta):
    """Describe a deliberate user fork recorded in a rollout's own session_meta.

    forked_from_id is overloaded: in the observed corpus most occurrences belong to
    sub-agents, which also carry parent_thread_id or source.subagent. Only a rollout with
    no sub-agent marker and an id of its own is reported as a fork, so "the user branched"
    is never inferred from forked_from_id alone.
    """
    if not isinstance(meta, dict):
        return None
    parent = meta.get('forked_from_id')
    if not isinstance(parent, str) or not parent or parent == meta.get('id'):
        return None
    if spawn_lineage(meta):
        return None
    ordinal = meta.get('forked_from_ordinal_exclusive')
    return {'session_id': parent, 'ordinal_exclusive': ordinal if type(ordinal) is int else None}


def _epoch(stamp):
    """Parse a record timestamp; return None when it is absent or not a usable instant."""
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError):
        return None


def _time_span(rows):
    """Return (first, last) instants covered by these records, or None when unknown."""
    stamps = [_epoch(r['record'].get('timestamp')) for r in rows]
    stamps = [s for s in stamps if s is not None]
    return (min(stamps), max(stamps)) if stamps else None


def _opening_instant(path):
    """When a rollout's first record was written, read without loading the file.

    A rollout is an append-only log, so this bounds the whole file from below and lets a
    later page be skipped without parsing megabytes of records to learn it is later.
    """
    try:
        with Path(path).open('rb') as stream:
            row = json.loads(stream.readline())
    except (OSError, ValueError, UnicodeError):
        return None
    return _epoch(row.get('timestamp')) if isinstance(row, dict) else None


_IDENTITY_CACHE = {}


def _identity(candidate):
    """Return a rollout's (session id, page alias), rereading only a changed file."""
    from sources import codex
    try:
        st = candidate.stat()
        signature = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
    except OSError:
        signature = None
    cached = _IDENTITY_CACHE.get(candidate)
    if signature is not None and cached and cached[0] == signature:
        return cached[1]
    meta = codex._read_session_meta(candidate) or {}
    value = (meta.get('id'), segment_alias(candidate, meta))
    if signature is not None:
        _IDENTITY_CACHE[candidate] = (signature, value)
    return value


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

    def same_id_rollouts(sid):
        """Every rollout that claims this session id, whether history_base links it or not."""
        if metadata_index is not None:
            return set(metadata_index.get(sid, ()))
        pattern = ('rollout-*' + sid + '*.jsonl' if _SAFE_GLOB_ID.fullmatch(sid)
                   else 'rollout-*.jsonl')
        found = set()
        for root in (codex.CODEX_ROOT, codex.CODEX_ARCHIVED_ROOT):
            if not root.exists():
                continue
            for candidate in root.rglob(pattern):
                owner, _alias = _identity(candidate)
                if owner == sid:
                    found.add(candidate.resolve())
        return found

    def boundaries(sid, byte, logical_owner=None):
        nonlocal metadata_index
        if metadata_index is None:
            metadata_index = {}
            for root in (codex.CODEX_ROOT, codex.CODEX_ARCHIVED_ROOT):
                if root.exists():
                    for candidate in root.rglob('rollout-*.jsonl'):
                        owner, alias = _identity(candidate)
                        metadata_index.setdefault(owner, set()).add(candidate.resolve())
                        if alias:
                            metadata_index.setdefault((owner, alias), set()).add(candidate.resolve())
        identity = (logical_owner, sid) if logical_owner else sid
        key = (identity, byte)
        if key not in boundary_indexes:
            index = {}
            for candidate in sorted(metadata_index.get(identity, ())):
                # Probe the explicit byte boundary before loading a candidate prefix.
                ordinal = boundary_ordinal(candidate, byte)
                if ordinal is not None:
                    index.setdefault(ordinal + 1, []).append(candidate)
            boundary_indexes[key] = index
        return boundary_indexes[key]

    layers = []
    target, target_meta = Path(path).resolve(), None
    p, stop = target, None
    while True:
        if p in visited:
            layers.append(([], [{'path': str(p), 'reason': 'history_cycle'}]))
            break
        visited.add(p)
        rows, errors = read(p, stop)
        meta = _meta(rows)
        if target_meta is None:
            target_meta = meta
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
            # Two independent signs that records before this file's first one are missing.
            # A forked_from_id claims an inherited prefix — but only on a rollout that is not
            # a spawned sub-agent thread, where the same field merely repeats the parent id
            # and nothing external is inherited (see spawn_lineage). A first ordinal other
            # than 0 means earlier records existed in this ordinal space, and that holds for
            # a sub-agent as much as for anyone, so it is checked for every rollout.
            claims_prefix = bool(meta.get('forked_from_id')) and not spawn_lineage(meta)
            starts_late = bool(ordinals) and type(ordinals[0]) is int and ordinals[0] != 0
            if claims_prefix or starts_late:
                errors.append({'path': str(p), 'reason': 'missing_history_base',
                               'first_ordinal': ordinals[0] if ordinals else None})
            break
        sid, end, byte = base.get('thread_id'), base.get('end_ordinal_exclusive'), base.get('end_byte_offset')
        issue = {'path': str(p), 'thread_id': sid, 'end_ordinal_exclusive': end, 'end_byte_offset': byte}
        valid = isinstance(sid, str) and type(end) is int and end >= 0 and type(byte) is int and byte > 0
        if not valid:
            errors.append({**issue, 'reason': 'incomplete_history_boundary'})
            break
        physical_alias = sid != meta.get('id') and not (
            meta.get('forked_from_id') == sid and meta.get('forked_from_ordinal_exclusive') == end)
        if physical_alias and (not isinstance(meta.get('id'), str) or not meta['id']):
            errors.append({**issue, 'reason': 'unverified_parent_history'})
            break
        if rows and rows[0]['record'].get('ordinal') != end:
            errors.append({**issue, 'reason': 'continuation_start_conflict'})
            break
        matches = [candidate for candidate in boundaries(sid, byte, meta.get('id') if physical_alias else None).get(end, ())
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
            reason = ('unverified_parent_history' if physical_alias and not matches else
                      'missing_history_segment' if not groups else 'ambiguous_history_segment')
            errors.append({**issue, 'reason': reason})
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

    # A same-id rollout that history_base never reaches is still a page of this session.
    # Observed shape: restarting after an aborted first turn writes a new page alias with no
    # history_base, so the aborted page — holding a real user message — is linked only by the
    # filename. Place it when record timestamps prove where it belongs; otherwise say so.
    # Never drop it: the previous behaviour reported complete with those messages missing.
    sid = (target_meta or {}).get('id')
    if isinstance(sid, str) and sid:
        known = {Path(segment['path']).resolve() for segment in segments}
        # Archiving moves a rollout between roots with its name and bytes unchanged, so an
        # equal filename elsewhere is the same page rather than a second one.
        known_names = {p.name for p in known}
        selected_span = _time_span(records)
        candidates, unplaceable = [], []
        for candidate in sorted(same_id_rollouts(sid)):
            if candidate in known or candidate.name in known_names:
                continue
            # A page written entirely after everything selected here is a later page of this
            # session, not missing history: reading an earlier entry point means "the history
            # up to this point", the same way a replaced tail is excluded. Decide that from
            # the opening record alone so a large later page is never loaded. Every rollout
            # in the observed corpus carries that timestamp; one without it falls through and
            # is reported below rather than guessed at.
            opening = _opening_instant(candidate)
            if selected_span is not None and opening is not None and opening > selected_span[1]:
                continue
            rows, errors = read(candidate)
            span = _time_span(rows)
            if not rows or span is None or selected_span is None:
                unplaceable.append((span, candidate, rows, errors))
            else:
                candidates.append((span, candidate, rows, errors))
        # Ordinals restart at 0 in an unlinked page, so they cannot order it; only a record-time
        # range ending before everything already selected can. Walk from the latest candidate
        # backwards so each one has to fit in front of what is already placed.
        start = selected_span[0] if selected_span else None
        for span, candidate, rows, errors in sorted(candidates, key=lambda o: o[0], reverse=True):
            if start is None or span[1] >= start:
                unplaceable.append((span, candidate, rows, errors))
                continue
            records = rows + records
            issues = errors + issues
            segments.insert(0, {'path': rows[0]['path'], 'session_id': sid,
                                'records': rows, 'relation': 'unlinked'})
            start = span[0]
        for _span, candidate, _rows, _errors in unplaceable:
            issues.append({'path': str(candidate), 'session_id': sid,
                           'reason': 'unlinked_same_id_segment'})

    current = str(target)
    for segment in segments:
        segment.setdefault('relation',
                           'inherited' if segment['session_id'] != sid else
                           'current' if segment['path'] == current else 'continuation')
    return {'records': records, 'segments': segments, 'issues': issues,
            'complete': not issues, 'forked_from': fork_lineage(target_meta),
            'spawned_from': spawn_lineage(target_meta)}


def load(path):
    """Return a session's effective history; the single entry point for readers.

    Same shape as resolve(). When the history index is available, verified coordinates
    replace re-deriving the ancestry and only the needed byte ranges are read. Any index
    miss, source change during the read, or index failure falls back to resolve().
    Readers must call this rather than resolve() (enforced by tests), so the physical
    layout of a session never leaks into features.
    """
    from sources import history_index
    if history_index.index_path() is None:
        return resolve(path)
    plan = history_index.plan(path, resolve)
    records, segments = [], []
    try:
        for segment in plan['segments']:
            rows = history_index.window(segment)
            records.extend(rows)
            if rows:
                segments.append({'path': segment['path'], 'session_id': segment['session_id'],
                                 'relation': segment.get('relation'), 'records': rows})
    except (OSError, ValueError):
        return resolve(path)
    if not segments and not plan['complete']:
        return resolve(path)
    from sources import codex
    meta = codex._read_session_meta(path)
    return {'records': records, 'segments': segments, 'issues': plan['issues'],
            'complete': plan['complete'],
            'forked_from': fork_lineage(meta), 'spawned_from': spawn_lineage(meta)}


def fingerprint(path):
    from sources import history_index
    if history_index.index_path() is not None:
        plan = history_index.plan(path, resolve)
        if plan['complete'] and all('prefix_hash' in s for s in plan['segments']):
            identity = [(s['path'], s['coordinates'][-1], s['prefix_hash']) for s in plan['segments']]
            return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
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
    history = load(path) if history is None else history
    current = str(Path(path).resolve())
    from sources import codex
    sid = (codex._read_session_meta(path) or {}).get('id')
    return [{
        'path': segment['path'], 'session_id': segment['session_id'],
        # resolve() labels each segment; an ad-hoc manifest assembled by a caller does not.
        'relation': segment.get('relation') or (
                    'inherited' if segment['session_id'] != sid else
                    'current' if segment['path'] == current else 'continuation'),
        'first_line': segment['records'][0]['line'],
        'last_line': segment['records'][-1]['line'],
    } for segment in history['segments']]


def iter_messages(path):
    """Yield effective message text and its original physical evidence reference."""
    from sources import codex
    history = load(path)
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
