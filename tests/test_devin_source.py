"""Synthetic Devin Local SQLite coverage; never uses private session fixtures."""
from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import server
import session_logbook_cli as cli
from sources import devin


class DevinSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.patch = mock.patch.object(devin, 'DEVIN_ROOT', self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        db = devin.database_path()
        db.parent.mkdir(parents=True)
        self.conn = sqlite3.connect(db)
        self.addCleanup(self.conn.close)
        self.conn.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE sessions (id TEXT PRIMARY KEY, working_directory TEXT,
              model TEXT, title TEXT, created_at INTEGER, last_activity_at INTEGER,
              main_chain_id INTEGER, hidden INTEGER);
            CREATE TABLE message_nodes (row_id INTEGER PRIMARY KEY, session_id TEXT,
              node_id INTEGER, parent_node_id INTEGER, chat_message TEXT, created_at INTEGER);
            INSERT INTO sessions VALUES ('alpha', '/Users/alice/my-app', 'example-model',
              'Synthetic review', 1700000000, 1700000010, 50, 0);
            INSERT INTO sessions VALUES ('hidden', '/Users/alice/my-app', '', '', 1, 1, NULL, 1);
        ''')
        self.node(1, 10, None, {'role': 'system', 'content': 'injected-only'})
        self.node(2, 20, 10, {'role': 'user', 'content': 'review payment',
                              'metadata': {'is_user_input': True}})
        self.node(3, 30, 20, {'role': 'assistant', 'content': 'abandoned branch'})
        self.node(4, 40, 20, {'role': 'assistant', 'content': 'selected answer',
            'thinking': {'thinking': 'not public'},
            'tool_calls': [{'id': 'call-a', 'name': 'read_file', 'arguments': {'path': 'app.py'}}]})
        self.node(5, 50, 40, {'role': 'tool', 'tool_call_id': 'call-a', 'content': 'failure details',
            'metadata': {'extensions': {'chisel/tool_result_meta': {'success': False}}}})
        self.conn.commit()
        self.ref = devin.reference('alpha')

    def node(self, row_id, node_id, parent, msg, session='alpha'):
        self.conn.execute('INSERT INTO message_nodes VALUES (?,?,?,?,?,?)',
                          (row_id, session, node_id, parent, json.dumps(msg), 1700000000+row_id))

    def test_reads_selected_chain_and_wal_without_writing(self):
        before = {p.name: p.read_bytes() for p in self.root.rglob('*')
                  if p.is_file() and not p.name.endswith('-shm')}
        self.assertEqual(devin.scan_sessions(), [self.ref])
        meta, chain = devin.read_session(self.ref)
        self.assertEqual([n['row_id'] for n in chain], [1, 2, 4, 5])
        self.assertEqual(devin.extract_metadata(self.ref)['user_turn_count'], 1)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.rglob('*')
                                 if p.is_file() and not p.name.endswith('-shm')})
        with closing(devin._connect(devin.database_path())) as c:
            with self.assertRaises(sqlite3.OperationalError):
                c.execute("UPDATE sessions SET title='changed'")

    def test_conversation_tools_and_context(self):
        conv = devin.extract_conversation(self.ref)
        tool = next(t for t in conv['turns'] if t['type'] == 'tool')
        self.assertEqual(tool['name'], 'read_file')
        self.assertTrue(tool['is_error'])
        self.assertEqual(tool['result_node_id'], 5)
        ctx = cli.render_context(self.ref)
        self.assertIn('[U1] [N2]', ctx)
        self.assertIn('failure details', ctx)
        self.assertNotIn('abandoned branch', ctx)
        self.assertNotIn('not public', ctx)
        self.assertNotIn('injected-only', ctx)
        self.assertIn('NEXT_CURSOR: N5', ctx)

    def test_follow_refreshes_earlier_branch_and_evidence_keeps_old_nodes(self):
        self.conn.execute('UPDATE sessions SET main_chain_id=30 WHERE id=?', ('alpha',))
        self.conn.commit()
        text = cli.render_context(self.ref, after_line=5)
        self.assertIn('abandoned branch', text)
        self.assertNotIn('selected answer', text)
        self.assertIn('FOLLOW_MODE: full-snapshot', text)
        self.assertIn('selected answer', cli.read_evidence(self.ref, 4, context=0))
        self.node(99, 1, None, {'role': 'user', 'content': 'other session'}, session='hidden')
        self.conn.commit()
        with self.assertRaises(cli.SessionLookupError):
            cli.read_evidence(self.ref, 99, context=0)

    def test_broken_chains_fail_instead_of_combining_branches(self):
        for leaf in (999, None):
            self.conn.execute('UPDATE sessions SET main_chain_id=? WHERE id=?', (leaf, 'alpha'))
            self.conn.commit()
            with self.assertRaises(ValueError):
                devin.read_session(self.ref)
        self.conn.execute('UPDATE sessions SET main_chain_id=50 WHERE id=?', ('alpha',))
        self.conn.execute('UPDATE message_nodes SET parent_node_id=50 WHERE node_id=10')
        self.conn.commit()
        with self.assertRaises(ValueError):
            devin.read_session(self.ref)

    def test_cli_locate_search_role_filters_and_status(self):
        with mock.patch.object(server, '_cache', {}):
            self.assertEqual(cli.resolve_target('devin:alpha'), self.ref.resolve())
        self.assertEqual(cli.resolve_target(str(self.ref)), self.ref.resolve())
        self.assertEqual(cli.status_for(self.ref)['next_cursor'], 'N5')
        found = cli.search_sessions('payment selected', source='devin')
        self.assertEqual([x['id'] for x in found], ['devin:alpha'])
        self.assertEqual(found[0]['snippets'][0]['anchor_kind'], 'N')
        self.assertEqual(cli.search_sessions('selected', source='devin', role='user'), [])
        self.assertEqual(cli.search_sessions('abandoned', source='devin'), [])
        self.assertEqual(cli.search_sessions('injected-only', source='devin'), [])
        self.assertEqual(cli.search_sessions('failure', source='devin'), [])

    def test_server_search_skips_file_prefilter_for_database(self):
        meta = devin.extract_metadata(self.ref)
        with mock.patch.object(server, '_cache', {str(self.ref): meta}), \
             mock.patch.object(server, '_rg_prefilter', return_value=set()):
            self.assertEqual(server.search_sessions('selected')[0]['id'], 'devin:alpha')
            self.assertEqual(server.search_sessions('abandoned'), [])
            self.assertEqual(server._find_jsonl('devin:alpha'), self.ref)

    def test_scan_cache_refresh_and_hidden_removal(self):
        with mock.patch.object(server, 'PROJECTS_DIR', self.root/'missing'), \
             mock.patch.object(server.codex_source, 'scan_sessions', return_value=[]), \
             mock.patch.object(server.ag_source, 'scan_sessions', return_value=[]), \
             mock.patch.object(server, '_cache', {}), \
             mock.patch.object(server, 'save_scan_cache'):
            first = server.scan_sessions()
            self.assertEqual(len(first), 1)
            self.conn.execute("UPDATE sessions SET title='Updated title' WHERE id='alpha'")
            self.conn.commit()
            self.assertEqual(server.scan_sessions()[0]['custom_title'], 'Updated title')
            self.conn.execute("UPDATE sessions SET hidden=1 WHERE id='alpha'")
            self.conn.commit()
            self.assertEqual(server.scan_sessions(), [])

    def test_http_encoded_ids_and_organizing_state(self):
        handler = server.Handler.__new__(server.Handler)
        handler._request_is_trusted = lambda: True
        handler._send_json = lambda status, value: (status, value)
        handler._send_bytes = lambda status, value, *args, **kw: (status, value)
        with mock.patch.object(server, '_cache', {str(self.ref): devin.extract_metadata(self.ref)}), \
             mock.patch.object(server, 'load_state'), \
             mock.patch.object(server, '_state_loaded', True), \
             mock.patch.object(server, '_state', {}), \
             mock.patch.object(server, 'save_state'):
            for suffix in ('conversation', 'transcript', 'anchored'):
                handler.path = '/api/sessions/devin%3Aalpha/' + suffix
                status, data = handler.do_GET()
                self.assertEqual(status, 200, data)
                if suffix == 'conversation':
                    self.assertEqual(data['source'], 'devin')
                else:
                    self.assertIn('[N2]', data)
            handler.path = '/api/sessions/devin%3Aalpha/star'
            handler._read_json = lambda: {'starred': True}
            self.assertEqual(handler.do_POST()[0], 200)
            self.assertTrue(server._state['devin:alpha']['starred'])
            self.assertNotIn('devin%3Aalpha', server._state)

    def test_brief_cache_tracks_changed_text_without_external_model(self):
        with mock.patch.object(server, '_state', {}), \
             mock.patch.object(server, 'save_state'), \
             mock.patch.object(server, 'generate_briefing', return_value='Synthetic brief') as generate:
            self.assertEqual(server.get_or_generate_brief('devin:alpha', self.ref, 'first')[1], 'generated')
            self.assertEqual(server.get_or_generate_brief('devin:alpha', self.ref, 'first')[1], 'cached')
            self.assertEqual(server.get_or_generate_brief('devin:alpha', self.ref, 'other')[1], 'regenerated')
            self.assertEqual(generate.call_count, 2)

    def test_missing_database_does_not_create_it(self):
        with mock.patch.object(devin, 'DEVIN_ROOT', self.root/'absent'):
            self.assertEqual(devin.scan_sessions(), [])
            self.assertFalse(devin.database_path().exists())


if __name__ == '__main__':
    unittest.main()
