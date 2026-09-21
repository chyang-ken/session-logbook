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
OTHER = 'cccccccc-cccc-cccc-cccc-cccccccccccc'
ALIAS = 'dddddddd-dddd-dddd-dddd-dddddddddddd'


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

    def segment(self, label, rows, base=None, sid=SID, fork=False,
                alias=None, stamps=None, ordinals=True, meta_extra=None):
        """Write one rollout file.

        alias names the file as a physical page (…-<sid>_<alias>.jsonl), stamps gives every
        record a record-level timestamp starting at that ISO prefix, ordinals=False imitates
        the `history_mode: legacy` files the newest CLI writes with no ordinal fields, and
        meta_extra sets further session_meta fields.
        """
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
        if alias:
            meta.update(session_id=sid, history_mode='paginated')
        meta.update(meta_extra or {})
        name = f'rollout-{label}-{sid}' + (f'_{alias}' if alias else '') + '.jsonl'
        p = self.root / name
        records = [{'type': 'session_meta', 'payload': meta}, *rows]

        def write(index, row):
            row = dict(row, ordinal=ordinal + index) if ordinals else dict(row)
            if stamps:
                row['timestamp'] = f'{stamps}{index:02d}.000Z'
            return json.dumps(row) + '\n'

        p.write_text(''.join(write(i, row) for i, row in enumerate(records)))
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

    def test_physical_page_alias_keeps_logical_identity_and_verified_boundaries(self):
        old, middle, latest = self.chain()
        alias = 'dddddddd-dddd-dddd-dddd-dddddddddddd'
        renamed = middle.with_name(middle.stem + '_' + alias + '.jsonl')
        rows = [json.loads(line) for line in middle.read_text().splitlines()]
        rows[0]['payload'].update(session_id=SID, history_mode='paginated')
        renamed.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        middle.unlink()
        rows = [json.loads(line) for line in latest.read_text().splitlines()]
        rows[0]['payload']['history_base'].update(thread_id=alias, end_byte_offset=renamed.stat().st_size)
        latest.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        history = codex_history.resolve(latest)
        self.assertTrue(history['complete'], history['issues'])
        self.assertEqual([s['session_id'] for s in history['segments']], [SID] * 3)
        text = anchored_transcript.render_codex(latest)
        self.assertIn('Middle unread request', text)
        self.assertNotIn('Discarded old branch', text)
        replay = cli.render_context(latest, 2)
        self.assertIn('Goal: build.', replay)
        self.assertIn('SOURCE_CHANGED: true', replay)
        # A filename alone is insufficient: the page must affirm the same logical ID.
        changed = [json.loads(line) for line in renamed.read_text().splitlines()]
        changed[0]['payload']['session_id'] = PARENT
        renamed.write_text(''.join(json.dumps(row) + '\n' for row in changed))
        self.assertFalse(codex_history.resolve(latest)['complete'])

    def test_load_matches_resolve_and_survives_growth(self):
        old, middle, latest = self.chain()
        for _ in range(2):  # second pass reads through a warm index when one is enabled
            self.assertEqual(codex_history.load(latest), codex_history.resolve(latest))
        with latest.open('a') as stream:
            stream.write(json.dumps(dict(msg('Appended request'), ordinal=99)) + '\n')
        self.assertEqual(codex_history.load(latest), codex_history.resolve(latest))

    def test_fresh_continued_page_is_not_single_turn(self):
        old, middle, latest = self.chain()
        self.assertEqual(codex.extract_metadata(latest)['user_turn_count'], 2)
        plain = self.segment('d', [msg('Only request')], sid=PARENT)
        self.assertEqual(codex.extract_metadata(plain)['user_turn_count'], 1)

    def search_both_ways(self, query):
        """Search with matching lines and with whole-file reads; both must agree."""
        # The fast path must really run; a silent fallback would make this comparison vacuous.
        with patch.object(server, '_warn_search_fallback', side_effect=AssertionError):
            fast = server.search_sessions(query)
        with patch.object(server, '_rg_matching_lines', side_effect=RuntimeError('forced')):
            full = server.search_sessions(query)
        self.assertEqual(fast, full)
        return fast

    def test_line_search_matches_whole_file_search(self):
        old, middle, latest = self.chain()
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        claude = Path(other.name) / 'cccccccc-cccc-cccc-cccc-cccccccccccc.jsonl'
        claude.write_text(''.join(json.dumps(row) + '\n' for row in [
            {'type': 'user', 'message': {'content': 'Deploy the widget'}},
            {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'grep widget launch.json'}}]}},
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Widget deployed'}]}},
            {'type': 'user', 'message': {'content': 'Why is {"sessionId": 1} in the log?'}},
        ]))
        server._cache.update({str(latest): codex.extract_metadata(latest),
                              str(claude): {'id': claude.stem, 'jsonl_path': str(claude), 'mtime': 1}})
        hits = lambda query: [r['id'] for r in self.search_both_ways(query)]
        self.assertEqual(hits('Goal: build'), [SID])           # inherited prefix
        self.assertEqual(hits('Latest request'), [SID])        # current page
        self.assertEqual(hits('Discarded old branch'), [])     # replaced tail
        self.assertEqual(hits('launch.json'), [])              # tool input only
        self.assertEqual(hits('widget'), [claude.stem])
        self.assertEqual(hits('widget deployed'), [claude.stem])
        self.assertEqual(hits('unread widget'), [])            # AND across sessions
        self.assertEqual(hits('sessionid'), [claude.stem])     # key-like text in a message
        self.assertEqual(hits('parentuuid'), [])               # JSON keys only
        with patch.dict(server._RG_PCRE2, {server._find_ripgrep(): False}):
            self.assertEqual(hits('sessionid'), [claude.stem])  # literal-match ripgrep
            self.assertEqual(hits('Goal: build'), [SID])
        plain = self.segment('d', [msg('Complete note')], sid=PARENT)
        server._cache[str(plain)] = codex.extract_metadata(plain)
        with plain.open('a') as stream:                        # unfinished record
            stream.write(json.dumps(dict(msg('Half written note'), ordinal=9)))
        self.assertEqual(hits('Half written'), [])
        self.assertEqual(hits('Complete note'), [PARENT])

    def test_line_search_failure_mid_stream_falls_back(self):
        old, middle, latest = self.chain()
        server._cache[str(latest)] = codex.extract_metadata(latest)
        real = server._rg_matching_lines

        def broken(terms, paths):
            yield from real(terms, paths)
            raise RuntimeError('ripgrep exited with status 2')
        expected = server.search_sessions('request')
        with patch.object(server, '_rg_matching_lines', broken):
            self.assertEqual(server.search_sessions('request'), expected)
        self.assertEqual([r['id'] for r in expected], [SID])

    def test_alias_lookup_never_falls_back_without_logical_owner(self):
        old, middle, latest = self.chain()
        rows = [json.loads(line) for line in latest.read_text().splitlines()]
        for value in (None, ''):
            rows[0]['payload']['id'] = value
            latest.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            history = codex_history.resolve(latest)
            self.assertFalse(history['complete'])
            self.assertEqual(history['issues'][0]['reason'], 'unverified_parent_history')

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

    def test_large_replaced_tail_is_not_loaded_and_original_evidence_remains_available(self):
        old = self.segment('old', [msg('Retained constraint')])
        latest = self.segment('new', [msg('New request')], base=(old, 2))
        with old.open('ab') as stream:
            stream.write(json.dumps(dict(msg('x' * (8 * 1024 * 1024)), ordinal=2)).encode() + b'\n')
        original_open = Path.open
        bytes_read = [0]

        class CountedFile:
            def __init__(self, stream):
                self.stream = stream
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.stream.close()
            def __getattr__(self, name):
                return getattr(self.stream, name)
            def read(self, *args):
                data = self.stream.read(*args)
                bytes_read[0] += len(data)
                return data
            def readline(self, *args):
                data = self.stream.readline(*args)
                bytes_read[0] += len(data)
                return data

        def counted_open(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            return CountedFile(stream) if path == old else stream

        with patch.object(Path, 'open', counted_open):
            history = codex_history.resolve(latest)
        self.assertTrue(history['complete'], history['issues'])
        self.assertEqual(len(history['records']), 4)
        self.assertLess(bytes_read[0], 16 * 1024)
        self.assertIn('Retained constraint', cli.read_evidence(old, 2, context=0))
        self.assertGreater(old.stat().st_size, 8 * 1024 * 1024)

    def test_boundary_probe_handles_long_records_and_invalid_prefix(self):
        old = self.segment('long', [msg('Earlier'), msg('x' * 12000)])
        byte = old.stat().st_size
        self.assertEqual(codex_history.boundary_ordinal(old, byte), 2)
        self.assertIsNone(codex_history.boundary_ordinal(old, byte - 1))
        latest = self.segment('new', [msg('Next')], base=(old, 3))
        data = old.read_bytes().replace(b'"text": "Earlier"', b'"text": XEarlier"', 1)
        old.write_bytes(data)
        self.assertFalse(codex_history.resolve(latest)['complete'])

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

    # ---- Session identity: one id is one conversation, and a fork is a different one ----

    def test_paginated_thread_shows_one_card_at_its_newest_page(self):
        """Every rollout file sharing a session_meta.id is one session, one card, one state."""
        first = self.segment('page1', [msg('First page')], stamps='2026-05-01T00:00:')
        second = self.segment('page2', [msg('Second page')], base=(first, 2),
                              alias=ALIAS, stamps='2026-05-01T01:00:')
        metas = [codex.extract_metadata(p) for p in (first, second)]
        self.assertEqual({m['id'] for m in metas}, {SID})
        cards = server._dedup_by_id(sorted(metas, key=server.activity_time, reverse=True))
        self.assertEqual([c['jsonl_path'] for c in cards], [str(second)])
        self.assertEqual(max((first, second), key=codex.rollout_rank), second)

    def test_user_fork_reports_its_parent_instead_of_owning_the_inherited_turns(self):
        parent = self.segment('parent', [msg('Parent turn')], sid=PARENT)
        child = self.segment('child', [msg('Fork own turn')], base=(parent, 2), fork=True)
        history = codex_history.resolve(child)
        self.assertEqual(history['forked_from'], {'session_id': PARENT, 'ordinal_exclusive': 2})
        self.assertEqual([s['relation'] for s in history['segments']], ['inherited', 'current'])
        body = anchored_transcript.render_codex(child)
        self.assertIn(f'[SOURCE {parent} INHERITED from session {PARENT}]', body)
        self.assertIn(f'[SOURCE {child}]', body)
        self.assertIn(f'# FORKED FROM SESSION: {PARENT} at ordinal 2',
                      anchored_transcript.digest_header(child, 'codex'))
        conversation = codex.extract_conversation(child)
        self.assertEqual(conversation['forked_from'], {'session_id': PARENT, 'ordinal_exclusive': 2})
        self.assertEqual(conversation['id'], SID)
        code, text, err = self.cli('locate', SID)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(text)['forked_from']['session_id'], PARENT)

    def test_page_rollover_and_subagent_are_never_reported_as_user_forks(self):
        """forked_from_id is overloaded: most occurrences are sub-agents, not user branches."""
        first = self.segment('page1', [msg('First page')])
        rolled = self.segment('page2', [msg('Second page')], base=(first, 2), alias=ALIAS)
        self.assertIsNone(codex_history.resolve(rolled)['forked_from'])
        self.assertEqual([s['relation'] for s in codex_history.resolve(rolled)['segments']],
                         ['continuation', 'current'])
        spawned = self.segment('spawned', [msg('Sub-agent work')], sid=OTHER, meta_extra={
            'forked_from_id': SID, 'parent_thread_id': SID, 'source': {'subagent': {'other': 'guardian'}}})
        self.assertIsNone(codex_history.fork_lineage(codex._read_session_meta(spawned)))

    def test_unlinked_same_id_page_is_placed_by_record_time_rather_than_dropped(self):
        """A restart after an aborted turn writes a same-id page with no history_base link."""
        aborted = self.segment('aborted', [msg('Aborted first attempt'), event('turn_aborted', 't0')],
                               stamps='2026-06-01T00:00:')
        restart = self.segment('restart', [msg('Retyped request')], alias=ALIAS,
                               stamps='2026-06-01T00:02:')
        history = codex_history.resolve(restart)
        self.assertTrue(history['complete'], history['issues'])
        self.assertEqual([s['relation'] for s in history['segments']], ['unlinked', 'current'])
        self.assertEqual([s['path'] for s in history['segments']], [str(aborted), str(restart)])
        self.assertEqual([t['text'] for t in codex.extract_conversation(restart)['turns']],
                         ['Aborted first attempt', 'Retyped request'])
        self.assertEqual([f['relation'] for f in codex_history.references(restart, history)],
                         ['unlinked', 'current'])
        code, text, err = self.cli('context', SID)
        self.assertEqual(code, 0, err)
        self.assertIn('Aborted first attempt', text)
        self.assertIn(f'# unlinked: {aborted} L1-L3', text)
        code, text, err = self.cli('status', SID)
        self.assertEqual(code, 0, err)
        self.assertEqual([f['relation'] for f in json.loads(text)['source_files']],
                         ['unlinked', 'current'])

    def test_unplaceable_same_id_page_is_reported_incomplete_not_silently_dropped(self):
        overlapping = self.segment('overlap', [msg('Page of unknown order')], stamps='2026-07-01T00:00:')
        current = self.segment('current', [msg('Selected page')], alias=ALIAS, stamps='2026-07-01T00:00:')
        history = codex_history.resolve(current)
        self.assertFalse(history['complete'])
        self.assertEqual([i['reason'] for i in history['issues']], ['unlinked_same_id_segment'])
        self.assertEqual(history['issues'][0]['path'], str(overlapping))
        body = anchored_transcript.render_codex(current)
        self.assertIn('unlinked_same_id_segment', body)
        self.assertNotIn('Page of unknown order', body)
        code, text, err = self.cli('observe', SID)
        self.assertEqual(code, 1)
        self.assertIn('unlinked_same_id_segment', err)

    def test_a_later_same_id_page_is_not_a_gap_in_an_earlier_entry_point(self):
        first = self.segment('page1', [msg('Early turn')], stamps='2026-08-01T00:00:')
        later = self.segment('page2', [msg('Later turn')], base=(first, 2),
                             alias=ALIAS, stamps='2026-08-01T00:30:')
        # Deciding that from the opening record must not load the later page, which can be
        # tens of megabytes.
        real = codex_history.read_segment

        def guarded(target, *args, **kwargs):
            if Path(target).resolve() == later.resolve():
                raise AssertionError('a later page must not be loaded')
            return real(target, *args, **kwargs)

        with patch.object(codex_history, 'read_segment', guarded):
            earlier_view = codex_history.resolve(first)
        self.assertTrue(earlier_view['complete'], earlier_view['issues'])
        self.assertEqual([s['relation'] for s in earlier_view['segments']], ['current'])
        newest_view = codex_history.resolve(later)
        self.assertTrue(newest_view['complete'], newest_view['issues'])
        self.assertEqual([s['relation'] for s in newest_view['segments']], ['continuation', 'current'])

    def test_an_archived_copy_of_a_page_is_the_same_page_not_a_second_one(self):
        """Archiving moves a rollout between roots with its name and bytes unchanged."""
        first = self.segment('page1', [msg('First page')], stamps='2026-10-01T00:00:')
        second = self.segment('page2', [msg('Second page')], base=(first, 2),
                              alias=ALIAS, stamps='2026-10-01T01:00:')
        archive = self.root / 'archive'
        archive.mkdir(exist_ok=True)
        (archive / first.name).write_bytes(first.read_bytes())
        history = codex_history.resolve(second)
        self.assertTrue(history['complete'], history['issues'])
        self.assertEqual([s['path'] for s in history['segments']], [str(first), str(second)])

    def test_records_without_ordinals_resolve_and_keep_time_order(self):
        """`history_mode: legacy` rollouts carry no ordinal fields; nothing may assume them."""
        legacy = {'history_mode': 'legacy'}
        early = self.segment('legacy1', [msg('Legacy first')], ordinals=False,
                             stamps='2026-09-01T00:00:', meta_extra=legacy)
        late = self.segment('legacy2', [msg('Legacy second')], ordinals=False, alias=ALIAS,
                            stamps='2026-09-01T00:10:', meta_extra=dict(legacy, session_id=SID))
        self.assertNotIn('"ordinal"', late.read_text())
        history = codex_history.resolve(late)
        self.assertTrue(history['complete'], history['issues'])
        self.assertEqual([s['path'] for s in history['segments']], [str(early), str(late)])
        self.assertEqual([t['text'] for t in codex.extract_conversation(late)['turns']],
                         ['Legacy first', 'Legacy second'])
        self.assertEqual(codex.extract_metadata(late)['user_turn_count'], 1)
        text = anchored_transcript.render_codex(late)
        self.assertLess(text.index('Legacy first'), text.index('Legacy second'))

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
