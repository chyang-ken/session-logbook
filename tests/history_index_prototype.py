"""Isolated experiment only: no production imports or default cache location."""
import builtins
import hashlib
import json
from pathlib import Path
import sys
import time
import tempfile
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from sources import codex, codex_history


def signature(path):
    st = path.stat()
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


def inventory(root):
    return {str(p.resolve()): signature(p) for p in sorted(root.rglob('rollout-*.jsonl'))}


def digest_prefix(path, length, return_hasher=False):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        while length:
            block = stream.read(min(length, 65536))
            if not block:
                raise ValueError('truncated prefix')
            length -= len(block)
            h.update(block)
    return h if return_hasher else h.hexdigest()


def record_digest(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def collect(root, target, cache, cursor_line=None):
    before = inventory(root)
    current = str(target.resolve())
    checkpoint = None
    try:
        checkpoint = json.loads(cache.read_text())
        checksum = checkpoint.pop('checksum')
        if record_digest(checkpoint) != checksum or checkpoint['version'] != 1:
            checkpoint = None
    except (OSError, ValueError, KeyError, TypeError):
        pass
    mode, rows, history = 'rebuild', [], None
    if checkpoint and checkpoint['target'] == current and cursor_line is not None:
        changed = {p for p in set(before) | set(checkpoint['inventory'])
                   if before.get(p) != checkpoint['inventory'].get(p)}
        if not changed:
            if cursor_line > checkpoint['line']:
                return {'mode': 'cached', 'complete': False, 'reason': 'cursor_requires_reconciliation', 'index_written': False}
            pending = []
            if cursor_line < checkpoint['line']:
                pending, errors = codex_history.read_segment(target)
                if errors or before != inventory(root):
                    return {'mode': 'cached', 'complete': False, 'reason': 'source_changed_during_read', 'index_written': False}
                pending = [r for r in pending if r['line'] > cursor_line]
            return {'mode': 'quiet' if not pending else 'cached', 'complete': True,
                    'delta': pending, 'end_line': checkpoint['line'], 'index_written': False}

        old = checkpoint['inventory'].get(current)
        new = before.get(current)
        if changed == {current} and old and new and old[:2] == new[:2] and new[2] > old[2]:
            prefix_hasher = digest_prefix(target, checkpoint['end'], return_hasher=True)
            if prefix_hasher.hexdigest() == checkpoint['prefix_hash']:
                mode = 'append'
                offset, line, ordinal = checkpoint['end'], checkpoint['line'], checkpoint['ordinal']
                with target.open('rb') as stream:
                    stream.seek(offset)
                    while True:
                        raw = stream.readline()
                        if not raw:
                            break
                        if not raw.endswith(b'\n'):
                            return {'mode': 'append', 'complete': False, 'reason': 'unfinished_record', 'index_written': False}
                        try:
                            record = json.loads(raw)
                            if (not isinstance(record, dict) or type(record.get('ordinal')) is not int or
                                    record['ordinal'] != ordinal + 1):
                                raise ValueError('ordinal gap')
                            if record.get('type') == 'session_meta':
                                raise ValueError('unexpected metadata')
                            if record.get('type') in {'response_item', 'event_msg'} and not isinstance(record.get('payload'), dict):
                                raise ValueError('invalid payload')
                        except (ValueError, UnicodeError):
                            return {'mode': 'append', 'complete': False, 'reason': 'invalid_record', 'index_written': False}
                        prefix_hasher.update(raw)
                        line += 1
                        ordinal += 1
                        rows.append({'path': current, 'line': line, 'start': offset, 'end': offset + len(raw), 'record': record})
                        offset += len(raw)
    if mode == 'rebuild':
        history = codex_history.resolve(target)
        if not history['complete']:
            return {'mode': mode, 'complete': False, 'issues': history['issues'], 'index_written': False}
        rows = history['records']
    if before != inventory(root):
        return {'mode': mode, 'complete': False, 'reason': 'source_changed_during_read', 'index_written': False}
    own = [r for r in rows if r['path'] == current]
    last = own[-1]
    value = {'version': 1, 'target': current, 'inventory': before,
             'end': last['end'], 'line': last['line'], 'ordinal': last['record']['ordinal'],
             'prefix_hash': prefix_hasher.hexdigest() if mode == 'append' else digest_prefix(target, last['end'])}
    value['checksum'] = record_digest(value)
    # Recheck after hashing as well; do not bless a mixed source snapshot.
    if before != inventory(root):
        return {'mode': mode, 'complete': False, 'reason': 'source_changed_during_read', 'index_written': False}
    temporary = cache.with_suffix('.tmp')
    temporary.write_text(json.dumps(value))
    temporary.replace(cache)
    if mode == 'append' and cursor_line < checkpoint['line']:
        rows, errors = codex_history.read_segment(target)
        if errors or before != inventory(root):
            return {'mode': mode, 'complete': False, 'reason': 'source_changed_during_read', 'index_written': True}
        rows = [r for r in rows if r['line'] > cursor_line]
    return {'mode': mode, 'complete': True, 'delta': rows, 'end_line': last['line'], 'index_written': True}



class ReadCounter:
    def __init__(self, root):
        self.root = root
        self.bytes = 0
        self.original = builtins.open
    def open(self, path, *args, **kwargs):
        stream = self.original(path, *args, **kwargs)
        if not isinstance(path, (str, Path)) or not str(path).startswith(str(self.root)) or not str(path).endswith('.jsonl'):
            return stream
        counter = self
        class Reader:
            def __enter__(self): return self
            def __exit__(self, *exc): return stream.__exit__(*exc)
            def __getattr__(self, key): return getattr(stream, key)
            def __iter__(self): return self
            def __next__(self):
                value = self.readline()
                if not value: raise StopIteration
                return value
            def count(self, value):
                counter.bytes += len(value.encode() if isinstance(value, str) else value)
                return value
            def read(self, *a): return self.count(stream.read(*a))
            def readline(self, *a): return self.count(stream.readline(*a))
        return Reader()


def worker(root, target, cache, cursor_line=None):
    temporary_root = Path(tempfile.gettempdir()).resolve()
    if (not root.is_relative_to(temporary_root) or
            not root.name.startswith('logbook-index-experiment-') or
            cache.parent != root or not target.is_relative_to(root)):
        raise ValueError('prototype state and fixtures must stay in its isolated temporary directory')
    codex.CODEX_ROOT = root
    codex.CODEX_ARCHIVED_ROOT = root / 'archive'
    counter = ReadCounter(root)
    start = time.perf_counter()
    with patch('builtins.open', counter.open), patch('io.open', counter.open):
        result = collect(root, target, cache, cursor_line)
    rows = result.pop('delta', [])
    result.update(bytes_read=counter.bytes, seconds=round(time.perf_counter() - start, 4),
                  row_count=len(rows), row_digest=record_digest(rows),
                  index_bytes=cache.stat().st_size if cache.exists() else 0)
    print(json.dumps(result))

if __name__ == '__main__':
    worker(*(Path(p).resolve() for p in sys.argv[1:4]),
           cursor_line=int(sys.argv[4]) if len(sys.argv) > 4 else None)
