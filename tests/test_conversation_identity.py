"""Synthetic tests for conversation identity: grouping, fail-open, and the state merge.

Every fixture here is invented. No real session id, project path or transcript content
appears in this file.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sources import claude_desktop as desktop  # noqa: E402
from sources import session_identity  # noqa: E402

R1 = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
R2 = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
R3 = 'cccccccc-cccc-cccc-cccc-cccccccccccc'
R4 = 'dddddddd-dddd-dddd-dddd-dddddddddddd'
R5 = 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee'
CONVERSATION = 'local_aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
OTHER_CONVERSATION = 'local_ffffffff-ffff-ffff-ffff-ffffffffffff'


class Fixture(unittest.TestCase):
    """A synthetic project directory plus a synthetic Desktop descriptor store."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.descriptors = self.root / 'descriptors'
        (self.descriptors / 'account' / 'organization').mkdir(parents=True)
        self.project = str(self.root / 'workspace')
        self.transcripts = self.root / 'transcripts'
        self.transcripts.mkdir()

    def card(self, record_id, **extra):
        path = self.transcripts / (record_id + '.jsonl')
        if not path.exists():
            path.write_text('{}\n')
        item = {'id': record_id, 'source': 'claude', 'project_path': self.project,
                'jsonl_path': str(path), 'mtime': 1.0, 'size': 10}
        item.update(extra)
        return item

    def descriptor(self, name='local_conversation.json', **fields):
        value = {'sessionId': CONVERSATION, 'cliSessionId': R3, 'cwd': self.project}
        value.update(fields)
        (self.descriptors / 'account' / 'organization' / name).write_text(json.dumps(value))
        return value

    def edge(self, parent, child):
        return {'parent': parent, 'child': child, 'forkPoint': 'record-uuid',
                'at': 100, 'cwd': self.project}

    def memberships(self):
        return desktop.descriptor_memberships(self.descriptors)

    def identify(self, items, links=()):
        return session_identity.conversation_identity(items, self.memberships(), links)

    def by_id(self, items, links=()):
        return {item['id']: item for item in self.identify(items, links)}

    def assert_all_separate(self, items, links=()):
        for record_id, item in self.by_id(items, links).items():
            self.assertEqual(item['conversation_id'], record_id)
            self.assertEqual(item['conversation_current_id'], record_id)
            self.assertEqual([r['id'] for r in item['conversation_records']], [record_id])


class DescriptorMembershipTests(Fixture):
    def test_rewind_edges_name_the_relation_rewind(self):
        self.descriptor(priorCliSessionIds=[R1, R2],
                        rewindEdges=[self.edge(R1, R2), self.edge(R2, R3)])
        membership, = self.memberships()
        self.assertEqual(membership['conversation_id'], CONVERSATION)
        self.assertEqual(membership['members'], [R1, R2, R3])
        self.assertEqual(membership['current'], R3)
        self.assertEqual(membership['relations'], {R2: 'rewind', R3: 'rewind'})

    def test_prior_ids_without_an_edge_are_continuation_not_rewind(self):
        self.descriptor(priorCliSessionIds=[R1, R2])
        membership, = self.memberships()
        self.assertEqual(membership['members'], [R1, R2, R3])
        self.assertEqual(membership['relations'], {R2: 'continuation', R3: 'continuation'})

    def test_one_rewind_among_resumes_is_labelled_separately(self):
        self.descriptor(priorCliSessionIds=[R1, R2], rewindEdges=[self.edge(R2, R3)])
        membership, = self.memberships()
        self.assertEqual(membership['relations'], {R2: 'continuation', R3: 'rewind'})

    def test_descriptor_without_priors_is_a_one_record_conversation(self):
        self.descriptor()
        membership, = self.memberships()
        self.assertEqual(membership['members'], [R3])
        self.assertEqual(membership['relations'], {})

    def test_current_repeated_in_priors_stays_last(self):
        self.descriptor(priorCliSessionIds=[R3, R1])
        membership, = self.memberships()
        self.assertEqual(membership['members'], [R1, R3])

    def test_unreadable_prior_list_yields_no_membership(self):
        for value in ('not-a-list', [1, 2], [''], [R1, None]):
            self.descriptor(priorCliSessionIds=value)
            self.assertEqual(self.memberships(), [])

    def test_broken_edge_yields_no_membership(self):
        for edges in ('x', [{'parent': R1}], [self.edge(R1, R1)],
                      [dict(self.edge(R1, R2), forkPoint='')],
                      [dict(self.edge(R1, R2), cwd=str(self.root / 'elsewhere'))],
                      [self.edge(R1, R2), self.edge(R4, R2)]):
            self.descriptor(priorCliSessionIds=[R1, R2], rewindEdges=edges)
            self.assertEqual(self.memberships(), [])

    def test_two_stores_disagreeing_yield_no_membership(self):
        self.descriptor(priorCliSessionIds=[R1])
        self.descriptor('local_copy.json', priorCliSessionIds=[R2])
        self.assertEqual(self.memberships(), [])

    def test_identical_copies_are_one_membership(self):
        self.descriptor(priorCliSessionIds=[R1])
        self.descriptor('local_copy.json', priorCliSessionIds=[R1])
        self.assertEqual(len(self.memberships()), 1)

    def test_a_record_two_conversations_claim_drops_both(self):
        self.descriptor(priorCliSessionIds=[R1])
        self.descriptor('local_other.json', sessionId=OTHER_CONVERSATION, cliSessionId=R4,
                        priorCliSessionIds=[R1])
        self.assertEqual(self.memberships(), [])

    def test_fork_and_spawn_travel_as_lineage_only(self):
        self.descriptor(forkedFromSessionId=OTHER_CONVERSATION,
                        spawnedFrom={'sessionId': OTHER_CONVERSATION, 'taskId': 'task'})
        membership, = self.memberships()
        self.assertEqual(membership['forked_from_conversation_id'], OTHER_CONVERSATION)
        self.assertEqual(membership['spawned_from_conversation_id'], OTHER_CONVERSATION)
        self.assertEqual(membership['members'], [R3])

    def test_missing_working_directory_yields_no_membership(self):
        self.descriptor(cwd='relative/path')
        self.assertEqual(self.memberships(), [])


