"""A Claude Project worker is searchable by what the person and the worker said.

The worker is woken by a channel envelope and answers through channel tools, so both
sides of the conversation sit where a plain Claude session does not keep them.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server
import session_logbook_cli as cli
from sources import claude_events
from sources.claude_text import assistant_search_words, human_turn_words, project_wake_words

SID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
ASK = 'Which launch checklist items are still open & who owns them?'
REPLY = 'Three checklist items remain; the billing review is the blocker.'
EDIT = 'Update: the billing review is done, two items remain.'


def wake(*bodies):
    messages = ''.join(
        '    <message trigger="true" from="human" trust="principal" author-id="user_0"'
        f' id="cmsg_{i}" sent-at="2026-01-01T00:00:00Z" mention="true">{body}</message>\n'
        for i, body in enumerate(bodies))
    return ('<wake reason="mention" current-time="2026-01-01T00:00:00Z">\n'
            '  <project id="chan_0" type="project">\n' + messages + '  </project>\n</wake>\n')


def assistant(*blocks, uuid='a'):
    return {'type': 'assistant', 'uuid': uuid, 'sessionId': SID,
            'timestamp': '2026-01-01T00:01:00Z',
            'message': {'role': 'assistant', 'content': list(blocks)}}


def tool(name, **inputs):
    return {'type': 'tool_use', 'id': 'toolu_' + name, 'name': name, 'input': inputs}


class ProjectWorkerSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / (SID + '.jsonl')
        env = patch.dict('os.environ', {'SESSION_LOGBOOK_HISTORY_INDEX': str(self.path.parent / 'index.sqlite')})
        env.start()
        self.addCleanup(env.stop)
        self.human = {'type': 'user', 'uuid': 'u', 'sessionId': SID, 'cwd': '/Users/alice/my-app',
                      'timestamp': '2026-01-01T00:00:00Z', 'projectsUserTurn': True,
                      'message': {'role': 'user',
                                  'content': wake(ASK.replace('&', '&amp;'))}}
        self.rows = [
            self.human,
            assistant({'type': 'thinking', 'thinking': 'private reasoning about kiwi'},
                      tool('mcp__hearthbot__update_status', text='status board mango'),
                      tool('mcp__hearthbot__send_message', message='to another agent papaya'),
                      tool('mcp__hearthbot__reply', text=REPLY), uuid='a1'),
            assistant({'type': 'text', 'text': 'Waiting on the agents.'},
                      tool('mcp__hearthbot__update_message', message_id='cmsg_1', text=EDIT),
                      uuid='a2'),
        ]
        self.path.write_text(''.join(json.dumps(row) + '\n' for row in self.rows))

    def search(self, query):
        return server._search_session(self.path, query.lower().split())

    def test_worker_reply_and_edit_are_found(self):
        hits = self.search('billing blocker')
        self.assertTrue(hits)
        self.assertIn('billing review is the blocker', hits[0]['text'])
        self.assertEqual(hits[0]['role'], '')
        self.assertTrue(self.search('two items remain'))

    def test_other_worker_output_stays_out(self):
        for word in ('kiwi', 'mango', 'papaya'):
            self.assertEqual(self.search(word), [], word)

    def test_person_message_is_found_without_its_envelope(self):
        hits = self.search('open & who owns')
        self.assertTrue(hits)
        self.assertEqual(hits[0]['role'], 'you')
        self.assertNotIn('<message', hits[0]['text'])
        # Envelope attributes are transport, not something anyone said.
        self.assertEqual(self.search('mention'), [])
        self.assertEqual(self.search('principal'), [])

    def test_repeated_reply_text_gives_one_snippet(self):
        self.rows.append(assistant(tool('mcp__hearthbot__update_message', message_id='cmsg_0', text=REPLY),
                                   uuid='a3'))
        self.path.write_text(''.join(json.dumps(row) + '\n' for row in self.rows))
        hits = self.search('blocker')
        self.assertEqual(len(hits), 1)

    def test_undelivered_queued_wake_is_found_without_its_envelope(self):
        later = wake('Also add the pricing page to the checklist')
        self.rows.append({'type': 'queue-operation', 'operation': 'enqueue', 'sessionId': SID,
                          'timestamp': '2026-01-01T00:02:00Z', 'content': later})
        self.path.write_text(''.join(json.dumps(row) + '\n' for row in self.rows))
        hits = self.search('pricing')
        self.assertEqual(len(hits), 1)
        self.assertNotIn('<wake', hits[0]['text'])
        self.assertEqual(self.search('principal'), [])
        cli_rows = [row for row in cli.iter_messages(self.path, 'claude') if 'pricing' in (row[2] or '')]
        self.assertEqual([row[2] for row in cli_rows], ['Also add the pricing page to the checklist'])

    def test_cli_search_reads_the_same_words(self):
        self.assertEqual(cli._message_from_row(self.human, 'claude'), ('user', ASK))
        self.assertEqual(cli._message_from_row(self.rows[1], 'claude'), ('assistant', REPLY))

    def test_wake_with_several_messages_keeps_each(self):
        self.assertEqual(project_wake_words(wake('first', 'second &lt;b&gt;')), 'first\n\nsecond <b>')

    def test_plain_text_is_untouched(self):
        for text in ('a <wake> word typed mid-sentence', '<wakeful> is not an envelope', ''):
            self.assertEqual(project_wake_words(text), text)
        # An envelope with no message falls back to its own text rather than to nothing.
        empty = '<wake reason="x"></wake>'
        self.assertEqual(project_wake_words(empty), empty)

    def test_relay_from_coordinator_is_not_a_human_turn(self):
        relay = {'type': 'user', 'isMeta': True,
                 'message': {'role': 'user', 'content': '<relay from="coordinator">brief</relay>'}}
        self.assertEqual(human_turn_words(relay), '')

    def test_assistant_words_ignore_malformed_blocks(self):
        self.assertEqual(assistant_search_words(None), '')
        self.assertEqual(assistant_search_words(['x', tool('mcp__hearthbot__reply', text=None)]), '')


if __name__ == '__main__':
    unittest.main()


THREAD = 'cmsg_0aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'


class ProjectThreadIdSearchTests(unittest.TestCase):
    """A Project thread's id lives only in envelope attributes, yet it names the worker."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / (SID + '.jsonl')
        env = patch.dict('os.environ', {'SESSION_LOGBOOK_HISTORY_INDEX': str(self.path.parent / 'index.sqlite')})
        env.start()
        self.addCleanup(env.stop)

    def write(self, *rows):
        self.path.write_text(''.join(json.dumps(row) + '\n' for row in rows))

    def user(self, content):
        return {'type': 'user', 'uuid': 'u', 'sessionId': SID, 'cwd': '/Users/alice/my-app',
                'timestamp': '2026-01-01T00:00:00Z', 'message': {'role': 'user', 'content': content}}

    def threaded_wake(self, body):
        return wake(body).replace('<project id="chan_0" type="project">\n',
                                  f'<project id="chan_0" type="project">\n  <thread ts="{THREAD}">\n', 1)

    def search(self, query):
        return server._search_session(self.path, query.lower().split())

    def test_thread_id_in_a_wake_finds_the_worker(self):
        self.write(self.user(self.threaded_wake(ASK.replace('&', '&amp;'))),
                   assistant(tool('mcp__hearthbot__reply', text=REPLY)))
        hits = self.search(THREAD)
        self.assertTrue(hits)
        self.assertEqual(hits[0]['text'], 'Project id: ' + THREAD)
        # Case does not matter, and the id combines with words like any other term.
        self.assertTrue(self.search(THREAD.upper() + ' billing'))
        self.assertEqual(self.search(THREAD + ' papaya'), [])

    def test_message_id_in_a_wake_finds_the_worker(self):
        self.write(self.user(wake('hello there')))
        self.assertTrue(self.search('cmsg_0'))

    def test_thread_id_in_a_coordinator_relay_finds_the_worker(self):
        relay = (f'<project_claude_message session="s" thread_id="{THREAD}">\n'
                 '<relay from="coordinator" session="s" current-time="2026-01-01T00:00:00Z">\n'
                 'The note below was written by the coordinator session, a Claude session, not by your user.\n'
                 '<note>check in</note>\n</relay>\n</project_claude_message>')
        self.write(self.user(relay))
        self.assertTrue(self.search(THREAD))

    def test_partial_id_or_typed_id_is_not_an_id_hit(self):
        self.write(self.user(self.threaded_wake('hello there')))
        # Every Project id starts with cmsg_; a prefix must not match every worker.
        self.assertEqual(self.search('cmsg'), [])
        self.assertEqual(self.search(THREAD[:-3]), [])
        # An id typed into an ordinary prompt is found as typed words, not as an envelope id.
        self.write(self.user(f'please look at <thread ts="{THREAD}"> later'))
        hits = self.search(THREAD)
        self.assertTrue(hits)
        self.assertEqual(hits[0]['role'], 'you')

    def test_cli_search_finds_the_thread_id(self):
        self.write(self.user(self.threaded_wake('hello there')))
        self.assertEqual(cli._project_id_terms(self.path, [THREAD.upper().lower(), 'hello']), {THREAD})
        self.assertEqual(cli._project_id_terms(self.path, ['cmsg']), set())


