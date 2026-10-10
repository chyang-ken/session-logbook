"""The session list must survive concurrent requests and files that change while read.

Both used to close the /api/sessions connection with no response: two scans racing to
delete the same vanished cache key raised KeyError, and one transcript an agent was
writing at that moment raised ValueError out of the whole scan. The page then saw a
dropped connection and showed an empty list.
"""
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import server


class ScanRobustnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.projects = Path(self.tmp.name) / 'projects'
        project = self.projects / '-Users-alice-my-app'
        project.mkdir(parents=True)
        self.good = project / 'aaaaaaaa-0000-0000-0000-000000000001.jsonl'
        self.busy = project / 'aaaaaaaa-0000-0000-0000-000000000002.jsonl'
        for path in (self.good, self.busy):
            path.write_text('{}\n')
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(server, 'PROJECTS_DIR', self.projects))
        for source in (server.codex_source, server.ag_source, server.kimi_source,
                       server.pi_source, server.devin_source):
            stack.enter_context(mock.patch.object(source, 'scan_sessions', return_value=[]))
        stack.enter_context(mock.patch.object(server, '_build_cwd_map'))
        stack.enter_context(mock.patch.object(server, '_cache', {}))
        stack.enter_context(mock.patch.object(server, 'save_scan_cache'))

    @staticmethod
    def meta(path):
        return {'id': path.stem, 'jsonl_path': str(path), 'mtime': path.stat().st_mtime,
                'project_path': '/Users/alice/my-app'}

    def test_a_file_that_cannot_be_read_now_does_not_fail_the_list(self):
        def extract(path):
            if path == self.busy:
                raise ValueError('Claude source changed during indexing; retry the read')
            return self.meta(path)

        with mock.patch.object(server, 'extract_metadata', side_effect=extract):
            items = server.scan_sessions()
        self.assertEqual([m['id'] for m in items], [self.good.stem])

        # Its earlier entry is kept, and the next scan reads it again.
        stale = dict(self.meta(self.busy), mtime=0)
        server._cache[str(self.busy)] = stale
        with mock.patch.object(server, 'extract_metadata', side_effect=extract):
            server.scan_sessions()
        self.assertIs(server._cache[str(self.busy)], stale)
        with mock.patch.object(server, 'extract_metadata', side_effect=self.meta):
            server.scan_sessions()
        self.assertEqual(server._cache[str(self.busy)]['mtime'], self.busy.stat().st_mtime)

    def test_scans_never_overlap(self):
        running, peak = 0, 0
        guard = threading.Lock()
        real = server._scan_sessions_locked

        def slow_scan(force):
            nonlocal running, peak
            with guard:
                running += 1
                peak = max(peak, running)
            time.sleep(0.05)
            try:
                return real(force)
            finally:
                with guard:
                    running -= 1

        errors = []

        def request():
            try:
                server.scan_sessions()
            except Exception as exc:  # the race surfaced as KeyError here
                errors.append(exc)

        # A file that vanished is the one both scans used to delete.
        server._cache[str(self.projects / '-Users-alice-my-app' / 'gone.jsonl')] = {'id': 'gone'}
        with mock.patch.object(server, '_scan_sessions_locked', side_effect=slow_scan), \
             mock.patch.object(server, 'extract_metadata', side_effect=self.meta):
            threads = [threading.Thread(target=request) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(errors, [])
        self.assertEqual(peak, 1)
        self.assertNotIn('gone', {m.get('id') for m in server._cache.values()})


class UnexpectedErrorAnswersTest(unittest.TestCase):
    def test_an_unexpected_error_answers_500_instead_of_closing_the_connection(self):
        handler = server.Handler.__new__(server.Handler)
        handler._send_json = lambda status, value: (status, value)
        handler._do_get = mock.Mock(side_effect=KeyError('/x.jsonl'))
        with mock.patch('traceback.print_exc'):
            status, body = handler.do_GET()
        self.assertEqual(status, 500)
        self.assertIn('KeyError', body['error'])

    def test_a_client_that_went_away_is_not_answered(self):
        handler = server.Handler.__new__(server.Handler)
        handler._send_json = mock.Mock()
        handler._do_post = mock.Mock(side_effect=BrokenPipeError())
        self.assertIsNone(handler.do_POST())
        handler._send_json.assert_not_called()


if __name__ == '__main__':
    unittest.main()
