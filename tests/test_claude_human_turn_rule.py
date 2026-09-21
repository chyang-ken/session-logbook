"""One rule decides what a human turn is, and every counter asks it.

The reader's ``user`` turns, the card's ``user_turn_count``, the history index's
``user_turn`` and the anchored transcript's ``[U#]`` used to answer "did a person say
this?" with four separate pieces of code, and they drifted. These tests hold them to the
single rule in ``sources.claude_text.anchored_user_text``:

* the interrupt marker the client writes on Esc is an event, never a turn;
* a record the client marks ``isMeta`` is never a turn - not the "[Image: source: ...]"
  note after a pasted image, not hook feedback - and a message that is only an image is one;
* a person's message stays theirs after a slash command that announces no body, and when
  it shares a record with a tool result.

Every fixture here is synthetic.
"""
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server
import session_logbook_cli as cli
from sources import anchored_transcript, claude_events, claude_history, claude_text

SID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
CWD = '/Users/alice/my-app'
INTERRUPT = '[Request interrupted by user]'
INTERRUPT_TOOL = '[Request interrupted by user for tool use]'
IMAGE = {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'AAAA'}}
IMAGE_NOTE = '[Image: source: /tmp/example/pasted-1.png]'
BANNER = re.compile(r'^━+ \[U(\d+)\] \[L(\d+)\] USER', re.MULTILINE)


def user(content, n, **extra):
    """A user-role record. ``content`` is a string or a list of blocks; ``n`` orders it."""
    return dict({'type': 'user', 'uuid': 'u%d' % n, 'parentUuid': None, 'sessionId': SID,
                 'cwd': CWD, 'timestamp': '2026-01-01T00:00:%02dZ' % n,
                 'message': {'role': 'user', 'content': content}}, **extra)


def text(value):
    return [{'type': 'text', 'text': value}]


def assistant(blocks, n):
    if isinstance(blocks, str):
        blocks = [{'type': 'text', 'text': blocks}]
    return {'type': 'assistant', 'uuid': 'a%d' % n, 'parentUuid': None, 'sessionId': SID,
            'cwd': CWD, 'timestamp': '2026-01-01T00:00:%02dZ' % n,
            'message': {'role': 'assistant', 'content': blocks, 'stop_reason': 'end_turn'}}


class HumanTurnRuleTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        env = patch.dict('os.environ',
                         {'SESSION_LOGBOOK_HISTORY_INDEX': str(self.dir / 'index.sqlite')})
        env.start()
        self.addCleanup(env.stop)
        # The CLI resolves conversation facts through the scan cache; keep it off the
        # developer's real one.
        cache = patch.object(server, 'SCAN_CACHE_FILE', self.dir / 'scan-cache.json')
        cache.start()
        self.addCleanup(cache.stop)
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

    def assert_every_counter_says(self, path, expected_lines):
        """The reader, the card, the history index and [U#] all count the same records.

        ``expected_lines`` are the physical lines that are human turns. The anchored
        transcript is the one artifact that names its lines, so it is checked exactly; the
        other three are checked by count, and [U#] must run 1..n without a gap.
        """
        rendered = anchored_transcript.render_claude(path)
        banners = BANNER.findall(rendered)
        self.assertEqual([int(line) for _, line in banners], expected_lines, rendered)
        self.assertEqual([int(number) for number, _ in banners],
                         list(range(1, len(expected_lines) + 1)), rendered)
        self.assertEqual(self.kinds(path).count('user'), len(expected_lines))
        self.assertEqual(server.extract_metadata(path)['user_turn_count'], len(expected_lines))
        seen = [record['_logbook_user_turn'] for _, record in claude_history.records(path)]
        self.assertEqual(seen[-1] if seen else 0, len(expected_lines))
        return rendered

    # ---------- 1. the interrupt marker is an event ----------

    def test_an_interrupt_marker_is_an_event_in_every_artifact(self):
        for marker, wording in ((INTERRUPT, 'Request interrupted by user'),
                                (INTERRUPT_TOOL, 'Request interrupted by user for tool use')):
            with self.subTest(marker=marker):
                path = self.write([user('Start the work', 1), assistant('On it.', 2),
                                   user(text(marker), 3), user('Try the other way', 4)])
                rendered = self.assert_every_counter_says(path, [1, 4])
                self.assertIn('[L3]   ⚠ EVENT INTERRUPTED: ' + wording, rendered)
                turn = self.turns(path)[2]
                self.assertEqual((turn['type'], turn['text']), ('system_notification', wording))
                export = server.extract_transcript(path)
                self.assertIn('## NOTIFICATION\n' + wording, export)
                self.assertNotIn('## USER\n[Request', export)

    def test_an_interrupt_marker_written_as_a_plain_string_is_the_same_event(self):
        path = self.write([user('Start the work', 1), user(INTERRUPT, 2)])
        rendered = self.assert_every_counter_says(path, [1])
        self.assertIn('EVENT INTERRUPTED:', rendered)
        self.assertEqual(self.kinds(path), ['user', 'system_notification'])
        self.assertTrue(server.extract_metadata(path)['single_turn'])
        previews = [m['text'] for m in server.extract_metadata(path)['recent_msgs']]
        self.assertNotIn(INTERRUPT, previews)

    def test_a_person_quoting_the_marker_keeps_their_turn(self):
        for typed in (INTERRUPT + ' - why does this line keep showing up?',
                      'It printed ' + INTERRUPT,
                      '[Request interrupted by user\nand then a second line]'):
            with self.subTest(typed=typed):
                path = self.write([user('Start the work', 1), user(text(typed), 2)])
                self.assert_every_counter_says(path, [1, 2])
                self.assertEqual(self.kinds(path), ['user', 'user'])

    def test_the_marker_pattern_accepts_a_future_wording_but_only_as_the_whole_text(self):
        self.assertEqual(claude_text.interrupt_marker_text('  [Request interrupted by user for plan]\n'),
                         'Request interrupted by user for plan')
        self.assertEqual(claude_text.interrupt_marker_text('[Request interrupted by user] carry on'), '')
        self.assertEqual(claude_text.interrupt_marker_text('[Request interrupted]'), '')
        self.assertEqual(claude_text.interrupt_marker_text(None), '')
        self.assertFalse(claude_events.is_queued_human_text(INTERRUPT))

    # ---------- 2. an image is a human turn; the client's note about it is not ----------

    def test_an_image_only_message_is_one_human_turn_everywhere(self):
        path = self.write([user([IMAGE], 1), assistant('I see a chart.', 2)])
        rendered = self.assert_every_counter_says(path, [1])
        self.assertIn('USER 2026-01-01T00:00:01 ━━━━━━━━━━\n[image]', rendered)
        self.assertEqual(self.turns(path)[0]['text'], '[image]')
        self.assertTrue(server.extract_metadata(path)['single_turn'])

    def test_the_image_source_note_is_a_harness_note_not_a_second_turn(self):
        path = self.write([user([IMAGE, {'type': 'text', 'text': 'What is this?'}], 1),
                           user(text(IMAGE_NOTE), 2, isMeta=True),
                           assistant('A chart.', 3)])
        rendered = self.assert_every_counter_says(path, [1])
        self.assertNotIn('Image: source', rendered)
        turns = self.turns(path)
        self.assertEqual([t['type'] for t in turns], ['user', 'system_notification', 'assistant'])
        self.assertEqual(turns[0]['text'], '[image]\nWhat is this?')
        self.assertEqual((turns[1]['kind'], turns[1]['text']), ('harness_note', IMAGE_NOTE))
        self.assertIn('## SYSTEM\n' + IMAGE_NOTE, server.extract_transcript(path))

    def test_a_record_the_client_marks_meta_is_never_a_user_turn(self):
        feedback = 'Stop hook feedback: ' + 'keep going. ' * 60
        path = self.write([user('Start the work', 1), assistant('Done.', 2),
                           user(feedback, 3, isMeta=True), assistant('Continuing.', 4)])
        self.assert_every_counter_says(path, [1])
        note = self.turns(path)[2]
        self.assertEqual((note['type'], note['kind']), ('system_notification', 'harness_note'))
        self.assertLessEqual(len(note['text']), server.CONV_HARNESS_NOTE_MAX + 2)
        self.assertTrue(note['text'].startswith('Stop hook feedback: keep going.'))

    # ---------- 3. a person's message stays theirs ----------

    def test_a_message_after_a_command_with_no_body_is_the_persons_not_a_skill(self):
        path = self.write([user('<command-name>/clear</command-name>\n<command-message>clear</command-message>', 1),
                           user('Is the code clean? I want to change something.', 2),
                           assistant('Yes.', 3)])
        self.assert_every_counter_says(path, [2])
        self.assertEqual(self.kinds(path), ['user', 'assistant'])
        self.assertIn('## USER\nIs the code clean?', server.extract_transcript(path))

    def test_a_skill_body_is_still_a_skill_and_not_a_turn(self):
        path = self.write([user('<command-name>/review</command-name>', 1),
                           user(text('You are an expert reviewer. Review the diff.'), 2, isMeta=True),
                           assistant('Reviewing.', 3),
                           user('Thanks, ship it', 4)])
        self.assert_every_counter_says(path, [4])
        self.assertEqual(self.kinds(path), ['skill', 'assistant', 'user'])
        self.assertIn('## SKILL\nYou are an expert reviewer.', server.extract_transcript(path))

    def test_typed_text_sharing_a_record_with_a_tool_result_keeps_its_anchor(self):
        path = self.write([user('Start the work', 1),
                           assistant([{'type': 'tool_use', 'id': 't1', 'name': 'Bash',
                                       'input': {'command': 'ls'}}], 2),
                           user([{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'README.md'},
                                 {'type': 'text', 'text': 'Beware of over-design here.'}], 3)])
        rendered = self.assert_every_counter_says(path, [1, 3])
        self.assertIn('[L3]   ⮑ TOOL_RESULT OK:', rendered)
        self.assertIn('[U2] [L3] USER', rendered)
        self.assertLess(rendered.index('TOOL_RESULT OK'), rendered.index('[U2] [L3]'))
        self.assertEqual(self.kinds(path), ['user', 'tool', 'user'])

    def test_a_record_holding_only_tool_results_is_still_not_a_turn(self):
        path = self.write([user('Start the work', 1),
                           assistant([{'type': 'tool_use', 'id': 't1', 'name': 'Bash',
                                       'input': {'command': 'ls'}}], 2),
                           user([{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'README.md'}], 3)])
        self.assert_every_counter_says(path, [1])

    def test_reminder_wrapped_text_counts_on_the_card_too(self):
        wrapped = '<system-reminder>\nYou are in a worktree.\n</system-reminder>\nRename the module'
        path = self.write([user(wrapped, 1), assistant('Renamed.', 2)])
        rendered = self.assert_every_counter_says(path, [1])
        self.assertIn('Rename the module', rendered)
        self.assertNotIn('worktree', rendered)

    def test_a_reminder_with_nothing_after_it_and_blank_text_are_not_turns(self):
        path = self.write([user('<system-reminder>\nnothing typed\n</system-reminder>\n', 1),
                           user('   \n', 2), user(text('  '), 3), user('Real words', 4)])
        self.assert_every_counter_says(path, [4])

    def test_the_resume_prompt_is_an_event_only_when_the_client_wrote_it(self):
        sentence = 'Continue from where you left off.'
        path = self.write([user('Start the work', 1), user(text(sentence), 2, isMeta=True),
                           user(text(sentence), 3)])
        self.assert_every_counter_says(path, [1, 3])
        self.assertEqual(self.kinds(path), ['user', 'system_notification', 'user'])

    # ---------- 4. everything at once ----------

    def test_a_session_with_every_shape_counts_the_same_four_ways(self):
        rows = [
            user('<command-name>/clear</command-name>', 1),
            user('First real message', 2),                                     # U1
            assistant([{'type': 'tool_use', 'id': 't1', 'name': 'Bash',
                        'input': {'command': 'sleep 60'}}], 3),
            user([{'type': 'tool_result', 'tool_use_id': 't1', 'is_error': True,
                   'content': 'interrupted'}], 4),
            user(text(INTERRUPT_TOOL), 5),
            user([IMAGE], 6),                                                  # U2
            user(text(IMAGE_NOTE), 7, isMeta=True),
            user('<task-notification>\n<status>completed</status>\n'
                 '<summary>Agent "Fixture sweep" finished</summary>\n</task-notification>', 8),
            user('<bash-stdout>ok</bash-stdout><bash-stderr></bash-stderr>', 9),
            user('Another session sent a message: check the queue', 10, isMeta=True),
            user(text(INTERRUPT), 11),
            user([{'type': 'text', 'text': 'Look at both'}, IMAGE, IMAGE], 12),  # U3
            assistant('Looked.', 13),
        ]
        path = self.write(rows)
        rendered = self.assert_every_counter_says(path, [2, 6, 12])
        self.assertEqual(rendered.count('EVENT INTERRUPTED'), 2)
        self.assertEqual(self.kinds(path), [
            'user', 'tool', 'system_notification', 'user', 'system_notification',
            'system_notification', 'bash_output', 'system_notification',
            'system_notification', 'user', 'assistant'])
        context = cli.render_context(path)
        self.assertEqual(len(BANNER.findall(context)), 3)

    # ---------- 5. the cache that stores the answer is rebuilt ----------

    def test_a_history_cache_written_under_the_previous_schema_is_rebuilt(self):
        path = self.write([user('Start the work', 1), user(text(INTERRUPT), 2),
                           user('Carry on', 3)])
        self.assertEqual(claude_history.SCHEMA, 7)
        list(claude_history.records(path))
        with patch.object(claude_history, 'SCHEMA', 6):
            claude_history._MEMORY.clear()
            with patch.object(claude_history, 'anchored_user_text', lambda row: 'x'):
                stale = [r['_logbook_user_turn'] for _, r in claude_history.records(path)]
        self.assertEqual(stale, [1, 2, 3])  # what a cache from the old rule would hold
        claude_history._MEMORY.clear()
        fresh = [r['_logbook_user_turn'] for _, r in claude_history.records(path)]
        self.assertEqual(fresh, [1, 1, 2])


if __name__ == '__main__':
    unittest.main()
