"""Antigravity in-file rewind: live history versus abandoned branches.

An Antigravity rewind never leaves the file. The client re-opens an earlier `step_index`
and keeps appending, so an abandoned branch and the branch that replaced it sit in one
transcript with no lineage record. A drop in `step_index` alone does not mean a rewind:
rows are also persisted out of order, which lowers `step_index` while abandoning nothing.
The guard is that a rewind gives a step slot a second occupant.

These tests cover the classifier and every reader that presents Antigravity content.
All fixtures are synthetic. See docs/decisions/2026-09-20-antigravity-rewind-history.md.
"""
import json
import tempfile
import unittest
from pathlib import Path

import server
import session_logbook_cli as cli
from sources import antigravity as ag


# ---------- synthetic row builders ----------
def user(step, text, ts="2026-02-01T00:00:00Z"):
    return {"step_index": step, "source": "USER_EXPLICIT", "type": "USER_INPUT",
            "status": "DONE", "created_at": ts,
            "content": f"<USER_REQUEST>{text}</USER_REQUEST>"}


def planner(step, text="", ts="2026-02-01T00:00:00Z", tools=()):
    row = {"step_index": step, "source": "MODEL", "type": "PLANNER_RESPONSE",
           "status": "DONE", "created_at": ts, "content": text}
    if tools:
        row["tool_calls"] = [{"name": name, "args": {"toolSummary": f'"{name} call"'}}
                             for name in tools]
    return row


def result(step, text, kind="RUN_COMMAND", ts="2026-02-01T00:00:00Z"):
    return {"step_index": step, "source": "SYSTEM", "type": kind,
            "status": "DONE", "created_at": ts, "content": text}


def checkpoint(step, text="{{ CHECKPOINT 0 }} earlier parts were truncated",
               ts="2026-02-01T00:00:00Z"):
    return {"step_index": step, "source": "SYSTEM", "type": "CHECKPOINT",
            "status": "DONE", "created_at": ts, "content": text}


def steps(rows, plan):
    """The step slots the plan calls live, in file order."""
    return [row.get("step_index") for row, keep in zip(rows, plan["live"]) if keep]