class ConversationIdentityTests(Fixture):
    def test_rule_1_descriptor_members_fold_oldest_to_newest(self):
        self.descriptor(priorCliSessionIds=[R1, R2],
                        rewindEdges=[self.edge(R1, R2), self.edge(R2, R3)])
        items = self.by_id([self.card(R1), self.card(R2), self.card(R3), self.card(R4)])
        for record_id in (R1, R2, R3):
            self.assertEqual(items[record_id]['conversation_id'], CONVERSATION)
            self.assertEqual(items[record_id]['conversation_current_id'], R3)
        self.assertEqual([r['id'] for r in items[R3]['conversation_records']], [R1, R2, R3])
        self.assertEqual([r['relation'] for r in items[R3]['conversation_records']],
                         [None, 'rewind', 'rewind'])
        self.assertEqual([r['is_current'] for r in items[R3]['conversation_records']],
                         [False, False, True])
        self.assertEqual(items[R4]['conversation_id'], R4)

    def test_rule_2_a_record_no_descriptor_covers_is_its_own_conversation(self):
        self.assert_all_separate([self.card(R1), self.card(R2)])

    def test_rule_3_fork_by_file_head_stays_separate(self):
        items = self.by_id([self.card(R1), self.card(R2, head_session_id=R1)])
        self.assertEqual(items[R2]['forked_from_record_id'], R1)
        self.assertEqual(items[R2]['conversation_id'], R2)
        self.assertEqual(items[R1]['conversation_id'], R1)

    def test_rule_3_fork_descriptor_exposes_lineage_without_merging(self):
        self.descriptor(forkedFromSessionId=OTHER_CONVERSATION)
        items = self.by_id([self.card(R3)])
        self.assertEqual(items[R3]['conversation_id'], CONVERSATION)
        self.assertEqual(items[R3]['forked_from_conversation_id'], OTHER_CONVERSATION)
        self.assertNotIn(R1, [r['id'] for r in items[R3]['conversation_records']])

    def test_rule_3_a_fork_and_its_source_never_share_a_conversation(self):
        self.descriptor(priorCliSessionIds=[R1])
        items = self.by_id([self.card(R1), self.card(R3, head_session_id=R1)])
        self.assertEqual(items[R1]['conversation_id'], R1)
        self.assertEqual(items[R3]['conversation_id'], R3)

    def test_rule_4_cross_file_compaction_merges_into_the_older_record(self):
        items = self.by_id([self.card(R1), self.card(R2)], links=[(R2, R1)])
        self.assertEqual(items[R1]['conversation_id'], R1)
        self.assertEqual(items[R2]['conversation_id'], R1)
        self.assertEqual(items[R2]['conversation_current_id'], R2)
        self.assertEqual([r['relation'] for r in items[R2]['conversation_records']],
                         [None, 'compaction-continuation'])

    def test_rule_4_a_desktop_conversation_outranks_a_bare_record_id(self):
        self.descriptor(cliSessionId=R1)
        items = self.by_id([self.card(R1), self.card(R2)], links=[(R2, R1)])
        self.assertEqual(items[R2]['conversation_id'], CONVERSATION)
        self.assertEqual(items[R2]['conversation_current_id'], R2)
        self.assertEqual(items[R1]['conversation_current_id'], R2)

    def test_rule_4_two_desktop_conversations_never_merge(self):
        self.descriptor(cliSessionId=R1)
        self.descriptor('local_other.json', sessionId=OTHER_CONVERSATION, cliSessionId=R2)
        items = self.by_id([self.card(R1), self.card(R2)], links=[(R2, R1)])
        self.assertEqual(items[R1]['conversation_id'], CONVERSATION)
        self.assertEqual(items[R2]['conversation_id'], OTHER_CONVERSATION)

    def test_rule_4_compaction_extends_a_chain_past_the_descriptor_current(self):
        self.descriptor(priorCliSessionIds=[R1, R2],
                        rewindEdges=[self.edge(R1, R2), self.edge(R2, R3)])
        items = self.by_id([self.card(R1), self.card(R2), self.card(R3), self.card(R4)],
                           links=[(R4, R3)])
        self.assertEqual(items[R4]['conversation_id'], CONVERSATION)
        self.assertEqual([r['id'] for r in items[R4]['conversation_records']],
                         [R1, R2, R3, R4])
        self.assertEqual(items[R1]['conversation_current_id'], R4)

    def test_rule_5_spawned_from_is_provenance_not_membership(self):
        self.descriptor(spawnedFrom={'sessionId': OTHER_CONVERSATION})
        items = self.by_id([self.card(R3), self.card(R1)])
        self.assertEqual(items[R3]['spawned_from_conversation_id'], OTHER_CONVERSATION)
        self.assertEqual(items[R3]['conversation_id'], CONVERSATION)
        self.assertEqual(items[R1]['conversation_id'], R1)

    def test_rule_6_filename_must_match_the_record_id(self):
        self.descriptor(priorCliSessionIds=[R1, R2],
                        rewindEdges=[self.edge(R1, R2), self.edge(R2, R3)])
        misnamed = self.card(R1)
        misnamed['jsonl_path'] = str(self.transcripts / (R4 + '.jsonl'))
        Path(misnamed['jsonl_path']).write_text('{}\n')
        self.assert_all_separate([misnamed, self.card(R2), self.card(R3)])

    def test_rule_6_a_record_outside_the_conversation_project_does_not_fold(self):
        self.descriptor(priorCliSessionIds=[R1])
        outside = self.card(R1, project_path=str(self.root / 'another-workspace'))
        self.assert_all_separate([outside, self.card(R3)])

    def test_rule_6_a_duplicated_record_id_does_not_fold(self):
        self.descriptor(priorCliSessionIds=[R1])
        self.assert_all_separate([self.card(R1), dict(self.card(R1)), self.card(R3)])

    def test_rule_6_a_branch_dissolves_the_whole_group(self):
        # Two records claiming the same predecessor is a tree, not a conversation.
        self.assert_all_separate([self.card(R1), self.card(R2), self.card(R3)],
                                 links=[(R2, R1), (R3, R1)])

    def test_rule_6_a_cycle_dissolves_the_whole_group(self):
        self.assert_all_separate([self.card(R1), self.card(R2)], links=[(R2, R1), (R1, R2)])

    def test_rule_6_a_non_claude_card_never_folds(self):
        self.descriptor(priorCliSessionIds=[R1])
        self.assert_all_separate([self.card(R1, source='codex'), self.card(R3)])

    def test_rule_6_a_missing_file_does_not_fold(self):
        self.descriptor(priorCliSessionIds=[R1])
        absent = self.card(R1)
        Path(absent['jsonl_path']).unlink()
        self.assert_all_separate([absent, self.card(R3)])

    def test_a_record_deleted_from_disk_still_lets_the_rest_group(self):
        self.descriptor(priorCliSessionIds=[R1, R2],
                        rewindEdges=[self.edge(R1, R2), self.edge(R2, R3)])
        items = self.by_id([self.card(R2), self.card(R3)])  # R1 is gone from the library
        self.assertEqual(items[R3]['conversation_id'], CONVERSATION)
        self.assertEqual([r['id'] for r in items[R3]['conversation_records']], [R2, R3])

    def test_a_descriptor_whose_current_record_is_absent_groups_nothing(self):
        self.descriptor(priorCliSessionIds=[R1, R2])
        self.assert_all_separate([self.card(R1), self.card(R2)])

    def test_titles_never_influence_identity(self):
        before = self.by_id([self.card(R1, custom_title='One'),
                             self.card(R2, custom_title='One')])
        self.assertEqual(before[R1]['conversation_id'], R1)
        self.assertEqual(before[R2]['conversation_id'], R2)
        self.descriptor(priorCliSessionIds=[R1], cliSessionId=R2)
        after = self.by_id([self.card(R1, custom_title='One'),
                            self.card(R2, custom_title='Completely different')])
        self.assertEqual(after[R1]['conversation_id'], CONVERSATION)
        self.assertEqual(after[R2]['conversation_id'], CONVERSATION)

    def test_shared_history_alone_never_merges(self):
        # Copied records are what rewind, resume and fork all look like inside the file.
        # Without a descriptor there is nothing to tell them apart, so nothing merges.
        self.assert_all_separate([self.card(R1), self.card(R2)])

    def test_identity_is_recomputed_not_retained(self):
        self.descriptor(priorCliSessionIds=[R1], cliSessionId=R2)
        first = self.identify([self.card(R1), self.card(R2)])
        (self.descriptors / 'account' / 'organization' / 'local_conversation.json').unlink()
        second = {item['id']: item for item in self.identify(first)}
        self.assertEqual(second[R1]['conversation_id'], R1)
        self.assertEqual(second[R2]['conversation_id'], R2)

    def test_non_claude_sources_are_their_own_conversations(self):
        items = self.by_id([self.card('codex-id', source='codex'),
                            self.card('session_kimi', source='kimi'),
                            self.card('devin:slug', source='devin'),
                            self.card('pi-id', source='pi'),
                            self.card('brain-id', source='antigravity')])
        for record_id, item in items.items():
            self.assertEqual(item['conversation_id'], record_id)
            self.assertEqual(item['conversation_current_id'], record_id)

    def test_input_items_are_never_mutated(self):
        self.descriptor(priorCliSessionIds=[R1], cliSessionId=R2)
        items = [self.card(R1), self.card(R2)]
        snapshot = json.dumps(items, sort_keys=True)
        self.identify(items)
        self.assertEqual(json.dumps(items, sort_keys=True), snapshot)

    def test_project_subdirectory_drift_keeps_the_conversation(self):
        # Migration surface case 13: a worktree moves the display directory below the
        # Desktop's stable working root. That must keep working.
        self.descriptor(priorCliSessionIds=[R1], cliSessionId=R2)
        drifted = self.card(R1, project_path=str(Path(self.project) / 'tools' / 'parser'))
        items = self.by_id([drifted, self.card(R2)])
        self.assertEqual(items[R1]['conversation_id'], CONVERSATION)


