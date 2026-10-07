"""Two leftovers of the human-turn rule: compaction summaries, and the previews and search.

1. The record the client marks ``isCompactSummary`` is its own account of the turns it
   compacted away. Every counter used to call it a human turn - they agreed, and were wrong
   together. It is now an event in the anchored transcript, a folded ``compact_summary``
   block in the reader, a ``## COMPACTION SUMMARY`` block in the export, and no turn anywhere.

2. Card previews, dashboard search and the CLI's message stream read message *content*
   through a helper that could not see the record. Harness text marked ``isMeta`` reached
   them as the person's words, and a person's message that arrived as blocks (a picture
   attached, plain text blocks, text beside a tool result) never reached them at all. They
   now ask the same record-level rule as everything else.

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
IMAGE = {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'AAAA'}}
BANNER = re.compile(r'^━+ \[U(\d+)\] \[L(\d+)\] USER', re.MULTILINE)
OPENING = ('This session is being continued from a previous conversation that ran out of '
           'context. The summary below covers the earlier portion of the conversation.')
SUMMARY = OPENING + '\n\nSummary:\n1. Primary Request: tidy the orchard fixtures.\n9. Next step: rename the quince module.'
HOOK_FEEDBACK = 'Stop hook feedback:\nThe marmalade checklist is incomplete.'


def user(content, n, **extra):
    return dict({'type': 'user', 'uuid': 'u%d' % n, 'parentUuid': None, 'sessionId': SID,
                 'cwd': CWD, 'timestamp': '2026-01-01T00:00:%02dZ' % n,
                 'message': {'role': 'user', 'content': content}}, **extra)


def summary(n, content=SUMMARY):
    """The record a client writes after a compaction, as the 88 observed ones are shaped."""
    return user(content, n, isCompactSummary=True, isVisibleInTranscriptOnly=True)


def boundary(n):
    return {'type': 'system', 'subtype': 'compact_boundary', 'uuid': 'b%d' % n,
            'parentUuid': None, 'logicalParentUuid': 'a%d' % (n - 1), 'sessionId': SID,
            'cwd': CWD, 'timestamp': '2026-01-01T00:00:%02dZ' % n,
            'content': 'Conversation compacted', 'compactMetadata': {'trigger': 'auto'}}


def assistant(value, n):
    return {'type': 'assistant', 'uuid': 'a%d' % n, 'parentUuid': None, 'sessionId': SID,
            'cwd': CWD, 'timestamp': '2026-01-01T00:00:%02dZ' % n,
            'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': value}],
                        'stop_reason': 'end_turn'}}


class Fixture(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        env = patch.dict('os.environ',
                         {'SESSION_LOGBOOK_HISTORY_INDEX': str(self.dir / 'index.sqlite')})
        env.start()
        self.addCleanup(env.stop)
        cache = patch.object(server, 'SCAN_CACHE_FILE', self.dir / 'scan-cache.json')
        cache.start()
        self.addCleanup(cache.stop)
        claude_history._MEMORY.clear()
        self.addCleanup(claude_history._MEMORY.clear)

    def write(self, rows):
        path = self.dir / (SID + '.jsonl')
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        claude_history._MEMORY.clear()
        return path

    def turns(self, path):
        return server.extract_conversation(path)['turns']

    def previews(self, path):
        return [m['text'] for m in server.extract_metadata(path)['recent_msgs'] if m['role'] == 'user']

    def search_hits(self, path, *terms):
        return server._search_session(path, [term.lower() for term in terms])

    def cli_user_messages(self, path):
        return [text for _, role, text in cli.iter_messages(path, 'claude') if role == 'user' and text]

    def assert_every_counter_says(self, path, expected_lines):
        rendered = anchored_transcript.render_claude(path)
        banners = BANNER.findall(rendered)
        self.assertEqual([int(line) for _, line in banners], expected_lines, rendered)
        self.assertEqual([int(number) for number, _ in banners],
                         list(range(1, len(expected_lines) + 1)), rendered)
        self.assertEqual([t['type'] for t in self.turns(path)].count('user'), len(expected_lines))
        self.assertEqual(server.extract_metadata(path)['user_turn_count'], len(expected_lines))
        seen = [record['_logbook_user_turn'] for _, record in claude_history.records(path)]
        self.assertEqual(seen[-1] if seen else 0, len(expected_lines))
        return rendered


class CompactionSummaryTests(Fixture):

    def session(self):
        return self.write([user('Tidy the orchard fixtures', 1), assistant('Working.', 2),
                           boundary(3), summary(4), user('Now rename the quince module', 5),
                           assistant('Renamed.', 6)])

    def test_a_compaction_summary_is_no_human_turn_in_any_counter(self):
        rendered = self.assert_every_counter_says(self.session(), [1, 5])
        self.assertIn('[L4]   ⚠ EVENT COMPACTION_SUMMARY (written by the client, %d chars):\n%s'
                      % (len(SUMMARY), SUMMARY), rendered)
        self.assertIsNone(re.search(r'\[L4\] USER', rendered))

    def test_the_reader_keeps_the_summary_as_its_own_folded_block(self):
        turns = self.turns(self.session())
        self.assertEqual([t['type'] for t in turns],
                         ['user', 'assistant', 'compact_summary', 'user', 'assistant'])
        self.assertEqual(turns[2]['text'], SUMMARY)

    def test_the_reader_does_not_cut_the_summary_down_to_a_harness_note(self):
        # What a reader opens it for - what was still pending - is at its end.
        ending = 'Next step: bottle the damson jam.'
        long_summary = OPENING + '\n' + 'x' * (server.CONV_HARNESS_NOTE_MAX * 20) + '\n' + ending
        path = self.write([user('Start', 1), boundary(2), summary(3, long_summary)])
        self.assertEqual(self.turns(path)[-1]['text'], long_summary)

    def test_the_export_labels_the_summary_and_keeps_it_whole(self):
        export = server.extract_transcript(self.session())
        self.assertIn('## COMPACTION SUMMARY\n' + SUMMARY, export)
        self.assertNotIn('## USER\n' + OPENING, export)
        self.assertEqual(export.count('## USER\n'), 2)

    def test_the_flag_decides_not_the_opening_sentence(self):
        # A person may type the sentence; and a client may one day reword its summary.
        path = self.write([user(OPENING + ' Is that what it said?', 1),
                           summary(2, 'A reworded account of the earlier turns.')])
        self.assert_every_counter_says(path, [1])
        self.assertEqual([t['type'] for t in self.turns(path)], ['user', 'compact_summary'])
        self.assertIsNone(claude_events.compact_summary_text(user(OPENING, 3)))

    def test_a_summary_written_as_blocks_is_the_same_event(self):
        path = self.write([user('Start', 1), summary(2, [{'type': 'text', 'text': SUMMARY}])])
        rendered = self.assert_every_counter_says(path, [1])
        self.assertIn('EVENT COMPACTION_SUMMARY', rendered)
        self.assertEqual(self.turns(path)[1], {'type': 'compact_summary', 'text': SUMMARY,
                                               'ts': '2026-01-01T00:00:02Z'})

    def test_after_the_compact_command_it_is_a_summary_and_the_next_message_is_the_persons(self):
        path = self.write([user('Start', 1), user('<command-name>/compact</command-name>', 2),
                           boundary(3), summary(4), user('Carry on with the pears', 5)])
        self.assert_every_counter_says(path, [1, 5])
        self.assertEqual([t['type'] for t in self.turns(path)], ['user', 'compact_summary', 'user'])

    def test_one_message_and_a_compaction_is_a_single_turn_session(self):
        path = self.write([user('Do the whole migration', 1), assistant('Working.', 2),
                           boundary(3), summary(4), assistant('Done.', 5)])
        meta = server.extract_metadata(path)
        self.assertEqual((meta['user_turn_count'], meta['single_turn']), (1, True))

    def test_the_summary_is_nobodys_words_in_previews_search_and_the_cli(self):
        # A compaction can open a new file, and then the summary is its first record.
        path = self.write([boundary(1), summary(2), user('Now rename the quince module', 3),
                           assistant('Renamed.', 4)])
        self.assertEqual(self.previews(path), ['Now rename the quince module'])
        self.assertTrue(server.extract_metadata(path)['recent_msgs'][0]['text'].startswith('Now rename'))
        self.assertEqual(self.search_hits(path, 'orchard'), [])
        self.assertTrue(self.search_hits(path, 'quince', 'module'))
        self.assertEqual(self.cli_user_messages(path), ['Now rename the quince module'])

    def test_a_history_cache_written_before_the_rule_is_rebuilt(self):
        path = self.session()
        self.assertEqual(claude_history.SCHEMA, 8)
        with patch.object(claude_history, 'SCHEMA', 7):
            claude_history._MEMORY.clear()
            with patch.object(claude_history, 'anchored_user_text',
                              lambda row: claude_text.user_record_text(row) if row.get('type') == 'user' else ''):
                stale = [r['_logbook_user_turn'] for _, r in claude_history.records(path)]
        self.assertEqual(stale[-1], 3)  # what an index written under the old rule holds
        claude_history._MEMORY.clear()
        fresh = [r['_logbook_user_turn'] for _, r in claude_history.records(path)]
        self.assertEqual(fresh[-1], 2)

    def test_the_scan_cache_schema_moved_with_the_card_count(self):
        self.assertEqual(server.CACHE_SCHEMA_VERSION, 15)


class PreviewAndSearchTests(Fixture):

    def test_a_harness_note_is_not_the_persons_words_anywhere(self):
        path = self.write([user(HOOK_FEEDBACK, 1, isMeta=True), user('Check the greengage list', 2),
                           assistant('Checked.', 3), user(HOOK_FEEDBACK, 4, isMeta=True)])
        self.assertEqual(self.previews(path), ['Check the greengage list'])
        self.assertEqual(self.search_hits(path, 'marmalade'), [])
        self.assertTrue(self.search_hits(path, 'greengage'))
        self.assertEqual(self.cli_user_messages(path), ['Check the greengage list'])

    def test_the_first_message_of_a_selected_branch_skips_a_harness_note(self):
        rows = [dict(user(HOOK_FEEDBACK, 1, isMeta=True), uuid='note'),
                dict(user('Plant the medlar', 2), uuid='start', parentUuid='note'),
                dict(assistant('Abandoned answer', 3), uuid='old', parentUuid='start'),
                dict(assistant('Current answer', 4), uuid='new', parentUuid='start'),
                {'type': 'last-prompt', 'leafUuid': 'new', 'sessionId': SID}]
        path = self.write(rows)
        self.assertEqual(claude_history.rewind_status(path)['status'], 'selected')
        self.assertEqual(self.previews(path), ['Plant the medlar'])

    def test_a_message_with_a_picture_is_previewed_and_found_by_its_words(self):
        path = self.write([user([{'type': 'text', 'text': 'Why is the mulberry header misaligned?'},
                                 IMAGE], 1), assistant('Looking.', 2)])
        self.assertEqual(self.previews(path), ['Why is the mulberry header misaligned? [image]'])
        hits = self.search_hits(path, 'mulberry')
        self.assertTrue(hits)
        self.assertEqual({snippet['role'] for snippet in hits}, {'you'})
        self.assertEqual(self.cli_user_messages(path), ['Why is the mulberry header misaligned?'])

    def test_the_image_placeholder_is_our_word_and_never_answers_a_search(self):
        path = self.write([user([{'type': 'text', 'text': 'Why is the mulberry header misaligned?'},
                                 IMAGE], 1), user([IMAGE], 2)])
        self.assertEqual(self.search_hits(path, 'image'), [])
        self.assertEqual(self.cli_user_messages(path), ['Why is the mulberry header misaligned?'])
        self.assertEqual(self.previews(path)[-1], '[image]')  # still a turn, still on the card

    def test_a_message_of_plain_text_blocks_is_the_persons(self):
        path = self.write([user([{'type': 'text', 'text': 'Prune the loganberry rows'}], 1)])
        self.assertEqual(self.previews(path), ['Prune the loganberry rows'])
        self.assertTrue(self.search_hits(path, 'loganberry'))
        self.assertEqual(self.cli_user_messages(path), ['Prune the loganberry rows'])

    def test_text_typed_beside_a_tool_result_is_previewed_and_found(self):
        path = self.write([user('Start', 1), user([
            {'type': 'tool_result', 'tool_use_id': 't1', 'content': 'exit 0'},
            {'type': 'text', 'text': 'Stop, use the elderflower branch'}], 2)])
        self.assertIn('Stop, use the elderflower branch', self.previews(path))
        self.assertTrue(self.search_hits(path, 'elderflower'))
        self.assertEqual(self.search_hits(path, 'exit'), [])

    def test_no_helper_is_left_that_judges_a_message_by_its_content_alone(self):
        # The content cannot show isMeta or isCompactSummary; a helper that takes only the
        # content is how both leaks happened. Its callers ask the record-level rule.
        self.assertFalse(hasattr(server, '_user_text'))
        for record in (user(HOOK_FEEDBACK, 1, isMeta=True), summary(2), 'not a record', None):
            self.assertEqual(claude_text.anchored_user_text(record), '')
            self.assertEqual(claude_text.human_turn_words(record), '')


if __name__ == '__main__':
    unittest.main()
