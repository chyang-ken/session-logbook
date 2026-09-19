"""Synthetic tests for explicit Desktop rewind grouping and fail-open behavior."""
import json
from pathlib import Path
import tempfile
import unittest

from sources import claude_desktop as desktop

OLD = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
MID = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
NEW = 'cccccccc-cccc-cccc-cccc-cccccccccccc'
OTHER = 'dddddddd-dddd-dddd-dddd-dddddddddddd'


class DesktopRewindTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.descriptors = self.root / 'descriptors'
        self.folder = self.descriptors / 'account' / 'workspace'
        self.folder.mkdir(parents=True)
        self.project = str(self.root / 'project')
        self.items = []
        for sid in (OLD, MID, NEW, OTHER):
            path = self.root / (sid + '.jsonl')
            path.write_text('{}\n')
            self.items.append({'id': sid, 'source': 'claude', 'project_path': self.project,
                               'jsonl_path': str(path), 'display_title': 'Example ' + sid[0]})
        self.descriptor = {'sessionId': 'local_example', 'cliSessionId': NEW,
                           'cwd': self.project, 'priorCliSessionIds': [OLD, MID],
                           'rewindEdges': [self.edge(OLD, MID), self.edge(MID, NEW)]}
        self.save()

    def edge(self, parent, child):
        return {'parent': parent, 'child': child, 'forkPoint': 'example-message',
                'at': 123456, 'cwd': self.project}

    def save(self, value=None, name='local_example.json'):
        (self.folder / name).write_text(json.dumps(self.descriptor if value is None else value))

    def annotate(self):
        return desktop.annotate_sessions(self.items, self.descriptors)

    def assert_open(self):
        self.assertEqual(self.annotate(), self.items)

    def test_chain_current_and_history_with_no_source_mutation(self):
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = self.annotate()
        self.assertEqual(result[0]['rewind_current_session_id'], NEW)
        self.assertEqual(result[1]['rewind_current_session_id'], NEW)
        self.assertEqual(result[2]['desktop_session_id'], 'local_example')
        self.assertEqual([h['session_id'] for h in result[2]['rewind_history']], [MID, OLD])
        self.assertEqual(result[2]['rewind_history'][0]['title'], 'Example b')
        self.assertEqual(result[3], self.items[3])
        self.assertNotIn('desktop_session_id', self.items[0])
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        metadata = desktop.metadata_for_session(NEW, self.items, self.descriptors)
        self.assertIn('rewind_history', metadata)
        self.assertNotIn('jsonl_path', metadata)

    def test_prior_ids_alone_are_not_proof(self):
        self.descriptor.pop('rewindEdges')
        self.save()
        self.assert_open()

    def test_changed_descriptor_immediately_changes_current(self):
        self.annotate()
        self.descriptor['cliSessionId'] = MID
        self.save()
        result = self.annotate()
        self.assertEqual(result[0]['rewind_current_session_id'], MID)
        self.assertIn('rewind_history', result[1])
        self.assertNotIn('rewind_current_session_id', result[2])

    def test_unconnected_edges_are_not_hidden(self):
        self.descriptor['rewindEdges'].append(self.edge(OTHER, 'unavailable'))
        self.save()
        result = self.annotate()
        self.assertEqual(result[3], self.items[3])
        self.assertIn('rewind_history', result[2])

    def test_intentional_fork_keeps_old_session_visible(self):
        self.save({'sessionId': 'local_independent', 'cliSessionId': OLD,
                   'cwd': self.project}, 'local_independent.json')
        self.assert_open()

    def test_current_claimed_by_another_desktop_is_ambiguous(self):
        self.save({'sessionId': 'local_independent', 'cliSessionId': NEW,
                   'cwd': self.project}, 'local_independent.json')
        self.assert_open()

    def test_duplicate_conflicting_descriptor_is_ambiguous(self):
        other = dict(self.descriptor, cliSessionId=MID)
        self.save(other, 'local_duplicate.json')
        self.assert_open()

    def test_identical_descriptor_copy_is_safe(self):
        self.save(self.descriptor, 'local_duplicate.json')
        self.assertIn('rewind_history', self.annotate()[2])

    def test_duplicate_source_id_is_ambiguous(self):
        self.items.append(dict(self.items[0]))
        self.assert_open()

    def test_cycle_is_not_hidden(self):
        self.descriptor['rewindEdges'].append(self.edge(NEW, OLD))
        self.save()
        self.assert_open()

    def test_two_parents_are_ambiguous(self):
        self.descriptor['rewindEdges'].append(self.edge(OTHER, NEW))
        self.save()
        self.assert_open()

    def test_missing_card_or_file_is_not_hidden(self):
        self.items.pop(0)
        self.assert_open()
        self.items.insert(0, {'id': OLD, 'source': 'claude', 'project_path': self.project,
                              'jsonl_path': str(self.root / 'missing.jsonl')})
        self.assert_open()

    def test_wrong_source_is_not_hidden(self):
        self.items[0]['source'] = 'codex'
        self.assert_open()

    def test_wrong_project_card_or_edge_is_not_hidden(self):
        self.items[0]['project_path'] = str(self.root / 'another-project')
        self.assert_open()
        self.items[0]['project_path'] = self.project
        self.descriptor['rewindEdges'][0]['cwd'] = str(self.root / 'another-project')
        self.save()
        self.assert_open()

    def test_path_filename_must_match_identity(self):
        self.items[0]['jsonl_path'] = self.items[1]['jsonl_path']
        self.assert_open()

    def test_malformed_and_partial_descriptor_are_ignored(self):
        for value in ('{', '[]', '{"sessionId": "local_example"}'):
            (self.folder / 'local_example.json').write_text(value)
            self.assert_open()

    def test_removed_evidence_clears_previous_annotation(self):
        self.items = self.annotate()
        (self.folder / 'local_example.json').unlink()
        result = self.annotate()
        for item in result:
            self.assertNotIn('rewind_history', item)
            self.assertNotIn('rewind_current_session_id', item)
            self.assertNotIn('desktop_session_id', item)

    def test_no_descriptor_root_is_safe(self):
        self.assertEqual(desktop.annotate_sessions(self.items, self.root / 'absent'), self.items)


if __name__ == '__main__':
    unittest.main()
