"""Project conversations retain full authored messages through the Agent entry points."""
import copy
from contextlib import ExitStack, redirect_stdout
import hashlib
import html
import io
import json
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import patch

import server
import session_logbook_cli as cli
from sources import (anchored_transcript, antigravity, claude_desktop, claude_events, claude_history,
                     claude_text, codex, devin, kimi, pi, search_text)

SID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
THREAD = 'cmsg_0aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
REPLY = 'The answer has several parts.\n' * 45 + 'REPLY_FINAL_CONDITION'
EDIT = 'The corrected answer keeps the exception.\n' * 40 + 'EDIT_FINAL_CONDITION'
NOTE = 'The coordinator reports evidence, not user approval.\n' * 35 + 'PEER_FINAL_CONDITION'


def relay(text):
    return (f'<project_claude_message session="session_example" thread_id="{THREAD}">'
            '<relay from="coordinator"><note>' + html.escape(text)
            + '</note></relay></project_claude_message>')


def record(n, kind, **fields):
    return dict(type=kind, uuid=f'message-{n}', parentUuid=f'message-{n - 1}' if n > 1 else None,
                sessionId=SID, cwd='/Users/alice/my-app',
                timestamp=f'2026-01-01T00:00:{n:02d}Z', **fields)


def user(n, text, **extra):
    return record(n, 'user', message={'role': 'user', 'content': text}, **extra)


def attachment(n, text):
    return record(n, 'attachment', attachment={
        'type': 'queued_command', 'commandMode': 'prompt', 'prompt': relay(text),
        'origin': {'kind': 'task-notification', 'subkind': 'projects-relay'},
        'source_uuid': f'source-{n}', 'delivery_id': f'delivery-{n}'})


def call(n, name, text, **inputs):
    return record(n, 'assistant', message={'role': 'assistant', 'content': [
        {'type': 'tool_use', 'id': f'tool-{n}', 'name': name,
         'input': dict(text=text, **inputs)}]})


def receipt(n, tool, error=False):
    return record(n, 'user', message={'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': tool, 'is_error': error,
         'content': 'Delivery rejected' if error else '{"message_id":"cmsg_reply"}'}]})


class ProjectReadingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        stack = ExitStack()
        self.addCleanup(stack.close)
        # Every discovery source and writable derived store is isolated, even for exact
        # path calls: an implementation change must not silently touch a real library.
        for module, key, value in [
            (server, 'PROJECTS_DIR', self.root / 'claude'),
            (server, 'STATE_FILE', self.root / 'state.json'),
            (server, 'SCAN_CACHE_FILE', self.root / 'scan.json'),
            (server, 'SCAN_CACHE_BACKUP_DIR', self.root / 'scan-backups'),
            (server, 'BACKUP_DIR', self.root / 'backups'),
            (server, '_cache', {}), (server, '_state', {}),
            (server, '_state_loaded', True),
            (codex, 'CODEX_ROOT', self.root / 'codex'),
            (codex, 'CODEX_ARCHIVED_ROOT', self.root / 'codex-archive'),
            (codex, 'SESSION_INDEX_PATH', self.root / 'codex-index.jsonl'),
            (antigravity, 'AG_ROOT', self.root / 'antigravity'),
            (antigravity, 'AG_BRAIN', self.root / 'antigravity' / 'brain'),
            (kimi, 'KIMI_HOME', self.root / 'kimi'),
            (kimi, 'KIMI_SESSIONS_ROOT', self.root / 'kimi' / 'sessions'),
            (pi, 'PI_SESSIONS_ROOT', self.root / 'pi'),
            (devin, 'DEVIN_ROOT', self.root / 'devin'),
            (claude_desktop, 'DESKTOP_ROOT', self.root / 'desktop'),
        ]:
            stack.enter_context(patch.object(module, key, value))
        stack.enter_context(patch.dict('os.environ', {
            'SESSION_LOGBOOK_HISTORY_INDEX': str(self.root / 'history.sqlite'),
            'SESSION_LOGBOOK_EVENTS': str(self.root / 'events.sqlite'),
            'SESSION_LOGBOOK_SEARCH_TEXT': str(self.root / 'search-text')}))
        claude_history._MEMORY.clear()
        self.addCleanup(claude_history._MEMORY.clear)
        self.path = server.PROJECTS_DIR / '-Users-alice-my-app' / (SID + '.jsonl')
        self.path.parent.mkdir(parents=True)
        wake = ('<wake reason="mention"><project><thread ts="' + THREAD + '">'
                '<message from="human" trust="principal">First &amp; second</message>'
                '<message from="human" trust="principal">Keep the final condition.</message>'
                '</thread></project></wake>')
        self.rows = [user(1, wake, projectsUserTurn=True, origin={'kind': 'human'}),
                     attachment(2, NOTE),
                     call(3, 'mcp__hearthbot__reply', REPLY), receipt(4, 'tool-3'),
                     call(5, 'mcp__hearthbot__update_message', EDIT, message_id='cmsg_reply'),
                     receipt(6, 'tool-5', error=True),
                     call(7, 'mcp__hearthbot__reply', 'A reply with no receipt yet.'),
                     user(8, relay('Another delivered check-in.')),
                     {'type': 'last-prompt', 'sessionId': SID, 'leafUuid': 'message-8'}]
        self.write()

    def write(self):
        self.path.write_text(''.join(json.dumps(row) + '\n' for row in self.rows))
        claude_history._MEMORY.clear()

    def run_cli(self, *args, skill=False):
        output = io.StringIO()
        with redirect_stdout(output):
            if skill:
                wrapper = Path(__file__).resolve().parents[1] / 'skills/session-logbook/scripts/session_logbook.py'
                with patch.object(sys, 'argv', [str(wrapper), *map(str, args)]):
                    with self.assertRaises(SystemExit) as raised:
                        runpy.run_path(str(wrapper), run_name='__main__')
                code = raised.exception.code
            else:
                code = cli.main(list(map(str, args)))
        self.assertEqual(code, 0, output.getvalue())
        return output.getvalue()

    def test_skill_context_keeps_bodies_receipts_and_source_anchors(self):
        original = hashlib.sha256(self.path.read_bytes()).hexdigest()
        body = self.run_cli('context', self.path, skill=True)
        for text in (REPLY, EDIT, NOTE, 'First & second\n\nKeep the final condition.'):
            self.assertIn(text, body)
        self.assertIn('[L2]   ⚠ EVENT TEAMMATE_MESSAGE: [coordinator] ' + NOTE, body)
        self.assertIn('PROJECT_REPLY (tool_use_id=tool-3; result=OK at L4)', body)
        self.assertIn('PROJECT_EDIT (message_id=cmsg_reply; tool_use_id=tool-5; result=ERROR at L6)', body)
        self.assertIn('PROJECT_REPLY (tool_use_id=tool-7; result=unconfirmed)', body)
        self.assertIn('Delivery rejected', body)
        self.assertEqual(len(anchored_transcript.turn_anchors(body)), 1)
        evidence = self.run_cli('evidence', self.path, '--line', 2, '--context', 0)
        self.assertIn('projects-relay', evidence)
        self.assertIn('PEER_FINAL_CONDITION', evidence)
        self.assertEqual(original, hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_delta_keeps_full_messages_and_a_late_receipt_keeps_its_call_id(self):
        body = self.run_cli('follow', self.path, '--cursor-line', 2, '--delta')
        self.assertIn(NOTE, body)
        self.assertIn(REPLY, body)
        self.assertIn(EDIT, body)
        self.assertNotIn('First & second', body)
        late = self.run_cli('follow', self.path, '--cursor-line', 4, '--delta')
        self.assertNotIn(REPLY, late)
        self.assertIn('[tool_use_id=tool-3]', late)
        self.assertIn('TOOL_RESULT OK', late)

    def test_observe_reuses_full_text_without_making_a_peer_a_human_control(self):
        observation = json.loads(self.run_cli('observe', self.path))
        for text in (NOTE, REPLY, EDIT):
            self.assertIn(text, observation['conversation'])
        self.assertEqual([e['facts']['type'] for e in observation['native']['events']], ['user_input'])

    def test_peer_search_and_thread_discovery_keep_their_identity(self):
        peers = json.loads(self.run_cli('search', 'PEER_FINAL_CONDITION', '--role', 'peer'))
        self.assertEqual([r['id'] for r in peers], [SID])
        self.assertEqual(peers[0]['snippets'][0]['role'], 'peer')
        self.assertEqual(peers[0]['snippets'][0]['line'], 2)
        self.assertEqual(json.loads(self.run_cli('search', 'PEER_FINAL_CONDITION', '--role', 'user')), [])
        replies = json.loads(self.run_cli('search', 'REPLY_FINAL_CONDITION', '--role', 'assistant'))
        self.assertEqual([r['id'] for r in replies], [SID])
        located = json.loads(self.run_cli('locate', THREAD))
        self.assertEqual(located['id'], SID)
        self.assertIn(NOTE, self.run_cli('context', SID))

    def test_recent_keeps_a_worker_with_a_mid_turn_checkin(self):
        self.rows = self.rows[:2]
        self.write()
        recent = json.loads(self.run_cli('recent', '--source', 'claude', '--since', '2025-01-01'))
        self.assertEqual([item['id'] for item in recent], [SID])

    def test_turn_slice_retains_the_full_reply_after_the_requested_human_turn(self):
        self.rows = self.rows[:8] + [user(9, 'Now revise the answer.'),
                                    call(10, 'mcp__hearthbot__reply', REPLY + '\nRevised tail.')]
        self.write()
        body = self.run_cli('context', self.path, '--last-turns', 1)
        self.assertIn('Now revise the answer.', body)
        self.assertIn(REPLY + '\nRevised tail.', body)
        self.assertNotIn(NOTE, body)
        self.assertIn('OMITTED_BEFORE_SLICE', body)

    def test_shared_reader_and_export_keep_relays_edits_and_failure_receipts(self):
        turns = server.extract_conversation(self.path)['turns']
        self.assertIn('[coordinator] ' + NOTE, [t['text'] for t in turns if t['type'] == 'teammate_message'])
        self.assertIn(EDIT, [t['text'] for t in turns if t['type'] == 'assistant'])
        self.assertTrue(any(t.get('is_error') and 'Delivery rejected' in t.get('result', '') for t in turns))
        export = server.extract_transcript(self.path)
        for text in (NOTE, EDIT, REPLY):
            self.assertIn(text, export)

    def test_remote_session_messages_keep_full_input_and_separate_receipts(self):
        message = 'Report to the parent, not human authorization.\n' * 40 + 'REMOTE_MESSAGE_END'
        def send(n, text):
            return record(n, 'assistant', message={'role': 'assistant', 'content': [{
                'type': 'tool_use', 'id': f'tool-{n}',
                'name': 'mcp__claude-code-remote__send_message',
                'input': {'session_id': 'parent', 'visibility': 'sent_to_shared_agent',
                          'message': text}}]})
        self.rows = [user(1, 'Report the findings.'), send(2, message),
                     receipt(3, 'tool-2'), send(4, message + '\nRejected attempt.'),
                     receipt(5, 'tool-4', error=True), send(6, message + '\nPending attempt.')]
        self.write()
        turns = server.extract_conversation(self.path)['turns']
        sends = [t for t in turns if t.get('name') == 'mcp__claude-code-remote__send_message']
        self.assertEqual(len(sends), 3)
        for turn in sends:
            self.assertIn(message, turn['message'])
            self.assertIn('parent', turn['message_context'])
            self.assertIn('sent_to_shared_agent', turn['message_context'])
        self.assertFalse(sends[0]['is_error'])
        self.assertTrue(sends[1]['is_error'])
        self.assertIn('Delivery rejected', sends[1]['result'])
        self.assertNotIn('result', sends[2])
        self.assertEqual([t['type'] for t in turns], ['user', 'tool', 'tool', 'tool'])
        body = self.run_cli('context', self.path, skill=True)
        self.assertEqual(body.count(message), 3)
        for status in ('result=OK at L3', 'result=ERROR at L5', 'result=unconfirmed'):
            self.assertIn(status, body)
        self.assertIn('[L2] ASSISTANT SESSION_MESSAGE', body)
        self.assertIn('sent_to_shared_agent', body)
        self.assertEqual(len(anchored_transcript.turn_anchors(body)), 1)
        delta = self.run_cli('follow', self.path, '--cursor-line', 3, '--delta')
        self.assertIn('tool_use_id=tool-2', delta)
        self.assertIn(message + '\nPending attempt.', delta)
        export = server.extract_transcript(self.path)
        self.assertEqual(export.count(message), 3)
        self.assertIn('SESSION MESSAGE', export)

    def test_remote_send_is_not_inferred_from_arbitrary_message_fields(self):
        block = {'type': 'tool_use', 'name': 'mcp__other__send_message',
                 'input': {'message': 'Not a known session message.'}}
        self.assertIsNone(claude_text.session_message_text(block))
        block['name'] = 'mcp__claude-code-remote__send_message'
        for value in (None, {}, [], '', '   '):
            block['input'] = {'message': value}
            self.assertIsNone(claude_text.session_message_text(block))

    def test_queue_bookkeeping_and_duplicate_uuid_do_not_repeat_a_delivery(self):
        self.rows.insert(1, {'type': 'queue-operation', 'operation': 'enqueue', 'content': relay(NOTE)})
        self.rows.insert(3, copy.deepcopy(self.rows[2]))
        self.write()
        body = self.run_cli('context', self.path)
        self.assertEqual(body.count(NOTE), 1)
        self.assertNotIn('QUEUED_INPUT', body)
        self.assertEqual(server.extract_metadata(self.path)['user_turn_count'], 1)
        self.assertFalse(server.extract_metadata(self.path)['single_turn'])

    def test_compaction_preserves_messages_but_reports_unverified_selection(self):
        self.rows += [record(9, 'system', subtype='compact_boundary', compactMetadata={}),
                      user(10, 'Summary of earlier work.', isCompactSummary=True)]
        self.write()
        body = self.run_cli('context', self.path)
        self.assertIn(NOTE, body)
        self.assertIn(REPLY, body)
        self.assertIn('selected branch unverified', body)
        self.assertEqual(len(anchored_transcript.turn_anchors(body)), 1)

    def test_rewind_removes_reply_and_relay_from_delta_but_keeps_raw_evidence(self):
        replacement = user(9, 'Please take the other approach.')
        replacement['parentUuid'] = 'message-1'
        self.rows += [replacement, {'type': 'last-prompt', 'leafUuid': 'message-9', 'sessionId': SID}]
        self.write()
        body = self.run_cli('follow', self.path, '--cursor-line', 8, '--delta')
        self.assertNotIn(NOTE, body)
        self.assertNotIn(REPLY, body)
        self.assertIn('REMOVED_BEFORE_CURSOR: L2-L8', body)
        self.assertIn('Please take the other approach.', body)
        self.assertIn('PEER_FINAL_CONDITION', self.run_cli('evidence', self.path, '--line', 2, '--context', 0))

    def test_missing_wrapper_cannot_promote_a_structural_relay_to_human(self):
        self.rows[1]['attachment']['prompt'] = NOTE
        self.write()
        self.assertEqual(claude_text.anchored_user_text(self.rows[1]), '')
        body = self.run_cli('context', self.path)
        self.assertIn('TEAMMATE_MESSAGE: [peer] ' + NOTE, body)
        self.assertEqual(len(anchored_transcript.turn_anchors(body)), 1)

    def test_unknown_attachment_and_tool_names_are_not_guessed_as_messages(self):
        unknown = attachment(2, NOTE)
        unknown['attachment']['origin']['subkind'] = 'unknown'
        self.assertEqual(claude_text.normalize_record(unknown), unknown)
        self.assertIsNone(claude_text.project_reply_text({
            'type': 'tool_use', 'name': 'mcp__other__reply', 'input': {'text': REPLY}}))
        self.assertIsNone(claude_text.project_reply_text({
            'type': 'tool_use', 'name': ['mcp__hearthbot__reply'], 'input': {'text': REPLY}}))

    def test_old_history_cache_rebuilds_attachment_identity_without_source_change(self):
        with patch.object(claude_history, 'SCHEMA', 9), \
                patch.object(claude_history, 'normalize_record', lambda row: row):
            cached = claude_history.summary(self.path)
            self.assertEqual(cached['rows'][1]['type'], 'attachment')
        claude_history._MEMORY.clear()
        fresh = claude_history.summary(self.path)
        self.assertEqual(fresh['rows'][1]['type'], 'user')
        self.assertFalse(fresh['rows'][1]['user_turn'])

    def test_old_search_sidecar_rebuilds_to_include_peer_words(self):
        store = self.root / 'search-text'
        with patch.object(search_text, '_fresh', {}):
            with patch.object(search_text, 'FORMAT', 1), \
                    patch.object(claude_events, 'project_relay_words', return_value=''):
                sidecar, _ = search_text.refresh(self.path, 'generic', store, float('inf'))
                self.assertNotIn('peer_final_condition', sidecar.read_text())
            search_text._fresh.clear()  # A different build starts with an empty memory cache.
            rebuilt, read_bytes = search_text.refresh(self.path, 'generic', store, float('inf'))
            self.assertGreater(read_bytes, 0)
            self.assertIn('peer_final_condition', rebuilt.read_text())

    def test_peer_search_decodes_entities_and_does_not_match_generated_labels(self):
        self.rows = [user(1, 'Begin.'), attachment(2, 'Check <gate> & R&D.')]
        self.write()
        for term in ('<gate>', 'R&D'):
            self.assertEqual(len(json.loads(self.run_cli('search', term, '--role', 'peer'))), 1)
        self.rows[1]['attachment']['prompt'] = 'Plain delivered note.'
        self.write()
        text = search_text.message_text(claude_text.normalize_record(self.rows[1]))
        self.assertEqual(text, 'Plain delivered note.')
        self.assertEqual(json.loads(self.run_cli('search', 'peer', '--role', 'peer')), [])
