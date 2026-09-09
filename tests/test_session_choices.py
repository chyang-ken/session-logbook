"""Synthetic tests for shared session selection hints."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server
import session_logbook_cli as cli
from sources.session_identity import session_choices


class SessionChoicesTests(unittest.TestCase):
    def item(self, sid='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', **extra):
        return dict(id=sid, source='claude', jsonl_path='/tmp/project/'+sid+'.jsonl',
                    project_path='/tmp/project', mtime=1, **extra)

    def test_single_turn_is_reversible_hint_not_machine_identity(self):
        row = self.item(single_turn=True, custom_title='Named by a person', starred=True)
        result = session_choices([row])[0]
        self.assertEqual(result['selection_group'], 'other')
        self.assertEqual(result['interaction_kind'], 'unknown')
        self.assertFalse(row.get('archived', False))

    def test_children_excluded_and_cli_relationship_agrees(self):
        parent = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        row = self.item()
        row['jsonl_path'] = '/tmp/project/'+parent+'/subagents/agent-worker.jsonl'
        self.assertEqual(session_choices([row]), [])
        self.assertEqual(cli._relationship(Path(row['jsonl_path']), 'claude'), (True, parent))

    def test_noise_cannot_evict_primary_and_title_uses_actual_opener(self):
        normal = self.item(single_turn=False, recent_msgs=[{'role':'user','text':'Please investigate retries','ts':'2026-01-01'}])
        noise = self.item('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', single_turn=True)
        noise['mtime'] = 99
        result = session_choices([noise, normal], source='claude', limit=1)
        self.assertEqual([x['selection_group'] for x in result], ['primary','other'])
        self.assertEqual(result[0]['title'], 'Please investigate retries')
        self.assertEqual(result[0]['preview'], 'Please investigate retries')

    def test_archived_and_other_sources_do_not_leak(self):
        archived = self.item(archived=True)
        other = self.item(); other['source'] = 'codex'
        self.assertEqual(session_choices([archived, other], source='claude'), [])

    def test_large_single_turn_ignores_meta_and_tool_results(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.jsonl'
            rows=[{'type':'user','message':{'content':'Evaluate this example'}},
                  {'type':'assistant','message':{'content':'x'*5000}},
                  {'type':'user','isMeta':True,'message':{'content':'Injected context'}},
                  {'type':'user','message':{'content':[{'type':'tool_result','content':'Result'}]}}]
            p.write_text('\n'.join(json.dumps(r) for r in rows))
            with patch.object(server, 'TAIL_BUFFER', 1024):
                self.assertTrue(server.extract_metadata(p)['single_turn'])
                rows.append({'type':'user','message':{'content':[{'type':'text','text':'Follow up'}]}})
                p.write_text('\n'.join(json.dumps(r) for r in rows))
                self.assertFalse(server.extract_metadata(p)['single_turn'])

    def test_same_title_never_merges_session_identity(self):
        rows=[self.item(custom_title='Same'), self.item('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',custom_title='Same')]
        self.assertEqual(len(session_choices(rows)), 2)
