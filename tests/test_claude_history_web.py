"""Web retrieval keeps Claude physical sessions and shared-history evidence separate."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server


SID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
OTHER = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'


class ClaudeHistoryWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / (SID + '.jsonl')
        self.other = Path(self.tmp.name) / (OTHER + '.jsonl')
        shared = {'type': 'user', 'uuid': 'cccccccc-cccc-cccc-cccc-cccccccccccc',
                  'message': {'role': 'user', 'content': 'Shared request'}}
        self.path.write_text(json.dumps(shared) + '\n' + json.dumps({
            'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Selected reply'}]}
        }) + '\n')
        self.other.write_text(json.dumps(shared) + '\n' + json.dumps({
            'type': 'user', 'message': {'content': 'Other branch only'}
        }) + '\n')
        self.history = {'history_semantics': 'selected_file_only',
                        'continuation_status': 'unconfirmed', 'compactions': [],
                        'source_files': [
                            {'relation': 'selected', 'session_id': SID,
                             'path': str(self.path), 'first_line': 1, 'last_line': 2},
                            {'relation': 'shared_history', 'session_id': OTHER,
                             'path': str(self.other), 'first_line': 1, 'last_line': 1,
                             'shared_records': 1, 'relationship_kind': 'undetermined'}]}
        self.describe = patch.object(server.claude_history, 'describe', return_value=self.history)
        self.describe.start()
        self.addCleanup(self.describe.stop)

    def test_conversation_keeps_selected_id_path_and_body(self):
        result = server.extract_conversation(self.path)
        self.assertEqual(result['id'], SID)
        self.assertEqual(result['jsonl_path'], str(self.path))
        self.assertEqual(result['source'], 'claude')
        self.assertEqual(result['source_files'], self.history['source_files'])
        self.assertEqual(result['history_semantics'], 'selected_file_only')
        text = '\n'.join(turn.get('text', '') for turn in result['turns'])
        self.assertEqual(text.count('Shared request'), 1)
        self.assertIn('Selected reply', text)
        self.assertNotIn('Other branch only', text)

    def test_export_preserves_source_references_without_merging(self):
        text = server.extract_transcript(self.path)
        self.assertTrue(text.startswith('# Session ' + SID + '\n'))
        self.assertIn(str(self.other), text)
        self.assertIn('Session ' + OTHER, text)
        self.assertIn('does not establish continuation or fork', text)
        self.assertIn('Other source branches are not merged', text)
        self.assertEqual(text.count('Shared request'), 1)
        self.assertNotIn('Other branch only', text)

    def test_search_retains_physical_session_boundaries(self):
        self.assertTrue(server._search_session(self.path, ['shared']))
        self.assertTrue(server._search_session(self.other, ['shared']))
        self.assertFalse(server._search_session(self.path, ['other branch']))
        self.assertTrue(server._search_session(self.other, ['other branch']))

    def test_duplicate_uuid_is_rendered_once_but_physical_line_count_survives(self):
        with self.path.open('a') as stream:
            stream.write(self.path.read_text().splitlines()[0] + '\n')
        result = server.extract_conversation(self.path)
        self.assertEqual(result['total_lines'], 3)
        self.assertEqual(sum(t.get('text') == 'Shared request' for t in result['turns']), 1)
        self.assertEqual(server.extract_transcript(self.path).count('Shared request'), 1)

    def test_claude_http_fingerprint_includes_history_revision(self):
        with patch.object(server, 'PROJECTS_DIR', Path(self.tmp.name)):
            self.history['history_fingerprint'] = 'first-revision'
            self.assertEqual(server.file_fingerprint(self.path), 'first-revision')
            self.history['history_fingerprint'] = 'changed-evidence'
            self.assertEqual(server.file_fingerprint(self.path), 'changed-evidence')
