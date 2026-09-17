"""Synthetic continuous history, provenance, and consumer-contract acceptance."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server
import session_logbook_cli as cli
from sources import codex, codex_history, anchored_transcript

SID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
PARENT = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'


def msg(text):
    return {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
            'content': [{'type': 'input_text', 'text': text}]}}


def event(kind, turn):
    return {'type': 'event_msg', 'payload': {'type': kind, 'turn_id': turn}}


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in [('CODEX_ROOT', self.root), ('CODEX_ARCHIVED_ROOT', self.root / 'archive'),
                            ('SESSION_INDEX_PATH', self.root / 'index.jsonl')]:
            self.stack.enter_context(patch.object(codex, name, value))
        self.stack.enter_context(patch.object(server, '_cache', {}))
        self.stack.enter_context(patch.dict(os.environ, {'SESSION_LOGBOOK_EVENTS': str(self.root/'events.db')}))

    def segment(self, label, rows, base=None, sid=SID, fork=False):
        ordinal = 0
        meta = {'id': sid, 'cwd': '/example', 'timestamp': '2026-01-0' + str(len(list(self.root.glob('*.jsonl'))) + 1) + 'T00:00:00Z'}
        if base:
            prior, end_line = base
            selected = codex_history.read_segment(prior)[0][:end_line]
            ordinal = selected[-1]['record']['ordinal'] + 1
            prior_id = selected[0]['record']['payload']['id']
            meta['history_base'] = {'thread_id': prior_id, 'end_ordinal_exclusive': ordinal,
                                    'end_byte_offset': selected[-1]['end']}
            if fork:
                meta.update(forked_from_id=prior_id, forked_from_ordinal_exclusive=ordinal)
        p = self.root / f'rollout-{label}-{sid}.jsonl'
        records = [{'type': 'session_meta', 'payload': meta}, *rows]
        p.write_text(''.join(json.dumps(dict(row, ordinal=ordinal+i)) + '\n' for i, row in enumerate(records)))
        return p

    def cli(self, *args):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = cli.main(list(args))
        return code, output.getvalue(), error.getvalue()

    def chain(self):
        old = self.segment('a', [msg('Goal: build. Authorization: local only. Do not publish.'),
            event('task_started','t1'), msg('Old unread tail'), msg('Discarded old branch')])
        middle = self.segment('b', [msg('Middle unread request'), event('task_complete','t1')], base=(old,4))
        latest = self.segment('c', [msg('Latest request'), event('task_started','t2')], base=(middle,3))
        return old, middle, latest

    def test_complete_history_excludes_replaced_tail_with_provenance(self):
        old, middle, latest = self.chain()
        history = codex_history.resolve(latest)
        self.assertTrue(history['complete'], history['issues'])
        text = anchored_transcript.render_codex(latest)
        for value in ('Goal: build.', 'Do not publish.', 'Old unread tail', 'Middle unread request', 'Latest request'):
            self.assertEqual(text.count(value), 1)
        self.assertNotIn('Discarded old branch', text)
        for p in (old, middle, latest):
            self.assertIn(str(p), text)
        conv = codex.extract_conversation(latest)
        self.assertTrue(conv['context_complete'])
        self.assertEqual([t['source_path'] for t in conv['turns']], [str(old),str(old),str(middle),str(latest)])
        code, text, err = self.cli('follow', SID, '--cursor-line','2','--cursor-source-path',str(old))
        self.assertEqual(code,0,err)
        self.assertIn('Old unread tail',text)
        self.assertIn('Middle unread request',text)
        self.assertIn('Latest request',text)

    def test_incremental_pages_cross_all_segments_without_duplicate_events(self):
        old, middle, latest = self.chain()
        source, native, conv = str(old), 2, 2
        seen, bodies, pages = [], [], 0
        while True:
            code, text, err = self.cli('observe',SID,'--native-line-cursor',str(native),
                '--cursor-line',str(conv),'--cursor-source-path',source,'--limit','1')
            self.assertEqual(code,0,err)
            page=json.loads(text); pages+=1
            self.assertLess(pages,10)
            seen.extend(e['facts']['type'] for e in page['native']['events'])
            bodies.append(page['conversation'])
            source=page['transcript_path']; native=page['native']['next_line_cursor']
            import re
            conv=int(re.search(r'^# NEXT_CURSOR: L(\d+)$',page['conversation'],re.M)[1])
            self.assertIsInstance(native,int)
            if not page['native']['has_more']:break
        self.assertEqual(seen,['task_started','task_complete','task_started'])
        self.assertIn('Old unread tail','\n'.join(bodies))
        self.assertIn('Middle unread request','\n'.join(bodies))
        code,text,err=self.cli('observe',SID,'--native-line-cursor',str(native),'--cursor-line',str(conv),'--cursor-source-path',source)
        self.assertEqual(code,0,err)
        self.assertEqual(json.loads(text)['native']['events'],[])

    def test_observation_manifest_uses_effective_cutoff_without_resolving_again(self):
        old, middle, latest = self.chain()
        rows = codex_history.resolve(latest)['segments'][0]['records']
        with patch.object(codex_history, 'resolve', side_effect=AssertionError('duplicate history read')):
            text = cli._codex_context(old, records=rows)
        self.assertIn(str(old) + ' L1-L4', text)
        self.assertNotIn(str(old) + ' L1-L5', text)
        self.assertNotIn('Discarded old branch', text)

    def test_deep_history_does_not_depend_on_python_recursion_limit(self):
        previous = None
        for n in range(1100):
            previous = self.segment(str(n), [msg('Segment ' + str(n))],
                                    base=(previous, 2) if previous else None)
        history = codex_history.resolve(previous)
        self.assertTrue(history['complete'], history['issues'])
        self.assertEqual(len(history['segments']), 1100)
        self.assertEqual(len(history['records']), 2200)

    def test_missing_or_conflicting_boundary_is_explicit(self):
        old,middle,latest=self.chain()
        middle.unlink()
        history=codex_history.resolve(latest)
        self.assertFalse(history['complete'])
        self.assertEqual(history['issues'][0]['reason'],'missing_history_segment')
        code,text,err=self.cli('context',SID)
        self.assertIn('# CONTEXT_COMPLETE: false',text)
        code,text,err=self.cli('observe',SID)
        self.assertEqual(code,1)
        self.assertIn('context_incomplete',err)

    def test_parent_inheritance_does_not_import_parent_future_or_native_events(self):
        parent=self.segment('parent',[msg('Inherited constraints'),event('task_complete','parent'),msg('Parent future')],sid=PARENT)
        child=self.segment('child',[msg('Child task'),event('task_started','child')],base=(parent,3),fork=True)
        self.assertTrue(codex_history.resolve(child)['complete'])
        text=anchored_transcript.render_codex(child)
        self.assertIn('Inherited constraints',text)
        self.assertNotIn('Parent future',text)
        code,text,err=self.cli('observe',SID)
        self.assertEqual(code,0,err)
        self.assertEqual([e['facts']['turn_id'] for e in json.loads(text)['native']['events']],['child'])
        other=self.segment('subagent',[msg('Separate child')],sid='cccccccc-cccc-cccc-cccc-cccccccccccc')
        self.assertNotIn('Separate child',anchored_transcript.render_codex(child))

    def test_cross_segment_tool_result_pairs_in_shared_view(self):
        old=self.segment('call',[{'type':'response_item','payload':{'type':'function_call','call_id':'call1','name':'read','arguments':'{}'}}])
        new=self.segment('result',[{'type':'response_item','payload':{'type':'function_call_output','call_id':'call1','output':'done'}}],base=(old,2))
        conv=codex.extract_conversation(new)
        self.assertEqual(conv['turns'][0]['result'],'done')
        self.assertEqual(conv['turns'][0]['source_path'],str(old))
        self.assertEqual(conv['turns'][0]['result_source'],{'path':str(new),'line':2})
        self.assertIn(str(old),codex.extract_transcript(new))

    def test_incomplete_record_is_reported_and_never_advanced(self):
        old,middle,latest=self.chain()
        with latest.open('a') as stream:stream.write('{"unfinished":')
        self.assertFalse(codex_history.resolve(latest)['complete'])
        code,text,err=self.cli('observe',SID)
        self.assertEqual(code,1)
        self.assertIn('unfinished_record',err)

    def test_cursor_in_replaced_tail_fails_instead_of_silently_replaying(self):
        old,middle,latest=self.chain()
        code,text,err=self.cli('observe',SID,'--native-line-cursor','5','--cursor-line','5','--cursor-source-path',str(old))
        self.assertEqual(code,1)
        self.assertIn('replaced',err)

    def test_conflicting_boundary_and_unverified_fork_are_not_guessed(self):
        parent=self.segment('parent',[msg('Parent constraints')],sid=PARENT)
        child=self.segment('child',[msg('Own task')],base=(parent,2),fork=True)
        rows=[json.loads(line) for line in child.read_text().splitlines()]
        rows[0]['payload']['forked_from_ordinal_exclusive'] += 1
        child.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        self.assertEqual(codex_history.resolve(child)['issues'][0]['reason'],'unverified_parent_history')
        rows[0]['payload']['forked_from_ordinal_exclusive'] -= 1
        rows[0]['payload']['history_base']['end_byte_offset'] -= 1
        child.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        self.assertEqual(codex_history.resolve(child)['issues'][0]['reason'],'missing_history_segment')

    def test_conflicting_candidate_prefixes_report_ambiguity(self):
        old,middle,latest=self.chain()
        twin=self.root/'rollout-other.jsonl'
        twin.write_bytes(old.read_bytes().replace(b'Old unread tail',b'Bad unread tail'))
        history=codex_history.resolve(latest)
        self.assertFalse(history['complete'])
        self.assertEqual(history['issues'][0]['reason'],'ambiguous_history_segment')

    def test_source_tokens_remain_compatible_with_observe(self):
        old,middle,latest=self.chain()
        token=codex.next_cursor(old,4)
        code,text,err=self.cli('observe',SID,'--native-line-cursor',token,'--cursor-line',token)
        self.assertEqual(code,0,err)
        page=json.loads(text)
        self.assertEqual(page['transcript_path'],str(middle))
        self.assertEqual(page['native']['events'][0]['facts']['turn_id'],'t1')
        self.assertEqual(page['cursor_reset_reason'],'transcript_changed')

    def test_recovered_ancestor_changes_http_fingerprint(self):
        old,middle,latest=self.chain()
        contents=middle.read_bytes();middle.unlink()
        missing=server.file_fingerprint(latest)
        middle.write_bytes(contents)
        self.assertNotEqual(server.file_fingerprint(latest),missing)
        self.assertTrue(codex.extract_conversation(latest)['context_complete'])

    def test_stable_id_and_source_manifest_survive_multiple_segments(self):
        old,middle,latest=self.chain()
        renamed=latest.with_name('rollout-latest-'+SID+'_dddddddd-dddd-dddd-dddd-dddddddddddd.jsonl')
        latest.rename(renamed)
        conv=codex.extract_conversation(renamed)
        self.assertEqual(conv['id'],SID)
        self.assertEqual(conv['jsonl_path'],str(renamed))
        self.assertEqual([f['relation'] for f in conv['source_files']],['continuation','continuation','current'])
        self.assertEqual([f['last_line'] for f in conv['source_files']],[4,3,3])
        for command in ('locate','status'):
            code,text,err=self.cli(command,SID)
            self.assertEqual(code,0,err)
            metadata=json.loads(text)
            self.assertEqual(metadata['id'],SID)
            self.assertEqual(metadata['jsonl_path'],str(renamed))
            self.assertEqual(len(metadata['source_files']),3)
        header=anchored_transcript.digest_header(renamed,'codex')
        self.assertNotIn('SOURCE (full, authoritative)',header)
        self.assertIn(str(old),header)
        self.assertIn(str(renamed),header)

    def test_inherited_source_id_does_not_replace_selected_session_id(self):
        parent=self.segment('parent',[msg('Inherited restriction')],sid=PARENT)
        child=self.segment('child',[msg('Own task')],base=(parent,2),fork=True)
        conv=codex.extract_conversation(child)
        self.assertEqual(conv['id'],SID)
        self.assertEqual(conv['source_files'][0]['session_id'],PARENT)
        self.assertEqual(conv['source_files'][0]['relation'],'inherited')
        self.assertEqual(conv['source_files'][1]['session_id'],SID)
        self.assertEqual(conv['source_files'][1]['relation'],'current')

    def test_cli_and_web_search_effective_history_and_keep_snippet_origins(self):
        old,middle,latest=self.chain()
        self.stack.enter_context(patch.object(server,'load_state'))
        self.stack.enter_context(patch.object(server,'_state',{}))
        server._cache={str(p):codex.extract_metadata(p) for p in (old,middle,latest)}
        with patch.object(cli,'iter_session_paths',return_value=iter([old,middle,latest])):
            hits=cli.search_sessions('Goal Latest',source='codex',role='user')
        self.assertEqual(len(hits),1)
        self.assertEqual(hits[0]['id'],SID)
        self.assertEqual(hits[0]['jsonl_path'],str(latest))
        self.assertEqual({s['source_path'] for s in hits[0]['snippets']},{str(old),str(latest)})
        self.assertEqual(len(server.search_sessions('Goal Latest')),1)
        self.assertEqual(server.search_sessions('Discarded old branch'),[])
        with patch.object(cli,'iter_session_paths',return_value=iter([old,middle,latest])):
            self.assertEqual(cli.search_sessions('Discarded old branch',source='codex'),[])
        snippets=server.search_sessions('Old unread')[0]['snippets']
        self.assertEqual(snippets[0]['source_path'],str(old))
        self.assertEqual(snippets[0]['line'],4)

    def test_brief_cache_invalidates_when_only_inherited_context_changes(self):
        old,middle,latest=self.chain()
        self.stack.enter_context(patch.object(server,'_state',{}))
        with patch.object(server,'save_state'), patch.object(server,'generate_briefing',return_value='summary') as generate:
            text=codex.extract_transcript(latest)
            self.assertEqual(server.get_or_generate_brief(SID,latest,text)[1],'generated')
            self.assertEqual(server.get_or_generate_brief(SID,latest,text)[1],'cached')
            old.write_bytes(old.read_bytes().replace(b'Goal: build.',b'Goal: audit.'))
            self.assertEqual(server.get_or_generate_brief(SID,latest,codex.extract_transcript(latest))[1],'regenerated')
            self.assertEqual(generate.call_count,2)
