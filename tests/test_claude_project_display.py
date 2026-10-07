"""What a Claude Project worker shows, and who said it.

1. The person's message reaches a worker wrapped in a ``<wake>`` envelope. Search already
   matched only the messages inside it; every surface that *shows* the person's words -
   card preview, first message, CLI title, reader, export, anchored transcript - now shows
   them unwrapped too. Whether the record is a human turn is still decided on the record
   as written.

2. A Project's coordinator is another Claude session. Its notes arrive under the user role
   as ``<project_claude_message>`` (older ones as a bare ``<relay>``), and the record says
   itself that nobody typed it. Such a note is a teammate-style event, not a human turn.

3. A worker the coordinator checked in on still took more than its head message, so it is
   not a one-shot run even when the person spoke to it once: it stays out of the suspected
   filter in the CLI and the dashboard, as it was while the notes counted as turns.

Every fixture here is synthetic.
"""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server
import session_logbook_cli as cli
from sources import anchored_transcript, claude_events, claude_history
from sources.claude_text import anchored_user_text, human_turn_text, human_turn_words

SID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
CWD = '/Users/alice/my-app'
ASK = 'Which launch checklist items are still open & who owns them?'
NOTE = 'Check the <gate> folder & report back'
BANNER = re.compile(r'^━+ \[U(\d+)\] \[L(\d+)\] USER', re.MULTILINE)


def escape(text):
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def wake(*bodies):
    messages = ''.join(
        '    <message trigger="true" from="human" trust="principal" author-id="user_0"'
        f' id="cmsg_{i}" sent-at="2026-01-01T00:00:00Z" mention="true">{escape(body)}</message>\n'
        for i, body in enumerate(bodies))
    return ('<wake reason="mention" current-time="2026-01-01T00:00:00Z">\n'
            '  <project id="chan_0" type="project">\n' + messages + '  </project>\n</wake>\n')


def relay(note, sender='coordinator'):
    return (f'<relay from="{sender}" session="session_0" current-time="2026-01-01T00:00:00Z">\n'
            '  The note below was written by the coordinator session, a Claude session, '
            'not by your user.\n'
            f'  <note>\n      {escape(note)}\n  </note>\n</relay>\n')


def project_message(note):
    return ('<project_claude_message session="session_0" thread_id="cmsg_0">\n'
            + relay(note) + '</project_claude_message>\n')


def record(n, content, **extra):
    return dict({'type': 'user', 'uuid': 'u%d' % n, 'parentUuid': None, 'sessionId': SID,
                 'cwd': CWD, 'timestamp': '2026-01-01T00:00:%02dZ' % n,
                 'message': {'role': 'user', 'content': content}}, **extra)


def person(n, *bodies):
    """A delivered wake, shaped as the observed ones are."""
    return record(n, wake(*bodies), origin={'kind': 'human'}, turnOrigin='human',
                  promptSource='sdk', projectsUserTurn=True, verifiedSlackHumanTurn=True)


def spawn_brief(n, note):
    """The client's own copy of the brief a coordinator hands a worker it spawns."""
    content = relay(note).replace('<relay from=', '<relay reason="spawn" from=', 1)
    return record(n, content, isMeta=True, turnOrigin='system', promptSource='system')


def coordinator(n, content):
    """A delivered coordinator note, shaped as the observed ones are."""
    return record(n, content, origin={'kind': 'task-notification', 'subkind': 'projects-relay'},
                  turnOrigin='peer', promptSource='system', queueSkipAttachments=True)


def reply(n, text):
    return {'type': 'assistant', 'uuid': 'a%d' % n, 'parentUuid': None, 'sessionId': SID,
            'cwd': CWD, 'timestamp': '2026-01-01T00:00:%02dZ' % n,
            'message': {'role': 'assistant', 'stop_reason': 'end_turn',
                        'content': [{'type': 'text', 'text': text}]}}


