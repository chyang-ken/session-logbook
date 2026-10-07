"""Delivered human interruptions are messages; queue bookkeeping is not."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server
import session_logbook_cli as cli
from sources import anchored_transcript, claude_history
from sources.claude_text import normalize_record

SID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
TEXT = 'Clarify the acceptance criteria before starting.'


def interruption():
    return {'type': 'attachment', 'uuid': 'delivered', 'parentUuid': 'start',
            'sessionId': SID, 'cwd': '/Users/alice/my-app',
            'timestamp': '2026-01-01T00:01:00Z',
            'attachment': {'type': 'queued_command', 'commandMode': 'prompt',
                           'origin': {'kind': 'human'}, 'prompt': TEXT,
                           'source_uuid': 'queued-source'},
            'rendered': [{'content': '<system-reminder>Transport-only wrapper</system-reminder>'}]}


class QueuedMessageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / (SID + '.jsonl')
        env = patch.dict('os.environ', {'SESSION_LOGBOOK_HISTORY_INDEX': str(self.path.parent / 'index.sqlite')})
        env.start()
        self.addCleanup(env.stop)
        self.rows = [
            {'type': 'user', 'uuid': 'start', 'parentUuid': None, 'sessionId': SID,
             'cwd': '/Users/alice/my-app', 'timestamp': '2026-01-01T00:00:00Z',
             'message': {'role': 'user', 'content': 'Start the work'}},
            {'type': 'queue-operation', 'operation': 'enqueue', 'content': TEXT},
            interruption(),
            {'type': 'queue-operation', 'operation': 'dequeue', 'content': TEXT},
        ]
        self.write()

    def write(self):
        self.path.write_text(''.join(json.dumps(row) + '\n' for row in self.rows))

    def test_reader_preview_exports_search_and_anchors_agree(self):
        original = self.path.read_bytes()
        conv = server.extract_conversation(self.path)
        users = [t for t in conv['turns'] if t['type'] == 'user']
        self.assertEqual([t['text'] for t in users], ['Start the work', TEXT])
        self.assertEqual(users[1]['ts'], '2026-01-01T00:01:00Z')
        self.assertEqual(conv['total_lines'], 4)
        meta = server.extract_metadata(self.path)
        self.assertEqual(meta['user_turn_count'], 2)
        self.assertIn(TEXT, [m['text'] for m in meta['recent_msgs']])
        self.assertEqual(server.extract_transcript(self.path).count(TEXT), 1)
        anchored = anchored_transcript.render_claude(self.path)
        self.assertEqual(anchored.count(TEXT), 1)
        self.assertRegex(anchored, r'\[L3\][^\n]*USER[^\n]*\n' + TEXT)
        self.assertIn(TEXT, json.dumps(server._search_session(self.path, ['acceptance criteria'])))
        self.assertEqual(cli._message_from_row(interruption(), 'claude'), ('user', TEXT))
        rows = list(claude_history.records(self.path, first=3))
        self.assertEqual(rows[0][1]['_logbook_user_turn'], 2)
        self.assertEqual(original, self.path.read_bytes())

    def test_interruption_can_be_first_user_message(self):
        self.rows = self.rows[1:]
        self.write()
        meta = server.extract_metadata(self.path)
        self.assertEqual(meta['user_turn_count'], 1)
        self.assertEqual(meta['recent_msgs'][0]['text'], TEXT)

    def test_ordinary_resend_is_not_deduplicated_by_text(self):
        self.rows.append({'type': 'user', 'uuid': 'resend', 'parentUuid': 'delivered',
                          'message': {'role': 'user', 'content': TEXT}})
        self.write()
        users = [t for t in server.extract_conversation(self.path)['turns'] if t['type'] == 'user']
        self.assertEqual(sum(t['text'] == TEXT for t in users), 2)

    def test_nonhuman_incomplete_and_nonprompt_attachments_stay_hidden(self):
        for field, value in [('origin', {'kind': 'agent'}), ('origin', None),
                             ('commandMode', 'bash'), ('type', 'file'),
                             ('prompt', ''), ('prompt', [{'text': TEXT}])]:
            row = interruption()
            row['attachment'][field] = value
            with self.subTest(field=field, value=value):
                self.assertEqual(normalize_record(row), row)
                self.assertEqual(cli._message_from_row(row, 'claude'), (None, ''))
        for attachment in (None, [], 'invalid'):
            row = dict(interruption(), attachment=attachment)
            self.assertEqual(normalize_record(row), row)
        row = dict(interruption(), isMeta=True)
        self.assertEqual(normalize_record(row), row)

    def test_normalization_preserves_original_row_and_identity(self):
        row = interruption()
        original = copy.deepcopy(row)
        normalized = normalize_record(row)
        self.assertEqual(row, original)
        for key in ('uuid', 'parentUuid', 'sessionId', 'timestamp'):
            self.assertEqual(normalized[key], row[key])
        self.assertEqual(normalize_record(normalized), normalized)
