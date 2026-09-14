"""Conversation ordering must not follow filesystem-only rewrites."""
import json
import os
import tempfile
import unittest
from pathlib import Path

import server
from sources import codex, antigravity, kimi, devin
from sources.activity import activity_fields, activity_time


class ActivityTests(unittest.TestCase):
    def test_timestamp_and_fallback(self):
        self.assertEqual(activity_fields([None, 'invalid', float('nan')], 42)['activity_at'], 42)
        self.assertEqual(activity_fields(['2026-07-05T12:00:00Z', '2026-07-05T06:00:00-07:00'], 42)['activity_at_iso'], '2026-07-05T13:00:00+00:00')

    def test_claude_rewrite_keeps_conversation_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.jsonl'
            rows = [
                {'type': 'user', 'timestamp': '2026-07-05T11:00:00Z', 'message': {'content': 'Review the orchard plan'}},
                {'type': 'assistant', 'timestamp': '2026-07-05T12:00:00Z', 'message': {'content': [{'type': 'text', 'text': 'Done'}]}},
                {'type': 'system', 'timestamp': '2026-09-14T12:00:00Z'},
                {'type': 'user', 'timestamp': '2026-09-14T12:00:01Z', 'message': {'content': '<system-reminder>environment</system-reminder>'}},
            ]
            path.write_text('\n'.join(map(json.dumps, rows)))
            before = server.extract_metadata(path)
            os.utime(path, (1900000000, 1900000000))
            after = server.extract_metadata(path)
            self.assertEqual(after['activity_at_iso'], '2026-07-05T12:00:00+00:00')
            self.assertEqual(before['activity_at'], after['activity_at'])
            self.assertEqual(after['mtime'], 1900000000)
            newer = {'activity_at': after['activity_at'] + 3600, 'mtime': 1}
            self.assertIs(sorted([after, newer], key=activity_time, reverse=True)[0], newer)
            self.assertEqual(server.compute_scope(after, {}, now=1900000000), 'dusty')

    def test_existing_jsonl_source_fixtures_have_message_time(self):
        fixtures = Path(__file__).parent / 'fixtures'
        cases = [(codex, fixtures / 'codex' / 'basic_main.jsonl'),
                 (antigravity, next((fixtures / 'antigravity').rglob('transcript.jsonl'))),
                 (kimi, next((fixtures / 'kimi').rglob('agents/main/wire.jsonl')))]
        for source, path in cases:
            with self.subTest(source=source.__name__):
                meta = source.extract_metadata(path)
                self.assertGreater(meta['activity_at'], 0)
                self.assertNotEqual(meta['activity_at'], meta['mtime'])

    def test_devin_ignores_control_node_and_session_update(self):
        chain = [
            {'created_at': 100, 'chat_message': '{}', 'message': {'role': 'assistant', 'content': 'Answer'}},
            {'created_at': 300, 'chat_message': '{}', 'message': {'role': 'tool', 'content': 'Output'}},
        ]
        meta = devin._metadata(Path('/tmp/sessions.db/example'), {'id': 'example', 'last_activity_at': 500}, chain)
        self.assertEqual(meta['activity_at'], 100)
        self.assertEqual(meta['mtime'], 500)
