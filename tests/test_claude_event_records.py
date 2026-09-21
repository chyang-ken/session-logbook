"""Queue entries, task notices and API errors are events, and never human speech.

Three Claude record kinds used to fall out of the reader entirely. They are now shown, in
place, as system events. The rules these tests hold to:

* a queue entry appears only when nothing in the file confirms it was handed over, and a
  delivered one appears exactly once, as the delivered message;
* none of the three is ever counted as a user turn, previewed on a card, reachable by the
  ``user`` role filter, or given a ``[U#]`` anchor;
* a run of retries collapses into one row carrying its count and its time span.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server
import session_logbook_cli as cli
from sources import anchored_transcript, claude_events, claude_history

SID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
CWD = '/Users/alice/my-app'
TYPED = 'Stop and rerun the migration against the staging copy.'
NOTICE = ('<task-notification>\n<task-id>bbbb1111</task-id>\n'
          '<tool-use-id>toolu_bbbb1111</tool-use-id>\n'
          '<output-file>/tmp/example/bbbb1111.output</output-file>\n'
          '<status>completed</status>\n<summary>Agent "Fixture sweep" finished</summary>\n'
          '<note>Fires once per stop.</note>\n</task-notification>')


def user(text, ts, uuid, parent=None):
    return {'type': 'user', 'uuid': uuid, 'parentUuid': parent, 'sessionId': SID,
            'cwd': CWD, 'timestamp': ts, 'message': {'role': 'user', 'content': text}}


def assistant(text, ts, uuid, parent):
    return {'type': 'assistant', 'uuid': uuid, 'parentUuid': parent, 'sessionId': SID,
            'cwd': CWD, 'timestamp': ts,
            'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': text}],
                        'stop_reason': 'end_turn'}}


def enqueue(text, ts):
    return {'type': 'queue-operation', 'operation': 'enqueue', 'content': text,
            'sessionId': SID, 'timestamp': ts}


def dequeue(ts):
    return {'type': 'queue-operation', 'operation': 'dequeue', 'sessionId': SID,
            'timestamp': ts}


def notice_attachment(ts, uuid, parent):
    return {'type': 'attachment', 'uuid': uuid, 'parentUuid': parent, 'sessionId': SID,
            'cwd': CWD, 'timestamp': ts,
            'attachment': {'type': 'queued_command', 'commandMode': 'task-notification',
                           'prompt': NOTICE, 'timestamp': ts}}


def api_error(ts, attempt, formatted='Connection dropped (ECONNRESET)', parent='u1'):
    # The uuid keys on the timestamp because the history reader deduplicates a repeated
    # (uuid, payload) pair, and two retries are genuinely two records.
    return {'type': 'system', 'subtype': 'api_error', 'level': 'error',
            'parentUuid': parent, 'isSidechain': False, 'sessionId': SID, 'cwd': CWD,
            'uuid': 'err-%s-%d' % (ts, attempt), 'timestamp': ts,
            'error': {'message': 'Connection error.', 'formatted': formatted,
                      'connection': {'code': 'ECONNRESET', 'message': 'socket closed',
                                     'isSSLError': False},
                      'isNetworkDown': False, 'rateLimits': None, 'noResponse': None},
            'retryInMs': 560, 'retryAttempt': attempt, 'maxRetries': 10,
            'source': 'request_retry'}


class EventRecordTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        env = patch.dict('os.environ',
                         {'SESSION_LOGBOOK_HISTORY_INDEX': str(self.dir / 'index.sqlite')})
        env.start()
        self.addCleanup(env.stop)
        claude_history._MEMORY.clear()
        self.addCleanup(claude_history._MEMORY.clear)

    def write(self, rows, name=SID):
        path = self.dir / (name + '.jsonl')
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        claude_history._MEMORY.clear()
        return path

    def turns(self, path):
        return server.extract_conversation(path)['turns']

    def kinds(self, path):
        return [t['type'] for t in self.turns(path)]

    # ---------- the baseline: a file with none of these renders exactly as before ----------

    def test_a_file_without_event_records_is_untouched(self):
        rows = [user('Start the work', '2026-01-01T00:00:00Z', 'u1'),
                assistant('On it.', '2026-01-01T00:00:01Z', 'a1', 'u1')]
        path = self.write(rows)
        self.assertEqual(self.kinds(path), ['user', 'assistant'])
        self.assertEqual(server.extract_metadata(path)['user_turn_count'], 1)
        self.assertRegex(anchored_transcript.render_claude(path), r'\[U1\] \[L1\] USER')
        self.assertNotIn('EVENT', anchored_transcript.render_claude(path))

    # ---------- a. a queue entry with no delivery confirmation ----------

    def test_queued_input_without_delivery_is_shown_as_an_unconfirmed_event(self):
        rows = [user('Start the work', '2026-01-01T00:00:00Z', 'u1'),
                enqueue(TYPED, '2026-01-01T00:00:05Z'),
                dequeue('2026-01-01T00:00:05Z'),
                assistant('Done.', '2026-01-01T00:00:09Z', 'a1', 'u1')]
        path = self.write(rows)
        turns = self.turns(path)
        self.assertEqual([t['type'] for t in turns], ['user', 'queued_input', 'assistant'])
        self.assertEqual(turns[1]['text'], TYPED)
        self.assertEqual(turns[1]['ts'], '2026-01-01T00:00:05Z')

    def test_an_unconfirmed_queue_entry_is_never_a_user_turn_or_a_preview(self):
        rows = [user('Start the work', '2026-01-01T00:00:00Z', 'u1'),
                enqueue(TYPED, '2026-01-01T00:00:05Z')]
        path = self.write(rows)
        meta = server.extract_metadata(path)
        self.assertEqual(meta['user_turn_count'], 1)
        self.assertEqual([m['text'] for m in meta['recent_msgs']], ['Start the work'])
        self.assertNotIn(TYPED, json.dumps(meta, ensure_ascii=False))
        self.assertEqual(cli._message_from_row(enqueue(TYPED, 'x'), 'claude'), (None, ''))
        rendered = anchored_transcript.render_claude(path)
        self.assertIn('QUEUED_INPUT (delivery not confirmed)', rendered)
        self.assertEqual(rendered.count('[U'), 1)  # only the real turn took an anchor

    def test_unconfirmed_queued_text_is_searchable_under_its_own_role(self):
        rows = [user('Start the work', '2026-01-01T00:00:00Z', 'u1'),
                enqueue(TYPED, '2026-01-01T00:00:05Z')]
        path = self.write(rows)
        snippets = server._search_session(path, ['migration'])
        self.assertEqual([s['role'] for s in snippets], ['queued'])
        self.assertIn('migration', snippets[0]['text'])
        self.assertNotEqual(snippets[0]['role'], 'user')
        self.assertNotEqual(snippets[0]['role'], 'you')
        roles = [role for _, role, text in cli.iter_messages(path, 'claude') if text]
        self.assertIn(claude_events.QUEUED_INPUT_ROLE, roles)
        self.assertNotIn(claude_events.QUEUED_INPUT_ROLE, ('user', 'assistant'))

    # ---------- queued then delivered: exactly one turn ----------

    def test_a_delivered_queue_entry_appears_once_as_the_delivered_message(self):
        rows = [user('Start the work', '2026-01-01T00:00:00Z', 'u1'),
                enqueue(TYPED, '2026-01-01T00:00:05Z'),
                dequeue('2026-01-01T00:00:05Z'),
                user(TYPED, '2026-01-01T00:00:06Z', 'u2', 'u1')]
        path = self.write(rows)
        turns = self.turns(path)
        self.assertEqual([t['type'] for t in turns], ['user', 'user'])
        self.assertEqual(sum(t['text'] == TYPED for t in turns), 1)
        self.assertEqual(server.extract_metadata(path)['user_turn_count'], 2)
        self.assertEqual(anchored_transcript.render_claude(path).count(TYPED), 1)
        self.assertEqual(server.extract_transcript(path).count(TYPED), 1)
        self.assertEqual([s['role'] for s in server._search_session(path, ['migration'])],
                         ['you'])

    def test_delivery_through_an_interruption_attachment_also_counts(self):
        rows = [user('Start the work', '2026-01-01T00:00:00Z', 'u1'),
                enqueue(TYPED, '2026-01-01T00:00:05Z'),
                {'type': 'attachment', 'uuid': 'att', 'parentUuid': 'u1', 'sessionId': SID,
                 'cwd': CWD, 'timestamp': '2026-01-01T00:00:06Z',
                 'attachment': {'type': 'queued_command', 'commandMode': 'prompt',
                                'origin': {'kind': 'human'}, 'prompt': TYPED}},
                {'type': 'queue-operation', 'operation': 'remove', 'content': TYPED,
                 'sessionId': SID, 'timestamp': '2026-01-01T00:00:06Z'}]
        path = self.write(rows)
        self.assertEqual(self.kinds(path), ['user', 'user'])
        self.assertEqual(anchored_transcript.render_claude(path).count(TYPED), 1)

    def test_the_same_text_typed_twice_and_delivered_once_keeps_one_of_each(self):
        rows = [user('Start', '2026-01-01T00:00:00Z', 'u1'),
                enqueue(TYPED, '2026-01-01T00:00:05Z'),
                enqueue(TYPED, '2026-01-01T00:00:07Z'),
                user(TYPED, '2026-01-01T00:00:08Z', 'u2', 'u1')]
        path = self.write(rows)
        self.assertEqual(self.kinds(path), ['user', 'queued_input', 'user'])

    # ---------- b. background-task completion notices ----------

    def test_a_notification_attachment_is_shown_as_a_system_event(self):
        rows = [user('Start the work', '2026-01-01T00:00:00Z', 'u1'),
                notice_attachment('2026-01-01T00:00:04Z', 'att', 'u1')]
        path = self.write(rows)
        turns = self.turns(path)
        self.assertEqual([t['type'] for t in turns], ['user', 'system_notification'])
        self.assertEqual(turns[1]['text'], '[completed] Agent "Fixture sweep" finished')
        self.assertTrue(turns[1]['delivered'])
        self.assertEqual(server.extract_metadata(path)['user_turn_count'], 1)
        self.assertIn('EVENT TASK_NOTIFICATION:', anchored_transcript.render_claude(path))

    def test_a_queued_notification_says_it_was_never_delivered(self):
        rows = [user('Start the work', '2026-01-01T00:00:00Z', 'u1'),
                enqueue(NOTICE, '2026-01-01T00:00:04Z')]
        path = self.write(rows)
        turns = self.turns(path)
        self.assertEqual([t['type'] for t in turns], ['user', 'system_notification'])
        self.assertIs(turns[1]['delivered'], False)
        self.assertIn('TASK_NOTIFICATION (queued, delivery not confirmed)',
                      anchored_transcript.render_claude(path))

    def test_a_notification_delivered_after_being_queued_appears_once(self):
        rows = [user('Start the work', '2026-01-01T00:00:00Z', 'u1'),
                enqueue(NOTICE, '2026-01-01T00:00:04Z'),
                dequeue('2026-01-01T00:00:04Z'),
                user(NOTICE, '2026-01-01T00:00:05Z', 'u2', 'u1')]
        path = self.write(rows)
        turns = self.turns(path)
        self.assertEqual([t['type'] for t in turns], ['user', 'system_notification'])
        self.assertNotIn('delivered', turns[1])

    def test_a_notification_queued_then_delivered_by_attachment_appears_once(self):
        rows = [user('Start the work', '2026-01-01T00:00:00Z', 'u1'),
                enqueue(NOTICE, '2026-01-01T00:00:04Z'),
                notice_attachment('2026-01-01T00:00:05Z', 'att', 'u1')]
        path = self.write(rows)
        turns = self.turns(path)
        self.assertEqual([t['type'] for t in turns], ['user', 'system_notification'])
        self.assertTrue(turns[1]['delivered'])
        self.assertEqual(anchored_transcript.render_claude(path).count('TASK_NOTIFICATION'), 1)
        self.assertEqual(server.extract_transcript(path).count('## NOTIFICATION'), 1)

    def test_a_delivered_notification_is_an_event_not_a_user_turn_anywhere(self):
        rows = [user('Start the work', '2026-01-01T00:00:00Z', 'u1'),
                user(NOTICE, '2026-01-01T00:00:05Z', 'u2', 'u1'),
                user('Carry on', '2026-01-01T00:00:06Z', 'u3', 'u2')]
        path = self.write(rows)
        self.assertEqual(self.kinds(path), ['user', 'system_notification', 'user'])
        self.assertEqual(server.extract_metadata(path)['user_turn_count'], 2)
        rendered = anchored_transcript.render_claude(path)
        self.assertEqual(rendered.count('[U'), 2)
        self.assertIn('[U2] [L3] USER', rendered)  # the notice did not consume an anchor
        self.assertIn('EVENT TASK_NOTIFICATION:', rendered)
        self.assertEqual(server._search_session(path, ['fixture sweep']), [])
        rows_seen = list(claude_history.records(path))
        self.assertEqual([r[1]['_logbook_user_turn'] for r in rows_seen], [1, 1, 2])

    def test_notifications_are_not_indexed_for_search(self):
        rows = [user('Start the work', '2026-01-01T00:00:00Z', 'u1'),
                enqueue(NOTICE, '2026-01-01T00:00:04Z'),
                notice_attachment('2026-01-01T00:00:05Z', 'att', 'u1')]
        path = self.write(rows)
        self.assertEqual(server._search_session(path, ['fixture sweep']), [])
        self.assertEqual(server._search_session(path, ['bbbb1111']), [])

    # ---------- c. connection errors and their retries ----------

    def test_a_single_api_error_is_one_event_row(self):
        rows = [user('Start the work', '2026-01-01T00:00:00Z', 'u1'),
                api_error('2026-01-01T00:00:03Z', 1),
                assistant('Recovered.', '2026-01-01T00:00:20Z', 'a1', 'u1')]
        path = self.write(rows)
        turns = self.turns(path)
        self.assertEqual([t['type'] for t in turns], ['user', 'api_error', 'assistant'])
        self.assertEqual(turns[1]['text'], 'Connection dropped (ECONNRESET)')
        self.assertEqual(turns[1]['retries'], 1)
        self.assertEqual(server.extract_metadata(path)['user_turn_count'], 1)

    def test_consecutive_retries_collapse_into_one_row_with_count_and_span(self):
        rows = [user('Start the work', '2026-01-01T00:00:00Z', 'u1')]
        rows += [api_error('2026-01-01T00:0%d:00Z' % n, n) for n in range(1, 5)]
        rows += [assistant('Recovered.', '2026-01-01T00:05:00Z', 'a1', 'u1')]
        path = self.write(rows)
        turns = self.turns(path)
        self.assertEqual([t['type'] for t in turns], ['user', 'api_error', 'assistant'])
        self.assertEqual(turns[1]['retries'], 4)
        self.assertEqual(turns[1]['text'],
                         'Connection dropped (ECONNRESET) — retried 4 times over 3m00s')
        self.assertEqual(turns[1]['ts'], '2026-01-01T00:01:00Z')
        self.assertIn('retried 4 times over 3m00s', anchored_transcript.render_claude(path))

    def test_a_different_failure_never_hides_inside_another_ones_count(self):
        rows = [user('Start the work', '2026-01-01T00:00:00Z', 'u1'),
                api_error('2026-01-01T00:01:00Z', 1),
                api_error('2026-01-01T00:01:03Z', 2),
                api_error('2026-01-01T00:01:09Z', 1, '529 Overloaded'),
                assistant('Recovered.', '2026-01-01T00:02:00Z', 'a1', 'u1')]
        path = self.write(rows)
        turns = self.turns(path)
        self.assertEqual([t['type'] for t in turns],
                         ['user', 'api_error', 'api_error', 'assistant'])
        self.assertEqual(turns[1]['retries'], 2)
        self.assertEqual(turns[2]['text'], '529 Overloaded')

    def test_a_reply_between_two_failures_starts_a_new_run(self):
        rows = [user('Start the work', '2026-01-01T00:00:00Z', 'u1'),
                api_error('2026-01-01T00:01:00Z', 1),
                assistant('Partial.', '2026-01-01T00:01:30Z', 'a1', 'u1'),
                api_error('2026-01-01T00:02:00Z', 1)]
        path = self.write(rows)
        self.assertEqual(self.kinds(path),
                         ['user', 'api_error', 'assistant', 'api_error'])

    def test_the_raw_upstream_error_body_never_reaches_a_conversation_line(self):
        row = api_error('2026-01-01T00:01:00Z', 1, '529 Overloaded')
        row['error']['message'] = '529 {"type":"error","error":{"details":"secret"}}'
        path = self.write([user('Start', '2026-01-01T00:00:00Z', 'u1'), row])
        rendered = json.dumps(self.turns(path)) + anchored_transcript.render_claude(path)
        self.assertNotIn('secret', rendered)
        self.assertIn('529 Overloaded', rendered)

    def test_api_errors_are_not_indexed_for_search(self):
        path = self.write([user('Start', '2026-01-01T00:00:00Z', 'u1'),
                           api_error('2026-01-01T00:01:00Z', 1)])
        self.assertEqual(server._search_session(path, ['econnreset']), [])

    # ---------- every consumer, including sub-agent transcripts ----------

    def test_the_markdown_export_labels_all_three(self):
        rows = [user('Start the work', '2026-01-01T00:00:00Z', 'u1'),
                enqueue(TYPED, '2026-01-01T00:00:05Z'),
                notice_attachment('2026-01-01T00:00:06Z', 'att', 'u1'),
                api_error('2026-01-01T00:00:07Z', 1),
                api_error('2026-01-01T00:00:09Z', 2)]
        path = self.write(rows)
        text = server.extract_transcript(path)
        self.assertIn('## QUEUED-INPUT (delivery not confirmed)', text)
        self.assertIn('## NOTIFICATION\n[completed]', text)
        self.assertIn('## CONNECTION-ERROR', text)
        self.assertIn('retried 2 times', text)
        self.assertEqual(text.count('## USER'), 1)

    def test_a_sub_agent_transcript_gets_the_same_treatment(self):
        rows = [dict(user('Do the sweep', '2026-01-01T00:00:00Z', 'u1'), isSidechain=True),
                dict(notice_attachment('2026-01-01T00:00:04Z', 'att', 'u1'), isSidechain=True),
                dict(api_error('2026-01-01T00:00:05Z', 1), isSidechain=True)]
        path = self.write(rows, name='bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb')
        self.assertEqual(self.kinds(path), ['user', 'system_notification', 'api_error'])
        rendered = anchored_transcript.render_claude(path)
        self.assertIn('EVENT (subagent) TASK_NOTIFICATION:', rendered)
        self.assertIn('EVENT (subagent) API_ERROR:', rendered)

    def test_the_cli_context_and_follow_carry_the_same_markers(self):
        rows = [user('Start the work', '2026-01-01T00:00:00Z', 'u1'),
                enqueue(TYPED, '2026-01-01T00:00:05Z'),
                notice_attachment('2026-01-01T00:00:06Z', 'att', 'u1'),
                api_error('2026-01-01T00:00:07Z', 1),
                user('Carry on', '2026-01-01T00:00:20Z', 'u2', 'u1')]
        path = self.write(rows)
        rendered = cli.render_context(path)
        for marker in ('QUEUED_INPUT (delivery not confirmed)',
                       'EVENT TASK_NOTIFICATION:',
                       'API_ERROR: Connection dropped (ECONNRESET)'):
            self.assertIn(marker, rendered, marker)
        self.assertIn('[U2] [L5] USER', rendered)
        self.assertEqual(rendered.count('] USER'), 2)
        # Following from a cursor keeps the markers for the lines it newly covers.
        tail = cli.render_context(path, after_line=4)
        self.assertIn('API_ERROR:', tail)
        self.assertNotIn('QUEUED_INPUT', tail)


class ClassifierTests(unittest.TestCase):
    """The predicates key on explicit client markers, never on what the text looks like."""

    def test_only_enqueue_records_carry_a_queue_entry(self):
        self.assertEqual(claude_events.enqueued_text(enqueue(TYPED, 'x')), TYPED)
        for operation in ('dequeue', 'remove'):
            row = dict(enqueue(TYPED, 'x'), operation=operation)
            self.assertIsNone(claude_events.enqueued_text(row))
        self.assertIsNone(claude_events.enqueued_text(dict(enqueue('   ', 'x'))))
        self.assertIsNone(claude_events.enqueued_text({'type': 'user'}))

    def test_an_injected_block_in_the_queue_is_not_human_text(self):
        self.assertTrue(claude_events.is_queued_human_text(TYPED))
        self.assertFalse(claude_events.is_queued_human_text(NOTICE))
        for prefix in ('<command-name>x</command-name>', '<local-command-stdout>x',
                       '<system-reminder>x</system-reminder>', '<bash-stdout>x',
                       '<teammate-message id="a">x'):
            self.assertFalse(claude_events.is_queued_human_text(prefix), prefix)

    def test_language_is_never_the_signal(self):
        """The 2026-09-20 audit found machine records in Chinese and human ones in English."""
        self.assertTrue(claude_events.is_queued_human_text('Ship it.'))
        self.assertFalse(claude_events.is_queued_human_text(
            NOTICE.replace('Fixture sweep', 'Fixture sweep (done)')))

    def test_only_the_machine_notification_mode_is_picked_up(self):
        row = notice_attachment('2026-01-01T00:00:00Z', 'att', 'u1')
        self.assertEqual(claude_events.notification_prompt(row), NOTICE)
        for field, value in (('commandMode', 'prompt'), ('type', 'file'), ('prompt', '')):
            broken = notice_attachment('2026-01-01T00:00:00Z', 'att', 'u1')
            broken['attachment'][field] = value
            self.assertIsNone(claude_events.notification_prompt(broken), field)
        self.assertIsNone(claude_events.notification_prompt(
            dict(notice_attachment('t', 'att', 'u1'), isMeta=True)))

    def test_an_api_error_label_prefers_the_display_field_then_the_code(self):
        self.assertEqual(claude_events.api_error_label(api_error('t', 1)),
                         'Connection dropped (ECONNRESET)')
        row = api_error('t', 1)
        row['error'].pop('formatted')
        self.assertEqual(claude_events.api_error_label(row), 'Connection error (ECONNRESET)')
        row['error'] = {}
        self.assertEqual(claude_events.api_error_label(row), 'API error')
        self.assertIsNone(claude_events.api_error_label({'type': 'system'}))
        self.assertIsNone(claude_events.api_error_label(
            dict(api_error('t', 1), subtype='stop_hook_summary')))

    def test_a_delivery_is_recognised_as_a_message_or_an_attachment_prompt(self):
        self.assertEqual(claude_events.delivered_text(user(TYPED, 't', 'u1')), TYPED)
        self.assertEqual(
            claude_events.delivered_text(notice_attachment('t', 'att', 'u1')), NOTICE)
        self.assertIsNone(claude_events.delivered_text(enqueue(TYPED, 't')))
        self.assertIsNone(claude_events.delivered_text(dict(user(TYPED, 't', 'u1'), isMeta=True)))

    def test_a_queue_entry_is_confirmed_in_the_order_it_was_typed(self):
        parked = {'a': [1, 2, 3], 'b': [4]}
        self.assertEqual(claude_events.confirmed_queue_entries(parked, {'a': 2}), [1, 2])
        self.assertEqual(claude_events.confirmed_queue_entries(parked, {}), [])
        self.assertEqual(sorted(claude_events.confirmed_queue_entries(
            parked, {'a': 9, 'b': 1})), [1, 2, 3, 4])

    def test_a_retry_run_reports_its_count_and_span_only_when_there_was_one(self):
        run = claude_events.ApiErrorRun()
        self.assertEqual(run.summary(), '')
        run.open('529 Overloaded', '2026-01-01T00:00:00Z', None)
        self.assertEqual(run.summary(), '529 Overloaded')
        run.extend('2026-01-01T00:00:45Z')
        self.assertEqual(run.summary(), '529 Overloaded — retried 2 times over 45s')
        run.extend('2026-01-01T01:30:00Z')
        self.assertEqual(run.summary(), '529 Overloaded — retried 3 times over 1h30m')
        self.assertTrue(run.matches('529 Overloaded'))
        self.assertFalse(run.matches('Connection dropped (ECONNRESET)'))
        run.clear()
        self.assertEqual(run.summary(), '')

    def test_an_unreadable_timestamp_drops_the_span_and_keeps_the_count(self):
        run = claude_events.ApiErrorRun()
        run.open('529 Overloaded', 'not-a-time', None)
        run.extend('also-not-a-time')
        self.assertEqual(run.summary(), '529 Overloaded — retried 2 times')


if __name__ == '__main__':
    unittest.main()
