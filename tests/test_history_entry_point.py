"""Readers must obtain Codex history through codex_history.load, never resolve().

A reader that bypasses the entry point sees only one physical page of a split session
(missing turns, wrong previews) or pays the full ancestry cost on every call.
"""
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


if __name__ == '__main__':
    unittest.main()
