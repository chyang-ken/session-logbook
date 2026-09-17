"""Synthetic same-ID resume regressions across CLI, native events, and HTTP lookup."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import urllib.request
import urllib.parse
from http.server import ThreadingHTTPServer
import unittest
from unittest.mock import patch

import server
import session_logbook_cli as cli
from sources import codex, runtime_events

SID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
CHILD = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'


def message(text):
    return {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
            'content': [{'type': 'input_text', 'text': text}]}}


def event(kind, turn):
    return {'type': 'event_msg', 'payload': {'type': kind, 'turn_id': turn}}


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.active = self.root / 'sessions'
        self.archived = self.root / 'archived_sessions'
        self.active.mkdir()
        for name, value in [('CODEX_ROOT', self.active), ('CODEX_ARCHIVED_ROOT', self.archived),
                            ('SESSION_INDEX_PATH', self.root / 'index.jsonl')]:
            self.stack.enter_context(patch.object(codex, name, value))
        self.stack.enter_context(patch.object(server, '_cache', {}))
        self.stack.enter_context(patch.dict(os.environ, {'SESSION_LOGBOOK_EVENTS': str(self.root / 'absent.db')}))
        self.old = self.write('old', SID, '2026-01-01T00:00:00Z', [
            message('old request'), event('task_complete', 'old-turn'),
            *[{'type': 'unused', 'payload': {'text': 'padding' * 50}} for _ in range(15)]])
        self.new = self.write('new', SID, '2026-01-02T00:00:00Z', [
            event('task_started', 'new-turn'), message('new request')], continued=True)
        # Old file is both larger and has a later mtime. Neither identifies the source.
        os.utime(self.old, (2000000000, 2000000000))
        os.utime(self.new, (1000000000, 1000000000))

    def write(self, label, sid, stamp, rows, continued=False, child=False):
        path = self.active / f'rollout-{label}-{sid}.jsonl'
        meta = {'id': sid, 'timestamp': stamp, 'cwd': '/Users/alice/my-app'}
        if continued:
            meta['history_base'] = {'thread_id': sid, 'end_ordinal_exclusive': 12}
        if child:
            meta['parent_thread_id'] = SID
            meta['source'] = {'subagent': {'thread_spawn': {'parent_thread_id': SID}}}
        records = [{'type': 'session_meta', 'payload': meta}, *rows]
        path.write_text(''.join(json.dumps(row) + '\n' for row in records))
        return path

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(list(args)), 0)
        return out.getvalue()

    def test_locate_context_follow_observe_share_current_source(self):
        server._cache[str(self.old)] = codex.extract_metadata(self.old)
        self.assertEqual(cli.resolve_target(SID), self.new.resolve())
        self.assertEqual(json.loads(self.run_cli('locate', SID))['jsonl_path'], str(self.new.resolve()))
        self.assertIn('new request', self.run_cli('context', SID))
        # An old cursor can exceed the new file length; migrate rather than stall.
        followed = self.run_cli('follow', SID, '--cursor-line', '18')
        self.assertIn('new request', followed)
        self.assertNotIn('old request', followed)
        self.assertIn('SOURCE_CHANGED: true', followed)
        observed = json.loads(self.run_cli('observe', SID, '--native-line-cursor', '18', '--cursor-line', '18'))
        self.assertEqual(observed['native']['events'][0]['facts']['turn_id'], 'new-turn')
        self.assertIn('new request', observed['conversation'])
        self.assertIn('HISTORICAL_TERMINAL_NOT_CURRENT_STATE: unknown', observed['conversation'])

    def test_qualified_cursor_switch_and_subsequent_increment(self):
        old_cursor = codex.next_cursor(self.old, 18)
        first = runtime_events.native_events(self.new, 'codex', old_cursor)
        self.assertTrue(first['source_changed'])
        self.assertFalse(first['has_more'])
        second = runtime_events.native_events(self.new, 'codex', first['next_line_cursor'])
        self.assertFalse(second['source_changed'])
        self.assertEqual(second['events'], [])
        with self.new.open('a') as stream:
            stream.write(json.dumps(event('task_complete', 'new-turn')) + '\n')
        third = runtime_events.native_events(self.new, 'codex', second['next_line_cursor'])
        self.assertEqual([e['facts']['type'] for e in third['events']], ['task_complete'])
        token = codex.next_cursor(self.new, 3)
        with self.new.open('a') as stream:
            stream.write(json.dumps(message('later request')) + '\n')
        body = cli.render_context(self.new, token)
        self.assertIn('later request', body)
        self.assertNotIn('old request', body)
        self.assertIn('REPEATED_CURSOR_LINE: L3', body)

    def test_longer_replacement_and_native_pagination(self):
        with self.new.open('a') as stream:
            for n in range(25):
                stream.write(json.dumps(event('task_complete', f'turn-{n}')) + '\n')
        # Even when line 18 exists in the new file, it belongs to the old source.
        first = runtime_events.native_events(self.new, 'codex', 18, limit=1)
        self.assertEqual(first['events'][0]['facts']['turn_id'], 'new-turn')
        self.assertTrue(first['has_more'])
        rest = runtime_events.native_events(self.new, 'codex', first['next_line_cursor'])
        self.assertEqual(len(rest['events']), 25)
        self.assertFalse(rest['has_more'])

    def test_ui_cache_and_search_choose_same_segment(self):
        old, new = codex.extract_metadata(self.old), codex.extract_metadata(self.new)
        self.assertEqual(server._dedup_by_id([old, new]), [new])
        server._cache[str(self.old)] = old
        self.assertEqual(server._find_jsonl(SID), self.new)
        conversation = codex.extract_conversation(server._find_jsonl(SID))
        self.assertIn('new request', json.dumps(conversation))
        self.assertNotIn('old request', json.dumps(conversation))
        with patch.object(cli, 'iter_session_paths', return_value=iter([self.old, self.new])):
            found = cli.search_sessions('request', source='codex')
        self.assertEqual(found[0]['jsonl_path'], str(self.new.resolve()))
        # Equal size/mtime must still invalidate HTTP polling after a source switch.
        twin = self.active / 'rollout-twin.jsonl'
        twin.write_bytes(self.new.read_bytes())
        os.utime(twin, ns=(self.new.stat().st_atime_ns, self.new.stat().st_mtime_ns))
        self.assertNotEqual(server.file_fingerprint(twin), server.file_fingerprint(self.new))

    def test_http_conversation_and_anchored_use_new_source_with_warm_cache(self):
        server._cache[str(self.old)] = codex.extract_metadata(self.old)
        self.stack.enter_context(patch.object(server, '_state', {}))
        self.stack.enter_context(patch.object(server, '_state_loaded', True))
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.stack.enter_context(patch.object(server, 'PORT', httpd.server_port))
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        try:
            base = f'http://127.0.0.1:{httpd.server_port}/api/sessions/{SID}'
            old_fingerprint = urllib.parse.quote(server.file_fingerprint(self.old), safe='')
            with urllib.request.urlopen(base + '/conversation?fingerprint=' + old_fingerprint) as response:
                result = json.load(response)
            self.assertNotIn('unchanged', result)
            self.assertIn('new request', json.dumps(result))
            self.assertNotIn('old request', json.dumps(result))
            with urllib.request.urlopen(base + '/anchored') as response:
                body = response.read().decode()
            self.assertIn('new request', body)
            self.assertIn(str(self.new), body)
            self.assertNotIn('old request', body)
        finally:
            httpd.shutdown()
            httpd.server_close()
            worker.join()

    def test_child_is_not_a_continuation_and_wrong_cursor_is_rejected(self):
        child = self.write('child', CHILD, '2026-01-03T00:00:00Z',
                           [event('task_complete', 'child-turn')], child=True)
        self.assertEqual(codex.find_rollout_by_session_id(SID)[0], self.new)
        self.assertNotIn(child, list(codex.scan_sessions()))
        with self.assertRaises(ValueError):
            runtime_events.native_events(self.new, 'codex', codex.next_cursor(child, 2))
        self.old.unlink()
        self.new.unlink()
        self.assertEqual(codex.find_rollout_by_session_id(SID), (None, None))

    def test_truncation_is_explicit_and_partial_line_is_retried(self):
        with self.assertRaises(ValueError):
            runtime_events.native_events(self.new, 'codex', codex.next_cursor(self.new, 99))
        with self.new.open('a') as stream:
            stream.write(json.dumps(event('task_complete', 'new-turn')))
        page = runtime_events.native_events(self.new, 'codex')
        self.assertEqual(codex.resume_cursor(self.new, page['next_line_cursor']), (3, False))
        with self.new.open('a') as stream:
            stream.write('\n')
        page = runtime_events.native_events(self.new, 'codex', page['next_line_cursor'])
        self.assertEqual(page['events'][0]['facts']['type'], 'task_complete')

    def test_start_clears_previous_terminal_in_same_segment(self):
        with self.new.open('a') as stream:
            stream.write(json.dumps(event('task_complete', 'new-turn')) + '\n')
            stream.write(json.dumps(event('task_started', 'next-turn')) + '\n')
        self.assertIsNone(codex.extract_metadata(self.new)['last_stop_reason'])