class ClassifierTests(unittest.TestCase):
    """The pure rule: which rows a later rewind abandoned."""

    def test_monotonic_transcript_keeps_every_row(self):
        rows = [user(0, "q"), planner(1, "a"), result(2, "out"), planner(3, "done")]
        plan = ag.rewind_plan(rows)
        self.assertEqual(plan["live"], [True] * 4)
        self.assertEqual(plan["abandoned_rows"], 0)
        self.assertEqual(plan["rewinds"], [])

    def test_single_rewind_abandons_the_branch_at_and_above_its_step(self):
        rows = [user(0, "q"), planner(1, "a"), user(2, "detour"), planner(3, "side"),
                user(2, "asked again"), planner(3, "new answer")]
        plan = ag.rewind_plan(rows)
        self.assertEqual(plan["live"], [True, True, False, False, True, True])
        self.assertEqual(plan["abandoned_rows"], 2)
        self.assertEqual(plan["rewinds"],
                         [{"index": 4, "step": 2, "abandoned_rows": 2}])

    def test_stacked_rewinds_in_one_transcript(self):
        # Mirrors the shape of the real transcript that rewinds once mid-conversation and
        # a second time back to the opening message.
        rows = [user(0, "q"), planner(1, "a"),
                user(2, "detour"), planner(3, "side"), result(4, "side out"),
                user(2, "second try"), planner(3, "second answer"),
                user(0, "start over"), planner(1, "fresh answer")]
        plan = ag.rewind_plan(rows)
        self.assertEqual(plan["live"], [False] * 7 + [True, True])
        self.assertEqual(plan["abandoned_rows"], 7)
        self.assertEqual([(r["index"], r["step"]) for r in plan["rewinds"]],
                         [(5, 2), (7, 0)])
        self.assertEqual(steps(rows, plan), [0, 1])

    def test_rewind_to_zero_abandons_everything_before_it(self):
        rows = [user(0, "q"), planner(1, "a"), result(2, "out"), planner(3, "more"),
                user(0, "start over")]
        plan = ag.rewind_plan(rows)
        self.assertEqual(plan["live"], [False, False, False, False, True])
        self.assertEqual(plan["abandoned_rows"], 4)

    def test_two_rewinds_to_the_same_step(self):
        # No real transcript rewinds twice to the same step, so this is the rule's own
        # answer rather than a measured one: each rewind abandons only what was live.
        rows = [user(0, "q"), planner(1, "a"), user(2, "x"), planner(3, "x answer"),
                user(2, "y"), planner(3, "y answer"),
                user(2, "z")]
        plan = ag.rewind_plan(rows)
        self.assertEqual(plan["live"], [True, True, False, False, False, False, True])
        self.assertEqual([(r["index"], r["step"], r["abandoned_rows"])
                          for r in plan["rewinds"]], [(4, 2, 2), (6, 2, 2)])

    def test_repeating_the_last_step_without_a_drop_keeps_both_rows(self):
        # A re-submission of the newest row does not lower step_index, so the rule cannot
        # see it and keeps both occupants. Over-inclusion is the safe direction.
        rows = [user(0, "q"), planner(1, "a"), user(2, "x"), user(2, "x again")]
        plan = ag.rewind_plan(rows)
        self.assertEqual(plan["live"], [True] * 4)
        self.assertEqual(plan["rewinds"], [])

    # ---------- the three write-order artifacts: nothing may be abandoned ----------
    def test_checkpoint_flushed_two_rows_late_keeps_every_row(self):
        rows = [user(0, "q"), planner(1, "a"), result(2, "out"), planner(3, "more"),
                planner(5, "later"), result(6, "later out"),
                checkpoint(4), planner(7, "after")]
        plan = ag.rewind_plan(rows)
        self.assertEqual(plan["live"], [True] * 8)
        self.assertEqual(plan["abandoned_rows"], 0)
        self.assertEqual(plan["rewinds"], [])

    def test_checkpoint_flushed_four_rows_late_keeps_every_row(self):
        rows = [user(0, "q"), planner(1, "a"), result(2, "out"), planner(3, "more"),
                planner(5, "p5"), result(6, "r6"), planner(7, "p7"), result(8, "r8"),
                checkpoint(4), planner(9, "p9")]
        plan = ag.rewind_plan(rows)
        self.assertEqual(plan["live"], [True] * 10)
        self.assertEqual(plan["abandoned_rows"], 0)

    def test_planner_persisted_after_its_parallel_results_keeps_every_row(self):
        # One planner row issues several parallel tool calls; the results land first.
        rows = [user(0, "q"), result(2, "r1", "VIEW_FILE"), result(3, "r2", "VIEW_FILE"),
                result(4, "r3", "VIEW_FILE"),
                planner(1, "", tools=("view_file", "view_file", "view_file")),
                planner(5, "answer")]
        plan = ag.rewind_plan(rows)
        self.assertEqual(plan["live"], [True] * 6)
        self.assertEqual(plan["rewinds"], [])

    def test_artifact_then_real_rewind_is_still_detected(self):
        # The late row lowers the previous row's step; the rewind after it must still fire.
        rows = [user(0, "q"), result(2, "r"), planner(1, "", tools=("view_file",)),
                planner(3, "answer"), user(2, "ask again")]
        plan = ag.rewind_plan(rows)
        self.assertEqual(plan["live"], [True, False, True, False, True])
        self.assertEqual([r["step"] for r in plan["rewinds"]], [2])

    # ---------- gaps and missing slots ----------
    def test_gap_in_step_slots_is_not_a_rewind(self):
        rows = [user(0, "q"), planner(1, "a"), result(3, "out"), planner(4, "more")]
        plan = ag.rewind_plan(rows)
        self.assertEqual(plan["live"], [True] * 4)

    def test_rewind_across_a_gap_keeps_the_prefix(self):
        rows = [user(0, "q"), planner(1, "a"), user(3, "detour"), planner(5, "side"),
                user(3, "asked again")]
        plan = ag.rewind_plan(rows)
        self.assertEqual(plan["live"], [True, True, False, False, True])
        self.assertEqual(steps(rows, plan), [0, 1, 3])

    def test_rewind_to_a_never_written_slot_fails_safe_by_keeping_too_much(self):
        # Step 2 was never written, so the guard cannot see a second occupant and keeps
        # the earlier rows. Over-inclusion is the safe direction; see the decision record.
        rows = [user(0, "q"), planner(1, "a"), result(3, "out"), planner(4, "more"),
                user(2, "rewound to a slot with no row")]
        plan = ag.rewind_plan(rows)
        self.assertEqual(plan["live"], [True] * 5)
        self.assertEqual(plan["rewinds"], [])

    # ---------- rows that carry no step slot ----------
    def test_row_without_a_step_slot_shares_the_fate_of_the_row_above(self):
        rows = [user(0, "q"), {"type": "SYSTEM_MESSAGE", "content": "kept notice"},
                user(1, "detour"), {"type": "SYSTEM_MESSAGE", "content": "branch notice"},
                planner(2, "side answer"), user(1, "asked again")]
        plan = ag.rewind_plan(rows)
        self.assertEqual(plan["live"], [True, True, False, False, False, True])

    def test_leading_row_without_a_step_slot_is_kept(self):
        rows = [{"type": "SYSTEM_MESSAGE", "content": "header"}, user(0, "q"),
                planner(1, "a"), user(0, "start over")]
        plan = ag.rewind_plan(rows)
        self.assertEqual(plan["live"], [True, False, False, True])

    def test_unusable_step_values_are_treated_as_absent(self):
        for value in (True, False, "3", 1.5, None, -1):
            with self.subTest(value=value):
                self.assertIsNone(ag._step_index({"step_index": value}))
        self.assertEqual(ag._step_index({"step_index": 0}), 0)

    # ---------- ordering and anchors ----------
    def test_rows_are_never_reordered(self):
        rows = [user(0, "q"), result(2, "r"), planner(1, ""), planner(3, "a")]
        self.assertEqual(ag.live_lines(rows), rows)

    def test_live_records_keep_the_original_line_numbers(self):
        rows = [user(0, "q"), planner(1, "a"), user(0, "start over"), planner(1, "b")]
        records = list(enumerate(rows, 1))
        live, report = ag.live_records(records)
        self.assertEqual([number for number, _ in live], [3, 4])
        self.assertEqual(report["abandoned_rows"], 2)
        self.assertEqual(report["rewinds"],
                         [{"line": 3, "step": 0, "abandoned_rows": 2}])


