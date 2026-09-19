"""The reader, preview, and follow agree on selected Claude history."""
import json
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

    def test_follow_reconciles_when_branch_becomes_unverified(self):
        with self.path.open('a') as stream:
            stream.write(json.dumps(self.message('broken', 'missing', 'Partial history')) + '\n')
        with patch.object(cli, 'session_metadata', return_value={
                'id': SID, 'source': 'claude', 'project_path': '/Users/alice/my-app'}):
            text = cli.render_context(self.path, after_line=4)
        self.assertIn('full saved history', text)
        self.assertIn('Start', text)
        self.assertIn('Abandoned answer', text)
