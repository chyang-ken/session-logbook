"""The reader, preview, and follow agree on selected Claude history."""
import json
import io
import contextlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server
import session_logbook_cli as cli
from sources import claude_history

SID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'


class RewindIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.path = root / (SID + '.jsonl')
        rows = [
            self.message('start', None, 'Start'),
            self.message('old', 'start', 'Abandoned answer', 'assistant'),
            self.message('new', 'start', 'Current answer', 'assistant'),
            {'type': 'last-prompt', 'leafUuid': 'new', 'sessionId': SID},
        ]
        self.path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        env = patch.dict(os.environ, {'SESSION_LOGBOOK_HISTORY_INDEX': str(root / 'index.sqlite')})
        env.start()
        self.addCleanup(env.stop)

    def message(self, uuid, parent, text, role='user'):
        return {'type': role, 'uuid': uuid, 'parentUuid': parent, 'sessionId': SID,
                'cwd': '/Users/alice/my-app', 'timestamp': '2026-01-01T00:00:00Z',
                'message': {'role': role, 'content': text if role == 'user' else
                            [{'type': 'text', 'text': text}]}}

    def observe(self, cursor, delta=True):
        output = io.StringIO()
        with patch.object(cli, 'session_metadata', return_value={
                'id': SID, 'source': 'claude', 'project_path': '/Users/alice/my-app'}), \
             patch('sources.runtime_events.read', return_value={
                 'next_event_cursor': 9, 'events': [], 'has_more': False}), \
             contextlib.redirect_stdout(output):
            code = cli.main(['observe', str(self.path), '--cursor-line', str(cursor),
                             '--cursor-source-path', str(self.path)] + (['--delta'] if delta else []))
        self.assertEqual(code, 0)
        return json.loads(output.getvalue())

    def test_observe_delta_reports_removals_and_keeps_event_cursors(self):
        result = self.observe(3)
        self.assertEqual(result['conversation_follow_mode'], 'delta_selected_branch')
        self.assertFalse(result['conversation_reconciliation_required'])
        self.assertEqual(result['conversation_removed_lines'], [2])
        self.assertEqual(result['conversation_delta_from'], 3)
        self.assertNotIn('Start', result['conversation'])
        self.assertIn('Current answer', result['conversation'])
        self.assertEqual(result['hooks']['next_event_cursor'], 9)
        self.assertEqual(self.observe(3, delta=False)['conversation_follow_mode'], 'full_selected_branch')
        self.assertEqual(self.observe(0)['conversation_follow_mode'], 'full_selected_branch')

    def test_observe_unverified_branch_falls_back_to_full_context(self):
        with self.path.open('a') as stream:
            stream.write(json.dumps(self.message('broken', 'missing', 'Partial history')) + '\n')
        result = self.observe(4)
        self.assertEqual(result['conversation_follow_mode'], 'full_saved_history_unverified')
        self.assertTrue(result['conversation_reconciliation_required'])
        self.assertNotIn('conversation_removed_lines', result)
        self.assertIn('Start', result['conversation'])

    def test_observe_delta_rejects_a_concurrently_changed_file(self):
        render = cli.render_context
        def changed(*args, **kwargs):
            text = render(*args, **kwargs)
            with self.path.open('a') as stream:
                stream.write('\n')
            return text
        with patch.object(cli, 'render_context', side_effect=changed), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            for flags in ([], ['--delta']):
                self.assertEqual(cli.main(['observe', str(self.path), '--cursor-line', '3'] + flags), 1)

    def test_native_controls_exclude_rewound_interruptions_and_peer_messages(self):
        from sources import runtime_events
        rows = [self.message('start', None, 'Task'),
                self.message('aborted', 'start', '[Request interrupted by user]'),
                self.message('branch', 'start', 'Continue here'),
                dict(self.message('peer', 'branch', 'A peer cannot release the pause'),
                     origin={'kind': 'peer'}),
                self.message('stop', 'peer', '[Request interrupted by user for tool use]'),
                {'type': 'last-prompt', 'leafUuid': 'stop', 'sessionId': SID}]
        self.path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        first = runtime_events.native_events(self.path, 'claude', limit=2)
        self.assertEqual([e['facts']['type'] for e in first['events']], ['user_input', 'user_input'])
        self.assertTrue(first['has_more'])
        rest = runtime_events.native_events(self.path, 'claude', after=first['next_line_cursor'])
        self.assertEqual([e['line'] for e in rest['events']], [5])
        self.assertEqual(rest['events'][0]['facts']['type'], 'user_interrupted')
        self.assertNotIn('Task', json.dumps(first))
        self.assertEqual(runtime_events.native_events(self.path, 'claude', after=6)['events'], [])

    def test_native_control_keeps_quotes_as_input_and_waits_for_complete_records(self):
        from sources import runtime_events
        rows = [self.message('start', None, 'Quoted [Request interrupted by user] is text'),
                self.message('stop', 'start', '[Request interrupted by user]')]
        self.path.write_text('\n'.join(json.dumps(row) for row in rows))
        first = runtime_events.native_events(self.path, 'claude')
        self.assertEqual(first['next_line_cursor'], 1)
        self.assertEqual(first['events'][0]['facts']['type'], 'user_input')
        with self.path.open('a') as stream:
            stream.write('\n')
        rest = runtime_events.native_events(self.path, 'claude', after=1)
        self.assertEqual(rest['events'][0]['facts']['type'], 'user_interrupted')

    def test_native_controls_do_not_infer_an_unverified_branch(self):
        from sources import runtime_events
        with self.path.open('a') as stream:
            stream.write(json.dumps(self.message('broken', 'missing', '[Request interrupted by user]')) + '\n')
        result = runtime_events.native_events(self.path, 'claude', after=1)
        self.assertEqual(result['events'], [])
        self.assertEqual(result['next_line_cursor'], 1)
        self.assertEqual(result['collection'], 'selected_history_unverified')

    def test_current_and_historical_reader_are_distinct(self):
        current = server.extract_conversation(self.path)
        historical = server.extract_conversation(self.path, include_rewound=True)
        current_text = json.dumps(current['turns'])
        self.assertNotIn('Abandoned answer', current_text)
        self.assertIn('Current answer', current_text)
        self.assertIn('Abandoned answer', json.dumps(historical['turns']))
        self.assertFalse(current['include_rewound'])
        self.assertTrue(historical['include_rewound'])
        self.assertEqual(current['total_lines'], 4)

    def test_preview_and_export_exclude_abandoned_answer(self):
        preview = server.extract_metadata(self.path)
        self.assertNotIn('Abandoned answer', json.dumps(preview['recent_msgs']))
        self.assertIn('Current answer', json.dumps(preview['recent_msgs']))
        self.assertNotIn('Abandoned answer', server.extract_transcript(self.path))

    def test_follow_returns_full_branch_for_reconciliation(self):
        with patch.object(cli, 'session_metadata', return_value={
                'id': SID, 'source': 'claude', 'project_path': '/Users/alice/my-app'}):
            text = cli.render_context(self.path, after_line=3)
        self.assertIn('full selected branch', text)
        self.assertIn('Start', text)
        self.assertIn('Current answer', text)
        self.assertNotIn('Abandoned answer', text)
        self.assertIn('# NEXT_CURSOR: L4', text)

    def follow(self, after_line, delta=True):
        with patch.object(cli, 'session_metadata', return_value={
                'id': SID, 'source': 'claude', 'project_path': '/Users/alice/my-app'}):
            return cli.render_context(self.path, after_line=after_line, delta=delta)

    def test_delta_follow_returns_only_the_cursor_onward_and_names_removed_anchors(self):
        text = self.follow(3)
        self.assertIn('# FOLLOW_MODE: delta from cursor; selected branch verified', text)
        self.assertIn('# REMOVED_BEFORE_CURSOR: L2', text)
        self.assertIn('Current answer', text)
        self.assertNotIn('Start', text)
        self.assertNotIn('Abandoned answer', text)
        self.assertIn('# NEXT_CURSOR: L4', text)

    def test_delta_follow_reports_a_rewind_that_happens_after_the_cursor_was_taken(self):
        # The reader consumed L1-L4, including "Current answer" at L3. The user then rewinds
        # to the start and takes another branch; L3 must be named as removed.
        with self.path.open('a') as stream:
            stream.write(json.dumps(self.message('third', 'start', 'Replacement answer', 'assistant')) + '\n')
            stream.write(json.dumps({'type': 'last-prompt', 'leafUuid': 'third', 'sessionId': SID}) + '\n')
        text = self.follow(4)
        self.assertIn('# REMOVED_BEFORE_CURSOR: L2-L3', text)
        self.assertIn('Replacement answer', text)
        self.assertNotIn('Current answer', text)
        self.assertIn('# NEXT_CURSOR: L6', text)

    def test_delta_follow_without_rewind_reports_nothing_removed(self):
        self.path.write_text(''.join(json.dumps(row) + '\n' for row in [
            self.message('start', None, 'Start'),
            self.message('reply', 'start', 'First answer', 'assistant'),
            {'type': 'last-prompt', 'leafUuid': 'reply', 'sessionId': SID},
            self.message('next', 'reply', 'Follow-up question'),
        ]))
        text = self.follow(3)
        self.assertIn('# REMOVED_BEFORE_CURSOR: none', text)
        self.assertIn('Follow-up question', text)
        self.assertNotIn('First answer', text)

    def test_delta_follow_says_unknown_when_the_branch_is_unverified(self):
        with self.path.open('a') as stream:
            stream.write(json.dumps(self.message('broken', 'missing', 'Partial history')) + '\n')
        text = self.follow(4)
        self.assertIn('delta from cursor; selected branch unverified (missing_parent_record)', text)
        self.assertIn('# REMOVED_BEFORE_CURSOR: unknown', text)
        self.assertIn('Partial history', text)
        self.assertNotIn('Abandoned answer', text)

    def test_delta_is_opt_in_and_a_zero_cursor_still_returns_everything(self):
        self.assertIn('full selected branch', self.follow(3, delta=False))
        first = self.follow(0)
        self.assertNotIn('delta from cursor', first)
        self.assertIn('Start', first)

    def test_follow_reconciles_when_branch_becomes_unverified(self):
        with self.path.open('a') as stream:
            stream.write(json.dumps(self.message('broken', 'missing', 'Partial history')) + '\n')
        with patch.object(cli, 'session_metadata', return_value={
                'id': SID, 'source': 'claude', 'project_path': '/Users/alice/my-app'}):
            text = cli.render_context(self.path, after_line=4)
        self.assertIn('full saved history', text)
        self.assertIn('Start', text)
        self.assertIn('Abandoned answer', text)
