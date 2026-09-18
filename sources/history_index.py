"""Rebuildable Codex history coordinates; never a consumer delivery cursor."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3

SCHEMA = 1


def index_path():
    from sources import codex
    configured = os.environ.get('SESSION_LOGBOOK_HISTORY_INDEX')
    if configured == 'off':
        return None
    if configured:
        return Path(configured).expanduser()
    # Isolated source fixtures must never write to the user's resident index.
    if codex.CODEX_ROOT.resolve() != (Path.home() / '.codex/sessions').resolve():
        return None
    return Path.home() / '.session-logbook/history-index.sqlite3'


def signature(path):
    st = Path(path).stat()
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def digest(path, end):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while end:
            chunk = stream.read(min(end, 65536))
            if not chunk:
                raise ValueError('history source truncated')
            result.update(chunk)
            end -= len(chunk)
    return result


def catalog(connection):
    from sources import codex
    saved = {p: (sig, sid) for p, sig, sid in connection.execute('SELECT path, signature, session_id FROM files')}
    found, updates = {}, []
    for root in (codex.CODEX_ROOT, codex.CODEX_ARCHIVED_ROOT):
        if not root.exists():
            continue
        for path in root.rglob('rollout-*.jsonl'):
            key = str(path.resolve())
            try:
                sig = signature(path)
            except OSError:
                continue
            previous = saved.get(key)
            if previous and previous[0] == encode(sig):
                sid = previous[1]
            else:
                sid = (codex._read_session_meta(path) or {}).get('id')
                updates.append((key, encode(sig), sid))
            found[key] = {'signature': sig, 'session_id': sid}
    with connection:
        connection.executemany('INSERT OR REPLACE INTO files VALUES (?, ?, ?)', updates)
        connection.executemany('DELETE FROM files WHERE path=?', [(p,) for p in saved.keys() - found.keys()])
    return found


def relevant(catalogue, ids):
    return {p: item['signature'] for p, item in catalogue.items() if item['session_id'] in ids}


def from_history(history, retain_records=False):
    segments = []
    for segment in history['segments']:
        rows = segment['records']
        segments.append({'path': segment['path'], 'session_id': segment['session_id'],
                         'coordinates': [[r['line'], r['start'], r['end'], r['record'].get('ordinal')] for r in rows],
                         **({'_records': rows} if retain_records else {})})
    return {'complete': history['complete'], 'issues': history['issues'], 'segments': segments}


def window(segment, first=1):
    """Read the requested physical rows only; coordinates never replace evidence."""
    from sources import codex_history
    if '_records' in segment:
        return [r for r in segment['_records'] if r['line'] >= first]
    coordinates = segment['coordinates']
    chosen = [c for c in coordinates if c[0] >= first]
    if not chosen:
        return []
    path = Path(segment['path'])
    expected = segment.get('signature')
    if expected and signature(path) != expected:
        raise ValueError('history source changed during read; retry observation')
    rows, issues = codex_history.read_segment(path, stop=chosen[-1][2],
                                             start=chosen[0][1], first_line=chosen[0][0])
    actual = [[r['line'], r['start'], r['end'], r['record'].get('ordinal')] for r in rows]
    if issues or actual != chosen or (expected and signature(path) != expected):
        raise ValueError('history source changed during read; retry observation')
    return rows


def validate(value):
    if value.get('version') != SCHEMA or not value.get('segments'):
        return False
    for segment in value['segments']:
        coordinates = segment.get('coordinates')
        if not coordinates or not isinstance(segment.get('path'), str):
            return False
        end = 0
        for number, coordinate in enumerate(coordinates, 1):
            if (len(coordinate) != 4 or coordinate[0] != number or coordinate[1] != end or
                    not isinstance(coordinate[2], int) or coordinate[2] <= end):
                return False
            end = coordinate[2]
    return True


def _plan(connection, target, resolver):
    from sources import codex_history
    catalogue = catalog(connection)
    row = connection.execute('SELECT payload, checksum FROM histories WHERE path=?', (str(target),)).fetchone()
    saved = None
    if row:
        try:
            value = json.loads(row[0])
            if hashlib.sha256(row[0].encode()).hexdigest() == row[1] and validate(value):
                saved = value
        except (ValueError, TypeError, KeyError):
            pass
    value = None
    if saved:
        current_inventory = relevant(catalogue, saved['session_ids'])
        changed = {p for p in current_inventory.keys() | saved['inventory'].keys()
                   if current_inventory.get(p) != saved['inventory'].get(p)}
        if not changed:
            saved['cache'] = 'hit'
            return saved
        latest = saved['segments'][-1]
        old = saved['inventory'].get(str(target))
        new = current_inventory.get(str(target))
        if changed == {str(target)} and latest['path'] == str(target) and old and new and old[:2] == new[:2] and new[2] > old[2]:
            end = latest['coordinates'][-1]
            hasher = digest(target, end[2])
            if hasher.hexdigest() == latest['prefix_hash']:
                rows, issues = codex_history.read_segment(target, start=end[2], first_line=end[0] + 1)
                ordinal = end[3]
                for entry in rows:
                    record = entry['record']
                    next_ordinal = record.get('ordinal')
                    if (record.get('type') == 'session_meta' or
                            (ordinal is not None and (type(next_ordinal) is not int or next_ordinal != ordinal + 1)) or
                            (ordinal is None and next_ordinal is not None)):
                        issues.append({'reason': 'noncontiguous_ordinals'})
                    ordinal = next_ordinal
                if not issues and rows:
                    # Hash the exact appended bytes; a second signature check below
                    # prevents accepting a mixed snapshot if the source changes.
                    with target.open('rb') as stream:
                        stream.seek(end[2])
                        for chunk in iter(lambda: stream.read(65536), b''):
                            hasher.update(chunk)
                    latest['coordinates'].extend([[r['line'], r['start'], r['end'], r['record'].get('ordinal')] for r in rows])
                    latest['prefix_hash'] = hasher.hexdigest()
                    latest['signature'] = new
                    saved.update(inventory=current_inventory, cache='append')
                    value = saved
    if value is None:
        history = resolver(target)
        value = from_history(history, retain_records=not history['complete'])
        if not value['complete']:
            return value
        ids = sorted({s['session_id'] for s in value['segments'] if s['session_id']})
        for segment in value['segments']:
            segment['signature'] = signature(segment['path'])
            segment['prefix_hash'] = digest(segment['path'], segment['coordinates'][-1][2]).hexdigest()
        value.update(version=SCHEMA, session_ids=ids, inventory=relevant(catalogue, ids), cache='rebuilt')
    after = relevant(catalog(connection), value['session_ids'])
    if (after != value['inventory'] or
            any(after.get(s['path']) != s['signature'] for s in value['segments'])):
        return {'complete': False, 'issues': [{'reason': 'history_changed_during_read'}], 'segments': []}
    text = encode(value)
    with connection:
        connection.execute('INSERT OR REPLACE INTO histories VALUES (?, ?, ?)',
                           (str(target), text, hashlib.sha256(text.encode()).hexdigest()))
    return value


def plan(path, resolver):
    """Cache availability can affect performance, never the returned source truth."""
    location = index_path()
    if location is None:
        return from_history(resolver(path), retain_records=True)
    connection = None
    try:
        location.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(str(location), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        except FileExistsError:
            pass
        connection = sqlite3.connect(str(location), timeout=0.2)
        connection.execute('CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, signature TEXT NOT NULL, session_id TEXT)')
        connection.execute('CREATE TABLE IF NOT EXISTS histories(path TEXT PRIMARY KEY, payload TEXT NOT NULL, checksum TEXT NOT NULL)')
        return _plan(connection, Path(path).resolve(), resolver)
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, IndexError):
        result = from_history(resolver(path), retain_records=True)
        result['cache'] = 'unavailable'
        return result
    finally:
        if connection is not None:
            connection.close()