class TranscriptFileMixin:
    def write_transcript(self, conversation_id, rows):
        root = Path(self.tmp.name) / "brain" / conversation_id
        path = root / ".system_generated" / "logs" / "transcript.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                        encoding="utf-8")
        return path

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)


REWOUND_ID = "eeeeeeee-1111-4111-8111-eeeeeeeeeeee"
STRAIGHT_ID = "eeeeeeee-2222-4222-8222-eeeeeeeeeeee"

REWOUND_ROWS = [
    user(0, "abandoned first question about /Users/alice/proj/a.md"),
    planner(1, "Abandoned answer."),
    result(2, "abandoned command output"),
    user(0, "live question about /Users/alice/proj/a.md"),
    planner(1, "Live answer."),
]
STRAIGHT_ROWS = [
    user(0, "only question about /Users/alice/proj/a.md"),
    planner(1, "Only answer."),
]


class ReaderTests(TranscriptFileMixin, unittest.TestCase):
    """Every reader presents the live history and says what it left out."""

    def setUp(self):
        super().setUp()
        self.rewound = self.write_transcript(REWOUND_ID, REWOUND_ROWS)
        self.straight = self.write_transcript(STRAIGHT_ID, STRAIGHT_ROWS)

    def test_card_metadata_describes_the_live_history(self):
        meta = ag.extract_metadata(self.rewound)
        self.assertEqual(meta["user_turn_count"], 1)
        self.assertEqual(meta["first_user_msg"],
                         "live question about /Users/alice/proj/a.md")
        texts = " ".join(m["text"] for m in meta["recent_msgs"])
        self.assertIn("Live answer.", texts)
        self.assertNotIn("Abandoned", texts)

    def test_project_path_still_reads_every_row(self):
        # A rewind does not move the IDE workspace the conversation ran in, so the
        # project stays inferable even when the live branch mentions no path.
        rows = [user(0, "look at /Users/alice/proj/a.md"), planner(1, "ok"),
                user(0, "never mind")]
        path = self.write_transcript("eeeeeeee-3333-4333-8333-eeeeeeeeeeee", rows)
        self.assertEqual(ag.extract_metadata(path)["project_path"], "/Users/alice/proj")

    def test_conversation_shows_only_live_turns(self):
        conv = ag.extract_conversation(self.rewound)
        texts = [t.get("text", "") + t.get("result", "") for t in conv["turns"]]
        self.assertEqual(len(conv["turns"]), 2)
        self.assertTrue(all("bandoned" not in t for t in texts), texts)

    def test_conversation_reports_what_the_rewind_abandoned(self):
        conv = ag.extract_conversation(self.rewound)
        self.assertEqual(conv["rewind_abandoned_rows"], 3)
        self.assertEqual(conv["rewinds"],
                         [{"line": 4, "step": 0, "abandoned_rows": 3}])
        # The raw line count is unchanged, so the hidden rows remain countable.
        self.assertEqual(conv["total_lines"], 5)

    def test_conversation_without_a_rewind_reports_zero(self):
        conv = ag.extract_conversation(self.straight)
        self.assertEqual(conv["rewind_abandoned_rows"], 0)
        self.assertEqual(conv["rewinds"], [])

    def test_abandoned_tool_call_cannot_capture_a_live_result(self):
        # The abandoned branch ends with an unanswered tool call. Pairing across the
        # rewind would hand the live result to it and drift every later pairing.
        rows = [user(0, "old"), planner(1, "", tools=("view_file",)),
                user(0, "new"), planner(1, "", tools=("run_command",)),
                result(2, "NEW_RESULT")]
        path = self.write_transcript("eeeeeeee-4444-4444-8444-eeeeeeeeeeee", rows)
        tools = [t for t in ag.extract_conversation(path)["turns"] if t["type"] == "tool"]
        self.assertEqual([t["name"] for t in tools], ["run_command"])
        self.assertIn("NEW_RESULT", tools[0]["result"])

    def test_markdown_export_shows_only_live_content_and_states_the_loss(self):
        text = ag.extract_transcript(self.rewound)
        self.assertIn("Live answer.", text)
        self.assertNotIn("Abandoned answer.", text)
        self.assertNotIn("abandoned command output", text)
        self.assertIn("Abandoned by rewind: 3 raw lines", text)

    def test_markdown_export_without_a_rewind_keeps_its_old_header(self):
        self.assertNotIn("Abandoned by rewind", ag.extract_transcript(self.straight))

    def test_abandoned_line_numbers_are_the_file_s_own(self):
        self.assertEqual(ag.abandoned_line_numbers(self.rewound), {1, 2, 3})
        self.assertEqual(ag.abandoned_line_numbers(self.straight), set())

    def test_raw_evidence_still_reads_an_abandoned_line(self):
        # Hiding a row from the reading must not make its source unreachable.
        evidence = cli.read_evidence(self.rewound, line=2, context=0)
        self.assertTrue(evidence.startswith("[L2] "), evidence[:40])
        self.assertIn("Abandoned answer.", evidence)