class Fixture(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        for patcher in (patch.dict('os.environ',
                                   {'SESSION_LOGBOOK_HISTORY_INDEX': str(self.dir / 'index.sqlite')}),
                        patch.object(server, 'SCAN_CACHE_FILE', self.dir / 'scan-cache.json'),
                        patch.object(server, '_cache', {}),
                        patch.object(server, '_state', {}),
                        patch.object(server, '_state_loaded', True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        claude_history._MEMORY.clear()
        self.addCleanup(claude_history._MEMORY.clear)

    def write(self, rows):
        path = self.dir / (SID + '.jsonl')
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        claude_history._MEMORY.clear()
        return path

    def worker(self):
        """One message from the person, then the coordinator twice, in both wrappings."""
        return self.write([person(1, ASK), reply(2, 'Looking into it.'),
                           coordinator(3, project_message(NOTE)), reply(4, 'Checked.'),
                           coordinator(5, relay('Any news on the pricing page?')),
                           reply(6, 'Still waiting.')])

    def previews(self, path):
        return [m['text'] for m in server.extract_metadata(path)['recent_msgs'] if m['role'] == 'user']


class WakeEnvelopeDisplayTests(Fixture):

    def test_the_verdict_is_taken_on_the_record_as_written(self):
        row = person(1, ASK)
        self.assertTrue(anchored_user_text(row).lstrip().startswith('<wake'))
        self.assertEqual(human_turn_text(row), ASK)
        self.assertEqual(human_turn_text(person(2, 'first', 'second')), 'first\n\nsecond')
        self.assertEqual(human_turn_text(record(3, 'plain words')), 'plain words')
        self.assertEqual(human_turn_text(record(4, 'x', isMeta=True)), '')

    def test_the_card_shows_what_the_person_typed(self):
        self.assertEqual(self.previews(self.worker()), [ASK])

    def test_the_first_message_read_from_the_file_head_is_unwrapped(self):
        # Four messages: the tail keeps the last three, so the first comes from the head.
        rows = [person(n, 'Message number %d & more' % n) for n in range(1, 5)]
        recent = server.extract_metadata(self.write(rows))['recent_msgs']
        self.assertTrue(recent[0]['is_first'])
        self.assertEqual([m['text'] for m in recent],
                         ['Message number %d & more' % n for n in range(1, 5)])

    def test_the_first_message_of_a_selected_branch_is_unwrapped(self):
        rows = [dict(person(n, 'Branch message %d' % n), uuid='w%d' % n,
                     parentUuid='w%d' % (n - 1) if n > 1 else None) for n in range(1, 5)]
        rows += [dict(reply(5, 'Abandoned answer'), uuid='old', parentUuid='w4'),
                 dict(reply(6, 'Current answer'), uuid='new', parentUuid='w4'),
                 {'type': 'last-prompt', 'leafUuid': 'new', 'sessionId': SID}]
        path = self.write(rows)
        self.assertEqual(claude_history.rewind_status(path)['status'], 'selected')
        self.assertEqual(self.previews(path), ['Branch message %d' % n for n in range(1, 5)])

    def test_the_cli_title_is_the_persons_message(self):
        path = self.worker()
        with patch.object(cli, '_candidate_paths', lambda source, include_subagents: [path]):
            rows = cli.recent_sessions(since=None, include_suspected=True)
        self.assertEqual([row['title'] for row in rows], [ASK])

    def test_the_reader_shows_the_persons_message(self):
        turns = server.extract_conversation(self.worker())['turns']
        self.assertEqual([t['text'] for t in turns if t['type'] == 'user'], [ASK])

    def test_the_export_shows_the_persons_message(self):
        export = server.extract_transcript(self.worker())
        self.assertEqual(export.count('## USER\n'), 1)
        self.assertIn('## USER\n' + ASK + '\n', export)
        self.assertNotIn('<wake', export)

    def test_the_anchored_transcript_shows_the_persons_message(self):
        rendered = anchored_transcript.render_claude(self.worker())
        self.assertEqual(BANNER.findall(rendered), [('1', '1')])
        self.assertIn('USER 2026-01-01T00:00:01 ━━━━━━━━━━\n' + ASK + '\n', rendered)
        self.assertNotIn('<wake', rendered)

    def test_typed_text_that_mentions_a_wake_is_left_alone(self):
        typed = 'Why does the <wake> tag show up in my titles?'
        self.assertEqual(self.previews(self.write([record(1, typed)])), [typed])


class CoordinatorNoteTests(Fixture):

    def test_a_coordinator_note_is_not_a_human_turn_in_either_wrapping(self):
        for content in (project_message(NOTE), relay(NOTE)):
            row = coordinator(1, content)
            self.assertEqual(anchored_user_text(row), '', content)
            self.assertEqual(human_turn_text(row), '', content)
            self.assertEqual(human_turn_words(row), '', content)

    def test_every_counter_counts_only_the_person(self):
        path = self.worker()
        meta = server.extract_metadata(path)
        self.assertEqual(meta['user_turn_count'], 1)
        self.assertEqual([r['_logbook_user_turn'] for _, r in claude_history.records(path)][-1], 1)
        self.assertEqual(len(BANNER.findall(anchored_transcript.render_claude(path))), 1)

    def test_the_reader_shows_a_note_as_the_coordinator_speaking(self):
        turns = server.extract_conversation(self.worker())['turns']
        notes = [t['text'] for t in turns if t['type'] == 'teammate_message']
        self.assertEqual(notes, ['[coordinator] ' + NOTE,
                                 '[coordinator] Any news on the pricing page?'])

    def test_the_export_and_the_anchored_transcript_label_the_note(self):
        path = self.worker()
        self.assertIn('## TEAMMATE\n[coordinator] ' + NOTE + '\n', server.extract_transcript(path))
        rendered = anchored_transcript.render_claude(path)
        self.assertIn('[L3]   ⚠ EVENT TEAMMATE_MESSAGE: [coordinator] ' + NOTE, rendered)
        self.assertIn('[L5]   ⚠ EVENT TEAMMATE_MESSAGE: [coordinator] Any news', rendered)
        self.assertNotIn('<project_claude_message', rendered)
        self.assertNotIn('not by your user', rendered)

    def test_a_note_is_nobodys_words_in_previews_search_and_the_cli(self):
        path = self.worker()
        self.assertEqual(self.previews(path), [ASK])
        self.assertEqual(server._search_session(path, ['gate']), [])
        self.assertEqual(server._search_session(path, ['pricing']), [])
        self.assertTrue(server._search_session(path, ['checklist']))
        said = [text for _, _, text in cli.iter_messages(path, 'claude')]
        self.assertFalse([text for text in said if 'gate' in text or 'pricing' in text], said)

    def test_a_queued_note_is_not_announced_as_queued_input(self):
        later = project_message('Undelivered check-in about the quince report')
        self.assertFalse(claude_events.is_queued_human_text(later))
        path = self.write([person(1, ASK),
                           {'type': 'queue-operation', 'operation': 'enqueue', 'sessionId': SID,
                            'timestamp': '2026-01-01T00:00:02Z', 'content': later}])
        self.assertEqual(server._search_session(path, ['quince']), [])
        self.assertNotIn('quince', anchored_transcript.render_claude(path))

    def test_a_note_without_a_note_body_keeps_its_text(self):
        event = claude_events.parse_system_user_event('<relay from="lead" session="s">Ping &amp; go</relay>')
        self.assertEqual(event, {'type': 'teammate_message', 'text': '[lead] Ping & go'})
        bare = claude_events.parse_project_relay('<project_claude_message session="s"></project_claude_message>')
        self.assertEqual(bare, '[coordinator] (empty)')

    def test_a_person_typing_about_relays_still_has_a_turn(self):
        for typed in ('Please <relay> this to the team', 'relay from the coordinator?'):
            self.assertEqual(anchored_user_text(record(1, typed)), typed)


class CheckInTests(Fixture):

    def recent(self, path, **options):
        with patch.object(cli, '_candidate_paths', lambda source, include_subagents: [path]):
            return cli.recent_sessions(since=None, **options)

    def test_a_worker_the_coordinator_checked_in_on_stays_listed(self):
        path = self.worker()
        self.assertFalse(server.extract_metadata(path)['single_turn'])
        self.assertEqual([(row['title'], row['selection_group']) for row in self.recent(path)],
                         [(ASK, 'primary')])

    def test_one_message_and_no_check_in_is_still_a_single_turn_run(self):
        path = self.write([person(1, ASK), reply(2, 'Done.')])
        self.assertEqual(server.extract_metadata(path)['single_turn'], True)
        self.assertEqual(self.recent(path), [])

    def test_a_spawn_brief_opens_the_session_and_is_no_check_in(self):
        path = self.write([spawn_brief(1, 'Own the pricing page.'), person(2, ASK), reply(3, 'Done.')])
        meta = server.extract_metadata(path)
        self.assertEqual((meta['user_turn_count'], meta['single_turn']), (1, True))
        # The reader shows the brief as the coordinator speaking, as it shows a check-in.
        self.assertEqual(server.extract_conversation(path)['turns'][0],
                         {'type': 'teammate_message', 'text': '[coordinator] Own the pricing page.',
                          'ts': '2026-01-01T00:00:01Z'})

    def test_a_check_in_before_the_tail_is_found_in_a_large_file(self):
        filler = [reply(n, 'Working through item %d.' % n) for n in range(3, 40)]
        with patch.object(server, 'TAIL_BUFFER', 400):
            checked = self.write([person(1, ASK), coordinator(2, project_message(NOTE))] + filler)
            meta = server.extract_metadata(checked)
            self.assertEqual((meta['user_turn_count'], meta['single_turn']), (2, False))
            plain = self.write([person(1, ASK), reply(2, 'Started.')] + filler)
            meta = server.extract_metadata(plain)
            self.assertEqual((meta['user_turn_count'], meta['single_turn']), (2, True))

    @unittest.skipUnless(shutil.which('node'), 'Node.js is needed to run the dashboard function')
    def test_the_dashboard_does_not_suspect_a_checked_in_worker(self):
        html = (Path(server.__file__).with_name('index.html')).read_text(encoding='utf-8')
        found = re.search(r'function isSuspected\(m\) \{.*?\n\}', html, re.DOTALL)
        self.assertIsNotNone(found)
        cases = [{'user_turn_count': 1, 'single_turn': False},
                 {'user_turn_count': 1, 'single_turn': True},
                 {'user_turn_count': 1},
                 {'user_turn_count': 1, 'single_turn': True, 'human_confirmed': True},
                 {'user_turn_count': 2, 'single_turn': True}]
        script = found.group(0) + '\nconsole.log(JSON.stringify(%s.map(isSuspected)));' % json.dumps(cases)
        result = subprocess.run([shutil.which('node'), '-e', script], capture_output=True, text=True,
                                timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [False, True, True, False, False])


if __name__ == '__main__':
    unittest.main()
