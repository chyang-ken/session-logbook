"""Selected Claude ancestry with conservative, read-only historical access."""
import json
from pathlib import Path
import tempfile
import unittest

from sources import claude_history


class ClaudeRewindTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.jsonl'

    def message(self, uuid, parent, kind='user', text=None):
        return {'type': kind, 'uuid': uuid, 'parentUuid': parent,
                'sessionId': 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                'message': {'role': kind, 'content': text or uuid}}

    def pointer(self, leaf):
        return {'type': 'last-prompt', 'leafUuid': leaf,
                'sessionId': 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'}

    def write(self, rows):
        self.path.write_text(''.join(json.dumps(row) + '\n' for row in rows))

    def branch(self):
        return [self.message('root', None), self.message('old', 'root', 'assistant'),
                self.message('old-user', 'old'), self.message('new', 'root'),
                self.message('reply', 'new', 'assistant'), self.pointer('reply')]

    def uuids(self, **kwargs):
        return [r['uuid'] for _, r in claude_history.records(self.path, **kwargs) if r.get('uuid')]

    def test_selected_chain_preserves_physical_anchors_and_user_numbers(self):
        self.write(self.branch())
        actual = [(n, r['uuid'], r['_logbook_user_turn'])
                  for n, r in claude_history.records(self.path) if r.get('uuid')]
        self.assertEqual(actual, [(1, 'root', 1), (4, 'new', 2), (5, 'reply', 2)])
        info = claude_history.describe(self.path)['rewind']
        self.assertEqual(info['status'], 'selected')
        self.assertEqual(info['hidden_message_count'], 2)
        self.assertEqual(info['active_leaf_uuid'], 'reply')

    def test_all_saved_records_remain_readable_without_source_mutation(self):
        self.write(self.branch())
        original = self.path.read_bytes()
        self.assertEqual(self.uuids(include_rewound=True), ['root', 'old', 'old-user', 'new', 'reply'])
        self.assertEqual(self.uuids(first=4), ['new', 'reply'])
        self.assertEqual(self.path.read_bytes(), original)

    def test_post_pointer_reply_is_retained(self):
        rows = self.branch()[:-2] + [self.pointer('new'), self.message('reply', 'new', 'assistant')]
        self.write(rows)
        self.assertEqual(self.uuids(), ['root', 'new', 'reply'])

    def test_pointer_at_user_cannot_hide_existing_assistant_descendant(self):
        self.write([self.message('root', None), self.message('reply', 'root', 'assistant'),
                    self.pointer('root')])
        self.assertEqual(self.uuids(), ['root', 'reply'])
        self.assertEqual(claude_history.rewind_status(self.path)['reason'],
                         'leaf_has_unselected_descendants')

    def test_without_pointer_physical_recency_does_not_select_branch(self):
        self.write(self.branch()[:-1])
        self.assertEqual(len(self.uuids()), 5)
        self.assertEqual(claude_history.rewind_status(self.path)['reason'], 'missing_leaf_pointer')

    def test_new_branch_after_pointer_is_ambiguous(self):
        self.write(self.branch() + [self.message('third', 'root')])
        self.assertEqual(len(self.uuids()), 6)
        self.assertEqual(claude_history.rewind_status(self.path)['reason'],
                         'ambiguous_records_after_pointer')

    def test_unsafe_graphs_never_hide_records(self):
        mutations = {
            'missing_parent_record': lambda rows: rows[1].update(parentUuid='absent'),
            'ambiguous_history_roots': lambda rows: rows[1].update(parentUuid=None),
            'cyclic_parent_chain': lambda rows: rows[1].update(parentUuid='old-user'),
            'missing_leaf_record': lambda rows: rows[-1].update(leafUuid='absent'),
            'embedded_sidechain': lambda rows: rows[1].update(isSidechain=True),
        }
        for reason, mutate in mutations.items():
            with self.subTest(reason=reason):
                rows = self.branch()
                mutate(rows)
                self.write(rows)
                self.assertEqual(len(self.uuids()), 5)
                self.assertEqual(claude_history.rewind_status(self.path)['reason'], reason)

    def test_conflicting_copied_uuid_is_preserved(self):
        rows = self.branch()
        rows.insert(3, self.message('old', 'root', 'assistant', 'changed copy'))
        self.write(rows)
        self.assertEqual(len(self.uuids()), 6)
        self.assertEqual(claude_history.rewind_status(self.path)['reason'], 'conflicting_record_uuid')

    def test_identical_copies_still_deduplicate_in_both_views(self):
        rows = self.branch()
        rows.insert(3, rows[1].copy())
        self.write(rows)
        self.assertEqual(len(self.uuids()), 3)
        self.assertEqual(len(self.uuids(include_rewound=True)), 5)

    def test_compaction_does_not_masquerade_as_rewind(self):
        rows = self.branch()
        rows[1].update(type='system', subtype='compact_boundary', compactMetadata={'trigger': 'auto'})
        self.write(rows)
        self.assertEqual(len(self.uuids()), 5)
        self.assertEqual(claude_history.rewind_status(self.path)['reason'], 'compacted_history')

    def test_partial_tail_does_not_hide_until_source_is_complete(self):
        self.write(self.branch())
        with self.path.open('a') as stream:
            stream.write('{"type":')
        self.assertEqual(len(self.uuids()), 5)
        self.assertEqual(claude_history.rewind_status(self.path)['reason'], 'incomplete_or_invalid_records')

    def test_incremental_pointer_change_reselects_branch(self):
        self.write(self.branch())
        self.assertEqual(self.uuids(), ['root', 'new', 'reply'])
        with self.path.open('a') as stream:
            stream.write(json.dumps(self.pointer('old-user')) + '\n')
        self.assertEqual(self.uuids(), ['root', 'old', 'old-user'])


if __name__ == '__main__':
    unittest.main()
