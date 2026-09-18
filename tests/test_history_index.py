"""Production index acceptance with isolated sources, state, and real CLI calls."""
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sources import codex, codex_history, history_index
import session_logbook_cli as cli
import test_codex_history as fixtures
msg, event, SID = fixtures.msg, fixtures.event, fixtures.SID


class IndexTests(fixtures.HistoryTests):
    # Inherit the common source fixture helper; its history cases also run with the index.
    def setUp(self):
        super().setUp()
        self.database = self.root / 'index.sqlite3'
        self.stack.enter_context(patch.dict(os.environ, {'SESSION_LOGBOOK_HISTORY_INDEX': str(self.database)}))

    def plan(self, target):
        return history_index.plan(target, codex_history.resolve)

    def test_append_quiet_and_undelivered_result_use_caller_cursor(self):
        old, middle, latest = self.chain()
        before = self.plan(latest)
        self.assertEqual(before['cache'], 'rebuilt')
        with patch.object(codex_history, 'resolve', side_effect=AssertionError('unexpected rebuild')):
            self.assertEqual(self.plan(latest)['cache'], 'hit')
        records = codex_history.read_segment(latest)[0]
        ordinal = records[-1]['record']['ordinal'] + 1
        with latest.open('a') as stream:
            stream.write(json.dumps(dict(event('task_complete', 't2'), ordinal=ordinal)) + '\n')
        with patch.object(codex_history, 'resolve', side_effect=AssertionError('unexpected rebuild')):
            self.assertEqual(self.plan(latest)['cache'], 'append')
        options = ('observe', SID, '--cursor-source-path', str(latest), '--cursor-line', '3', '--native-line-cursor', '3')
        for _ in range(2):
            code, text, err = self.cli(*options)
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(text)['native']['events'][0]['facts']['type'], 'task_complete')
        code, text, err = self.cli('observe', SID, '--cursor-source-path', str(latest), '--cursor-line', '4', '--native-line-cursor', '4')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(text)['native']['events'], [])

    def test_mutation_missing_partial_and_cache_corruption(self):
        old, middle, latest = self.chain()
        self.plan(latest)
        st = old.stat()
        old.write_bytes(old.read_bytes().replace(b'Old unread tail', b'New unread tail'))
        os.utime(old, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(self.plan(latest)['cache'], 'rebuilt')
        contents = middle.read_bytes()
        middle.unlink()
        self.assertFalse(self.plan(latest)['complete'])
        middle.write_bytes(contents)
        self.assertTrue(self.plan(latest)['complete'])
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE histories SET payload='broken'")
        self.assertEqual(self.plan(latest)['cache'], 'rebuilt')
        self.database.write_bytes(b'broken sqlite')
        fallback = self.plan(latest)
        self.assertTrue(fallback['complete'])
        self.assertEqual(fallback['cache'], 'unavailable')
        self.database.unlink()
        self.assertEqual(self.plan(latest)['cache'], 'rebuilt')
        with latest.open('a') as stream:
            stream.write('{"partial":')
        self.assertFalse(self.plan(latest)['complete'])

    def test_new_conflicting_candidate_invalidates_but_unrelated_append_does_not(self):
        old, middle, latest = self.chain()
        other = self.segment('unrelated', [msg('Unrelated')], sid='cccccccc-cccc-cccc-cccc-cccccccccccc')
        self.plan(latest)
        with other.open('a') as stream:
            stream.write(json.dumps(dict(msg('More unrelated'), ordinal=2)) + '\n')
        self.assertEqual(self.plan(latest)['cache'], 'hit')
        twin = self.root / 'rollout-twin.jsonl'
        twin.write_bytes(old.read_bytes().replace(b'Old unread tail', b'Bad unread tail'))
        result = self.plan(latest)
        self.assertFalse(result['complete'])
        self.assertEqual(result['issues'][0]['reason'], 'ambiguous_history_segment')

    def test_disabled_and_unavailable_fallback_keep_the_read_snapshot(self):
        for disabled in (True, False):
            old, middle, latest = self.chain()
            if not disabled:
                self.database.write_bytes(b'broken database')
            with patch.dict(os.environ, {'SESSION_LOGBOOK_HISTORY_INDEX': 'off' if disabled else str(self.database)}):
                plan = self.plan(latest)
                latest.write_bytes(latest.read_bytes().replace(b'Latest request', b'Altered demand'))
                rows = history_index.window(plan['segments'][-1])
                self.assertIn('Latest request', json.dumps(rows))
                self.assertNotIn('Altered demand', json.dumps(rows))

    def test_window_seeks_to_requested_evidence_and_detects_snapshot_change(self):
        old = self.segment('large', [msg('x' * 1000000), msg('Last')])
        plan = self.plan(old)
        rows = history_index.window(plan['segments'][0], 3)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['line'], 3)
        old.write_bytes(old.read_bytes().replace(b'Last', b'Edit'))
        with self.assertRaisesRegex(ValueError, 'changed'):
            history_index.window(plan['segments'][0], 3)

    def test_fresh_processes_and_concurrent_readers_share_index_not_cursors(self):
        old, middle, latest = self.chain()
        root = str(self.root)
        script = """import json,sys
from pathlib import Path
from sources import codex,codex_history,history_index
codex.CODEX_ROOT=Path(sys.argv[1]);codex.CODEX_ARCHIVED_ROOT=codex.CODEX_ROOT/'archive'
p=history_index.plan(Path(sys.argv[2]),codex_history.resolve)
assert p['complete'], p['issues']
print(json.dumps({'cache':p.get('cache'), 'lines':[s['coordinates'][-1][0] for s in p['segments']]}))
"""
        commands = [sys.executable, '-c', script, root, str(latest)]
        first = subprocess.run(commands, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(first.stdout)['cache'], 'rebuilt')
        processes = [subprocess.Popen(commands, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(3)]
        for process in processes:
            output, error = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, error)
            self.assertEqual(json.loads(output)['lines'], [4, 3, 3])
        with sqlite3.connect(self.database) as db:
            payload = db.execute('SELECT payload FROM histories').fetchone()[0]
        self.assertNotIn('Old unread tail', payload)
        self.assertNotIn('cursor', payload)