class ProjectHarnessQueueTests(unittest.TestCase):
    """What the channel queues for a worker without anyone typing it is not the person's text."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / (SID + '.jsonl')
        env = patch.dict('os.environ', {'SESSION_LOGBOOK_HISTORY_INDEX': str(self.path.parent / 'index.sqlite')})
        env.start()
        self.addCleanup(env.stop)

    def queue_then_meta(self, content):
        rows = [{'type': 'queue-operation', 'operation': 'enqueue', 'sessionId': SID,
                 'timestamp': '2026-01-01T00:00:00Z', 'content': content},
                {'type': 'queue-operation', 'operation': 'dequeue', 'sessionId': SID,
                 'timestamp': '2026-01-01T00:00:01Z'},
                {'type': 'user', 'uuid': 'u', 'sessionId': SID, 'isMeta': True,
                 'timestamp': '2026-01-01T00:00:01Z', 'message': {'role': 'user', 'content': content}}]
        self.path.write_text(''.join(json.dumps(row) + '\n' for row in rows))

    def test_session_context_block_is_not_queued_text(self):
        self.queue_then_meta('<session-context nonce="n">\nProject: launch plan\n</session-context>')
        self.assertEqual(server._search_session(self.path, ['launch']), [])

    def test_status_wake_is_not_queued_text(self):
        self.queue_then_meta('<wake reason="device-folder-thread-status" current-time="x">\n'
                             f'  <project id="chan_0" type=""><thread ts="{THREAD}"></thread></project>\n'
                             '  <system-note>status=ready folder connected</system-note>\n</wake>')
        self.assertEqual(server._search_session(self.path, ['folder']), [])

    def test_queued_wake_with_a_message_is_still_the_person(self):
        text = wake('please check the pricing page')
        self.assertTrue(claude_events.is_queued_human_text(text))
