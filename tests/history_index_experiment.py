"""Synthetic, fresh-process acceptance. All generated state is temporary."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sources import codex, codex_history

SID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
PROTOTYPE = Path(__file__).with_name('history_index_prototype.py')


def message(ordinal, text):
    return {'ordinal': ordinal, 'type': 'response_item', 'payload': {'type': 'message',
            'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}}


def digest(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def run(single_segment=False):
    report = {}
    with tempfile.TemporaryDirectory(prefix='logbook-index-experiment-') as directory:
        root = Path(directory).resolve()
        cache = root / 'test-index.json'
        codex.CODEX_ROOT = root
        codex.CODEX_ARCHIVED_ROOT = root / 'archive'
        previous, ordinal = None, 0
        for n in range(1 if single_segment else 100):
            meta = {'id': SID, 'cwd': '/synthetic'}
            if previous:
                meta['history_base'] = {'thread_id': SID, 'end_ordinal_exclusive': ordinal,
                                        'end_byte_offset': previous.stat().st_size}
            records = [{'ordinal': ordinal, 'type': 'session_meta', 'payload': meta}]
            ordinal += 1
            for m in range(16000 if single_segment else 160):
                records.append(message(ordinal, 'x' * 4096)); ordinal += 1
            previous = root / ('rollout-%04d-' % n + SID + '.jsonl')
            previous.write_text(''.join(json.dumps(row) + '\n' for row in records))
        target = previous
        report['source_bytes'] = sum(p.stat().st_size for p in root.glob('*.jsonl'))

        cursor = None

        def check(label, mode, complete=True, expected=None):
            nonlocal cursor
            process = subprocess.run([sys.executable, str(PROTOTYPE), str(root), str(target), str(cache)] +
                                     ([str(cursor)] if cursor is not None else []),
                                     capture_output=True, text=True, timeout=30, check=True)
            result = json.loads(process.stdout)
            assert result['mode'] == mode, (label, result)
            assert result['complete'] == complete, (label, result)
            if expected is not None:
                assert result['row_digest'] == digest(expected), (label, 'content mismatch')
            if result['complete']:
                cursor = result['end_line']
            report[label] = result
            return result

        def full(): return codex_history.resolve(target)['records']
        check('cold', 'rebuild', expected=full())
        initial = cache.read_bytes()
        check('quiet_new_process', 'quiet', expected=[])
        assert cache.read_bytes() == initial
        old_count = len(full())
        with target.open('a') as f: f.write(json.dumps(message(ordinal, 'Appended request')) + '\n')
        ordinal += 1
        saved_cursor = cursor
        appended = full()[old_count:]
        check('append_new_process', 'append', expected=appended)
        cursor = saved_cursor
        check('retry_after_undelivered_result', 'cached', expected=appended)
        if not single_segment:
            old = sorted(root.glob('rollout-*.jsonl'))[0]
            old_data = old.read_bytes()
            st = old.stat()
            old.write_bytes(old_data.replace(b'xxxxxxxx', b'yyyyyyyy', 1))
            os.utime(old, ns=(st.st_atime_ns, st.st_mtime_ns))
            check('same_size_ancestor_edit_preserved_mtime', 'rebuild', expected=full())
            replacement = root / 'replacement.tmp'
            replacement.write_bytes(target.read_bytes())
            replacement.replace(target)
            check('atomic_replacement', 'rebuild', expected=full())
            saved = old.read_bytes(); old.unlink()
            prior_cache = cache.read_bytes()
            check('missing_ancestor', 'rebuild', complete=False)
            assert cache.read_bytes() == prior_cache
            old.write_bytes(saved)
            check('restored_ancestor', 'rebuild', expected=full())
            cache.write_text('{broken')
            check('corrupt_index', 'rebuild', expected=full())
            cache.unlink()
            check('deleted_index', 'rebuild', expected=full())
            prior_cache = cache.read_bytes()
            original_size = target.stat().st_size
            with target.open('ab') as f: f.write(b'{"unfinished":')
            check('unfinished_append', 'append', complete=False)
            assert cache.read_bytes() == prior_cache
            with target.open('r+b') as f: f.truncate(original_size)
            # A valid append plus an old-prefix edit must rebuild, not trust file growth.
            data = target.read_bytes().replace(b'xxxxxxxx', b'zzzzzzzz', 1)
            target.write_bytes(data + json.dumps(message(ordinal, 'Another request')).encode() + b'\n')
            ordinal += 1
            check('rewrite_and_append', 'rebuild', expected=full())
            base = {'thread_id': SID, 'end_ordinal_exclusive': ordinal, 'end_byte_offset': target.stat().st_size}
            target = root / ('rollout-0100-' + SID + '.jsonl')
            rows = [{'ordinal': ordinal, 'type': 'session_meta', 'payload': {'id': SID, 'history_base': base}},
                    message(ordinal + 1, 'Next segment')]
            target.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            check('new_segment', 'rebuild', expected=full())
            check('quiet_after_new_segment', 'quiet', expected=[])
    report['temporary_state_removed'] = not root.exists()
    print(json.dumps(report, indent=2))

if __name__ == '__main__': run(single_segment='--single-segment' in sys.argv)