class MergeStateTests(unittest.TestCase):
    """The thirteen conflict cases from the migration surface, one test each."""

    def merge(self, state, records=(R1, R2, R3), current=R3):
        return session_identity.merge_conversation_state(list(records), current, state)

    def test_case_1_a_star_on_an_older_record_still_counts(self):
        merged = self.merge({R1: {'starred': True, 'starred_at': '2026-01-01T00:00:00+00:00'}})
        self.assertTrue(merged['starred'])
        self.assertEqual(merged['starred_at'], '2026-01-01T00:00:00+00:00')
        self.assertEqual(merged['starred_record_ids'], [R1])

    def test_case_1_the_earliest_star_time_wins(self):
        merged = self.merge({R1: {'starred': True, 'starred_at': '2026-02-01T00:00:00+00:00'},
                             R3: {'starred': True, 'starred_at': '2026-01-01T00:00:00+00:00'}})
        self.assertEqual(merged['starred_at'], '2026-01-01T00:00:00+00:00')
        self.assertEqual(merged['starred_record_ids'], [R1, R3])

    def test_case_2_every_starred_member_is_reported_so_unstar_can_clear_them(self):
        merged = self.merge({R1: {'starred': True}, R2: {'starred': False},
                             R3: {'starred': True}})
        self.assertEqual(merged['starred_record_ids'], [R1, R3])

    def test_case_3_an_archived_ancestor_never_hides_a_live_conversation(self):
        merged = self.merge({R1: {'archived': True, 'archived_at': 'then'}})
        self.assertEqual(merged['archived_entry'], {})
        self.assertIsNone(merged['archived_at'])

    def test_case_4_an_active_ancestor_never_unhides_an_archived_conversation(self):
        merged = self.merge({R1: {'archived': False},
                             R3: {'archived': True, 'archived_at': 'now'}})
        self.assertTrue(merged['archived_entry']['archived'])
        self.assertEqual(merged['archived_at'], 'now')

    def test_case_5_explicit_archived_means_the_current_records_entry(self):
        # _effective_archived reads "archived" out of exactly this entry, so a derived
        # Codex archive flag still loses to an explicit choice on the current record only.
        merged = self.merge({R1: {'archived': True}, R3: {'archived': False}})
        self.assertIn('archived', merged['archived_entry'])
        self.assertFalse(merged['archived_entry']['archived'])
        merged = self.merge({R1: {'archived': True}})
        self.assertNotIn('archived', merged['archived_entry'])

    def test_case_6_older_notes_are_returned_separately_never_dropped(self):
        merged = self.merge({R1: {'note': 'first thought'}, R2: {'note': '   '},
                             R3: {'note': 'current thought'}})
        self.assertEqual(merged['note'], 'current thought')
        self.assertEqual(merged['older_notes'], [{'record_id': R1, 'note': 'first thought'}])

    def test_case_6_an_older_note_survives_an_empty_current_note(self):
        merged = self.merge({R1: {'note': 'first thought'}})
        self.assertEqual(merged['note'], '')
        self.assertEqual(merged['older_notes'], [{'record_id': R1, 'note': 'first thought'}])

    def test_case_7_the_newest_older_title_is_used_and_attributed(self):
        merged = self.merge({R1: {'title_override': 'old'}, R2: {'title_override': 'newer'}})
        self.assertEqual(merged['title_override'], 'newer')
        self.assertEqual(merged['title_override_source_id'], R2)

    def test_case_7_the_current_records_title_wins(self):
        merged = self.merge({R1: {'title_override': 'old'}, R3: {'title_override': 'current'}})
        self.assertEqual(merged['title_override'], 'current')
        self.assertEqual(merged['title_override_source_id'], R3)

    def test_case_8_human_confirmed_only_ever_reveals(self):
        self.assertTrue(self.merge({R1: {'human_confirmed': True}})['human_confirmed'])
        self.assertFalse(self.merge({})['human_confirmed'])

    def test_case_9_a_cached_brief_is_never_merged(self):
        merged = self.merge({R1: {'brief': 'summary of the older record'},
                             R3: {'brief': 'summary of the current record'}})
        self.assertNotIn('brief', merged)

    def test_case_10_a_single_record_conversation_reads_exactly_like_the_record(self):
        entry = {'archived': True, 'archived_at': 'then', 'starred': True,
                 'starred_at': 'before', 'note': 'n', 'title_override': 't',
                 'human_confirmed': True}
        merged = self.merge({R1: entry}, records=(R1,), current=R1)
        self.assertEqual(merged['archived_entry'], entry)
        self.assertEqual((merged['starred'], merged['starred_at']), (True, 'before'))
        self.assertEqual((merged['note'], merged['older_notes']), ('n', []))
        self.assertEqual(merged['title_override'], 't')
        self.assertTrue(merged['human_confirmed'])

    def test_case_11_unknown_records_read_as_empty_rather_than_failing(self):
        merged = self.merge({}, records=(R1, R2, R3))
        self.assertEqual((merged['starred'], merged['note'], merged['title_override']),
                         (False, '', ''))
        self.assertIsNone(merged['title_override_source_id'])

    def test_case_12_the_merge_never_writes_to_the_state_it_was_given(self):
        state = {R1: {'starred': True}, R3: {'note': 'kept'}}
        snapshot = json.dumps(state, sort_keys=True)
        self.merge(state)
        self.assertEqual(json.dumps(state, sort_keys=True), snapshot)

    def test_case_13_a_star_survives_the_conversation_dissolving(self):
        # State stays keyed by record, so when identity fails open the star is still there.
        merged = self.merge({R1: {'starred': True}}, records=(R1,), current=R1)
        self.assertTrue(merged['starred'])


class SessionChoiceTests(Fixture):
    def test_superseded_records_are_not_offered_as_candidates(self):
        self.descriptor(priorCliSessionIds=[R1, R2],
                        rewindEdges=[self.edge(R1, R2), self.edge(R2, R3)])
        items = self.identify([self.card(R1, recent_msgs=[], user_turn_count=4),
                               self.card(R2, recent_msgs=[], user_turn_count=4),
                               self.card(R3, recent_msgs=[], user_turn_count=4),
                               self.card(R5, recent_msgs=[], user_turn_count=4)])
        offered = [choice['id'] for choice in session_identity.session_choices(items)]
        self.assertEqual(sorted(offered), sorted([R3, R5]))

    def test_records_without_conversation_data_are_still_offered(self):
        items = [self.card(R1, recent_msgs=[], user_turn_count=4)]
        self.assertEqual([c['id'] for c in session_identity.session_choices(items)], [R1])


if __name__ == '__main__':
    unittest.main()