class SearchTests(TranscriptFileMixin, unittest.TestCase):
    """Search matches the live history only, like the other branching sources."""

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, ag, "AG_BRAIN", ag.AG_BRAIN)
        ag.AG_BRAIN = Path(self.tmp.name) / "brain"
        self.path = self.write_transcript(REWOUND_ID, REWOUND_ROWS)
        self.meta = ag.extract_metadata(self.path)

    def search(self, *terms):
        return server._search_session(self.path, list(terms), self.meta)

    def test_live_text_matches(self):
        self.assertTrue(self.search("live question"))

    def test_abandoned_text_does_not_match(self):
        self.assertEqual(self.search("abandoned"), [])
        self.assertEqual(self.search("abandoned", "live"), [])

    def test_supplied_matching_lines_are_filtered_by_their_line_number(self):
        # The fast path hands over only the lines ripgrep matched, paired with their
        # physical line numbers; abandoned lines must be dropped there too.
        raw = self.path.read_text(encoding="utf-8").splitlines()
        supplied = list(enumerate((line + "\n" for line in raw), 1))
        self.assertEqual(server._search_session(self.path, ["abandoned"], self.meta,
                                                lines=supplied), [])
        self.assertTrue(server._search_session(self.path, ["live"], self.meta,
                                               lines=supplied))


class FollowTests(TranscriptFileMixin, unittest.TestCase):
    """Following a live conversation across a rewind that lands while it is open.

    Antigravity has no cursor-based follow: the CLI does not read this source, and the
    standalone reader follows a running conversation by polling
    /conversation?fingerprint=… and redrawing the whole payload when the file changes.
    That is the right shape here, because a rewind retroactively removes rows a follower
    has already been shown. The redraw carries the new abandoned-row count, so the
    follower is told that earlier steps are gone rather than silently losing them.
    """

    def test_a_rewind_that_arrives_later_is_reported_on_the_next_read(self):
        path = self.write_transcript(REWOUND_ID, [
            user(0, "first question"), planner(1, "first answer"), result(2, "output")])
        before = ag.extract_conversation(path)
        before_fingerprint = server.file_fingerprint(path)
        self.assertEqual(before["rewind_abandoned_rows"], 0)
        self.assertEqual(len(before["turns"]), 3)

        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(user(0, "asked differently")) + "\n")

        after = ag.extract_conversation(path)
        self.assertNotEqual(server.file_fingerprint(path), before_fingerprint)
        # The three rows the follower already received are gone from the live history,
        # and the payload says how many.
        self.assertEqual(after["rewind_abandoned_rows"], 3)
        self.assertEqual([t["type"] for t in after["turns"]], ["user"])
        self.assertEqual(after["rewinds"],
                         [{"line": 4, "step": 0, "abandoned_rows": 3}])


class SurfaceBoundaryTests(TranscriptFileMixin, unittest.TestCase):
    """A tripwire for the surfaces that do not read Antigravity yet."""

    def test_antigravity_is_not_an_anchored_or_cli_source(self):
        # The anchored renderer and the Agent CLI have no Antigravity reader, so there is
        # no third place presenting this content today. If either gains one, this test
        # fails: apply the live-history rule there before shipping it.
        self.assertNotIn("antigravity", cli.SUPPORTED_SOURCES)
        path = self.write_transcript(REWOUND_ID, REWOUND_ROWS)
        self.assertIsNone(cli.detect_source(path))


if __name__ == "__main__":
    unittest.main()
