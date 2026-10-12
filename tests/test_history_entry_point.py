"""Readers must obtain Codex history through codex_history.load, never resolve().

A reader that bypasses the entry point sees only one physical page of a split session
(missing turns, wrong previews) or pays the full ancestry cost on every call.
"""
import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Only the history layer itself may call the resolver; passing it as a callable is fine.
ALLOWED = {'sources/codex_history.py', 'sources/history_index.py'}
CALL = re.compile(r'\bcodex_history\.resolve\(|(?<![\w.])resolve\(\s*(?:path|jsonl_path|target)\b')


class EntryPointTests(unittest.TestCase):
    def test_no_reader_calls_the_resolver_directly(self):
        offenders = []
        for path in [ROOT / 'server.py', ROOT / 'session_logbook_cli.py', *sorted((ROOT / 'sources').glob('*.py'))]:
            rel = path.relative_to(ROOT).as_posix()
            if rel in ALLOWED:
                continue
            for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                if CALL.search(line):
                    offenders.append(f'{rel}:{number}: {line.strip()}')
        self.assertEqual(offenders, [], 'use codex_history.load() instead:\n' + '\n'.join(offenders))


class ClaudeMessageEntryPointTests(unittest.TestCase):
    def test_reading_surfaces_do_not_bypass_shared_message_reader(self):
        for filename, names in {
            'server.py': ('extract_conversation', 'extract_transcript'),
            'sources/anchored_transcript.py': ('render_claude',),
        }.items():
            tree = ast.parse((ROOT / filename).read_text())
            for function in tree.body:
                if not isinstance(function, ast.FunctionDef) or function.name not in names:
                    continue
                with self.subTest(surface=function.name):
                    calls = {ast.unparse(node.func) for node in ast.walk(function)
                             if isinstance(node, ast.Call)}
                    self.assertIn('claude_messages.read', calls)
                    forbidden = {'claude_history.records', 'json.loads', 'anchored_user_text',
                                 'project_reply_text', 'session_message_text',
                                 'claude_events.project_relay_event', 'claude_events.enqueued_text'}
                    self.assertFalse(calls & forbidden, calls & forbidden)


if __name__ == '__main__':
    unittest.main()
