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
