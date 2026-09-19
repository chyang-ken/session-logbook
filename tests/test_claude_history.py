"""Synthetic copied history, compaction, and explicit source-switch regressions."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import server
import session_logbook_cli as cli
from sources import anchored_transcript, claude_history as history

OLD = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
NEW = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'


def message(uuid, text, sid=OLD, parent=None):
    return {'uuid': uuid, 'type': 'user', 'sessionId': sid, 'parentUuid': parent,
            'cwd': '/Users/alice/my-app', 'message': {'role': 'user', 'content': text}}


def write(path, rows):
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    return path


class ClaudeHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'SESSION_LOGBOOK_HISTORY_INDEX': str(self.root / 'index.sqlite3')})
        self.env.start()
        self.addCleanup(self.env.stop)
        history._MEMORY.clear()
        self.project = self.root / 'project'
        self.project.mkdir()
        self.old = write(self.project / (OLD + '.jsonl'), [
            {'type': 'system', 'subtype': 'compact_boundary', 'sessionId': OLD,
             'uuid': 'boundary', 'compactMetadata': {'trigger': 'manual'}, 'logicalParentUuid': 'one'},
            message('two', 'shared text', parent='one'),
            message('branch', 'old branch only', parent='two')])
        self.new = write(self.project / (NEW + '.jsonl'), [
            {'type': 'system', 'subtype': 'compact_boundary', 'sessionId': NEW,
             'uuid': 'boundary', 'compactMetadata': {'trigger': 'manual'}, 'logicalParentUuid': 'one'},
            message('two', 'shared text', NEW, 'boundary'),
            message('three', 'new work', NEW, 'two')])

    def test_overlap_is_not_continuation_or_fork_and_preserves_branch(self):
        info = history.describe(self.new)
        other = info['source_files'][1]
        self.assertEqual(other['session_id'], OLD)
        self.assertEqual(other['shared_records'], 2)
        self.assertEqual(other['unshared_records'], 1)
        self.assertEqual(other['relationship_kind'], 'undetermined')
        self.assertEqual(info['compactions'][0]['trigger'], 'manual')
        body = anchored_transcript.render_claude(self.new)
        self.assertEqual(body.count('shared text'), 1)
        self.assertNotIn('old branch only', body)
        self.assertNotIn('earlier raw history', body)
        self.assertIn('old branch only', cli.read_evidence(self.old, 3))

    def test_explicit_target_cursor_map_rejects_uninherited_tail(self):
        self.assertEqual(history.remap_cursor(self.new, self.old, 2), 2)
        with self.assertRaisesRegex(ValueError, 'absent'):
            history.remap_cursor(self.new, self.old, 3)
        with self.assertRaisesRegex(ValueError, 'nonzero'):
            history.remap_cursor(self.new, self.old, 0)
        with patch.object(server, 'PROJECTS_DIR', self.root):
            body = cli.render_context(self.new, 2, cursor_source_path=str(self.old))
        self.assertIn('SOURCE_CHANGED: true', body)
        self.assertIn('new work', body)
        self.assertNotIn('old branch only', body)
        self.assertIn('CURSOR_SOURCE_PATH: ' + str(self.new.resolve()), body)

    def test_new_prefix_context_cannot_be_skipped_by_shared_anchor(self):
        write(self.old, [message('shared', 'already read')])
        write(self.new, [message('inserted', 'new important context', NEW),
                         message('shared', 'already read', NEW), message('later', 'new tail', NEW)])
        with self.assertRaisesRegex(ValueError, 'before the mapped cursor'):
            history.remap_cursor(self.new, self.old, 1)

    def test_matching_uuid_with_changed_content_is_not_delivered_as_same(self):
        write(self.new, [message('two', 'changed text', NEW)])
        self.assertEqual(history.describe(self.new)['source_files'][1]['content_conflicts'], 1)
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            history.remap_cursor(self.new, self.old, 2)

    def test_usage_updates_do_not_change_message_identity(self):
        first, second = message('same', 'text'), message('same', 'text', NEW)
        first['message']['usage'] = {'tokens': 1}
        second['message']['usage'] = {'tokens': 2}
        write(self.old, [first])
        write(self.new, [second])
        self.assertEqual(history.remap_cursor(self.new, self.old, 1), 1)

    def test_same_id_copy_and_unrelated_file_are_distinct_facts(self):
        copy = write(self.project / 'copy.jsonl', [message('two', 'shared text')])
        write(self.project / 'independent.jsonl', [message('different', 'shared text', 'cccccccc')])
        related = history.describe(self.old)['source_files'][1:]
        self.assertEqual(len(related), 2)
        item = next(x for x in related if x['path'] == str(copy.resolve()))
        self.assertEqual(item['relationship_kind'], 'same_id_copy')
        self.assertEqual(history.remap_cursor(copy, self.old, 2), 1)

    def test_same_id_source_move_across_directories_is_explicit(self):
        destination = self.root / 'moved'
        destination.mkdir()
        copy = write(destination / (OLD + '.jsonl'), [message('two', 'shared text')])
        self.assertEqual(history.remap_cursor(copy, self.old, 2), 1)
        write(copy, [message('two', 'shared text', NEW)])
        with self.assertRaisesRegex(ValueError, 'same project directory'):
            history.remap_cursor(copy, self.old, 2)

    def test_duplicate_rows_render_once_but_conflicts_remain_visible(self):
        row = message('repeat', 'once', NEW)
        write(self.new, [row, row, message('repeat', 'conflict', NEW)])
        body = anchored_transcript.render_claude(self.new)
        self.assertEqual(body.count('once'), 1)
        self.assertIn('conflict', body)
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            history.remap_cursor(self.old, self.new, 3)

    def test_follow_preserves_global_user_anchors_without_reading_old_bodies(self):
        history.summary(self.new)
        with patch.object(server, 'PROJECTS_DIR', self.root):
            body = cli.render_context(self.new, 3)
        self.assertIn('[U2] [L3]', body)
        self.assertNotIn('shared text', body)

    def test_partial_last_line_and_append_keep_physical_cursor(self):
        with self.new.open('a') as stream:
            stream.write('{"uuid":"four",')
        self.assertEqual(history.summary(self.new)['physical_lines'], 4)
        self.assertEqual(list(history.records(self.new, 4)), [])
        with self.new.open('a') as stream:
            stream.write('"type":"user","message":{"content":"completed"}}\n')
        rows = list(history.records(self.new, 4))
        self.assertEqual(rows[0][0], 4)
        self.assertEqual(rows[0][1]['uuid'], 'four')

    def test_rebuildable_cache_skips_parsing_unchanged_and_appends_only_suffix(self):
        history.summary(self.new)
        history._MEMORY.clear()  # Simulate a fresh polling process.
        with patch.object(history, '_payload', wraps=history._payload) as parse:
            history.summary(self.new)
            self.assertEqual(parse.call_count, 0)
            with self.new.open('a') as stream:
                stream.write(json.dumps(message('four', 'appended', NEW)) + '\n')
            history.summary(self.new)
            self.assertEqual(parse.call_count, 1)
        text = self.new.read_text().replace('new work', 'rewritten')
        self.new.write_text(text)
        self.assertIn('rewritten', anchored_transcript.render_claude(self.new))

    def test_unavailable_index_falls_back_and_no_bodies_are_cached(self):
        history.summary(self.new)
        self.assertNotIn('shared text', (self.root / 'index.sqlite3').read_bytes().decode('latin1'))
        with patch.dict(os.environ, {'SESSION_LOGBOOK_HISTORY_INDEX': str(self.root)}):
            history._MEMORY.clear()
            self.assertIn('new work', anchored_transcript.render_claude(self.new))

    def test_search_preserves_both_ids_and_original_evidence(self):
        with patch.object(server, 'PROJECTS_DIR', self.root), patch.object(server, 'load_state'), \
             patch.object(server, '_state', {}):
            hits = cli.search_sessions('shared text', source='claude')
            old_branch = cli.search_sessions('old branch only', source='claude')
        self.assertEqual({item['id'] for item in hits}, {OLD, NEW})
        self.assertEqual([item['id'] for item in old_branch], [OLD])
        for item in hits:
            for snippet in item['snippets']:
                self.assertEqual(snippet['source_path'], item['jsonl_path'])
                self.assertIn('shared text', cli.read_evidence(Path(snippet['source_path']), snippet['line']))

    def test_same_id_copy_preserves_hook_namespace_and_native_identity(self):
        copy = write(self.project / 'copy.jsonl', [message('two', 'shared text')])
        output = io.StringIO()
        with patch.object(server, 'PROJECTS_DIR', self.root), \
             patch('sources.runtime_events.read', return_value={'next_event_cursor': 999}) as hooks, \
             contextlib.redirect_stdout(output):
            code = cli.main(['observe', str(copy), '--cursor-line', '2',
                             '--cursor-source-path', str(self.old), '--event-cursor', '999'])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertFalse(result['session_changed'])
        self.assertEqual(result['session_id'], OLD)
        self.assertEqual(hooks.call_args.args[1:3], (OLD, 999))

    def test_observe_switch_resets_only_new_session_event_namespace(self):
        output = io.StringIO()
        with patch.object(server, 'PROJECTS_DIR', self.root), \
             patch('sources.runtime_events.read', return_value={'next_event_cursor': 7}) as hooks, \
             contextlib.redirect_stdout(output):
            code = cli.main(['observe', str(self.new), '--cursor-line', '2',
                             '--cursor-source-path', str(self.old), '--event-cursor', '999'])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertTrue(result['session_changed'])
        self.assertEqual(result['previous_session_id'], OLD)
        self.assertEqual(result['cursor_reset_scope'], ['hooks', 'native', 'turn_identity'])
        self.assertEqual(hooks.call_args.args[2], 0)
        self.assertIn('new work', result['conversation'])


if __name__ == '__main__':
    unittest.main()
