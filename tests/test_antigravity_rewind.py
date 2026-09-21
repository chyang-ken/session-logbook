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
import re
import tempfile
import unittest
from pathlib import Path

import server
import session_logbook_cli as cli
from sources import anchored_transcript as at
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
        # Keep every reader in this file off the machine's real Antigravity data.
        for attribute, value in (("AG_SUMMARIES", Path(self.tmp.name) / "no-summaries.pb"),
                                 ("_TITLE_CACHE", {"mtime": 0.0, "data": {}})):
            self.addCleanup(setattr, ag, attribute, getattr(ag, attribute))
            setattr(ag, attribute, value)


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
    """The web reader follows a live conversation across a rewind that lands while it is open.

    The standalone reader polls /conversation?fingerprint=… and redraws the whole payload
    when the file changes, which is the right shape here because a rewind retroactively
    removes rows a follower has already been shown. The redraw carries the new
    abandoned-row count, so the follower is told that earlier steps are gone rather than
    silently losing them. CommandTests covers the CLI's cursor-based equivalent.
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


class AnchoredRendererTests(TranscriptFileMixin, unittest.TestCase):
    """The anchored transcript renders the live history at the file's own line numbers."""

    def render(self, conversation_id, rows):
        return at.render_antigravity(self.write_transcript(conversation_id, rows))

    def test_a_plain_transcript_renders_every_row_at_its_physical_line(self):
        text = self.render(STRAIGHT_ID, STRAIGHT_ROWS)
        self.assertIn("[U1] [L1] USER", text)
        self.assertIn("[L2] ASSISTANT: Only answer.", text)

    def test_a_rewind_renders_the_live_branch_only(self):
        text = self.render(REWOUND_ID, REWOUND_ROWS)
        # The live user turn is the file's fourth line and the transcript's first turn.
        self.assertIn("[U1] [L4] USER", text)
        self.assertIn("[L5] ASSISTANT: Live answer.", text)
        self.assertNotIn("[U2]", text)
        for gone in ("Abandoned answer.", "abandoned command output", "abandoned first question"):
            self.assertNotIn(gone, text)

    def test_stacked_rewinds_leave_only_the_final_branch(self):
        rows = [user(0, "q"), planner(1, "a"),
                user(2, "detour"), planner(3, "side"), result(4, "side out"),
                user(2, "second try"), planner(3, "second answer"),
                user(0, "start over"), planner(1, "fresh answer")]
        text = self.render("eeeeeeee-5555-4555-8555-eeeeeeeeeeee", rows)
        self.assertIn("[U1] [L8] USER", text)
        self.assertIn("[L9] ASSISTANT: fresh answer", text)
        self.assertNotIn("second answer", text)
        self.assertNotIn("side out", text)

    def test_a_write_order_artifact_abandons_nothing_and_renders_in_file_order(self):
        # One planner row issues three parallel calls whose results were persisted first.
        # step_index drops without a rewind, so every row must still be rendered.
        rows = [user(0, "q"), result(2, "r1", "VIEW_FILE"), result(3, "r2", "VIEW_FILE"),
                result(4, "r3", "VIEW_FILE"),
                planner(1, "", tools=("view_file", "view_file", "view_file")),
                planner(5, "answer")]
        text = self.render("eeeeeeee-6666-4666-8666-eeeeeeeeeeee", rows)
        anchors = [int(number) for number in re.findall(r"\[L(\d+)\]", text)]
        self.assertEqual(anchors, sorted(anchors), text)
        self.assertEqual(anchors.count(5), 3)  # the three calls share their planner's line
        self.assertIn("[L6] ASSISTANT: answer", text)

    def test_tool_calls_and_results_stay_at_their_own_lines(self):
        rows = [user(0, "q"), planner(1, "", tools=("run_command",)),
                result(2, "command output text")]
        text = self.render("eeeeeeee-7777-4777-8777-eeeeeeeeeeee", rows)
        self.assertIn("[L2]   🔧 run_command: run_command call", text)
        # A successful body collapses to status and size, expandable by its own anchor.
        self.assertRegex(text, r"\[L3\]   ⮑ RESULT OK run_command: \[1 lines, \d+ chars hidden")

    def test_an_errored_result_keeps_its_leading_text(self):
        rows = [user(0, "q"), planner(1, "", tools=("run_command",)),
                {"step_index": 2, "source": "SYSTEM", "type": "ERROR_MESSAGE",
                 "status": "ERROR", "created_at": "2026-02-01T00:00:00Z",
                 "content": "command not found"}]
        text = self.render("eeeeeeee-8888-4888-8888-eeeeeeeeeeee", rows)
        self.assertIn("⮑ RESULT ERROR run_command: command not found", text)

    def test_a_user_row_that_carries_no_words_is_not_a_turn(self):
        # A settings-change row cleans to nothing. Counting it would make [U#] disagree
        # with the reader's user turns for the same session.
        rows = [user(0, "real question"),
                {"step_index": 1, "source": "USER_EXPLICIT", "type": "USER_INPUT",
                 "status": "DONE", "created_at": "2026-02-01T00:00:00Z",
                 "content": "<USER_SETTINGS_CHANGE>Model Selection from a to b."
                            "</USER_SETTINGS_CHANGE>"},
                user(2, "second question")]
        path = self.write_transcript("eeeeeeee-1010-4010-8010-eeeeeeeeeeee", rows)
        text = at.render_antigravity(path)
        self.assertIn("[U1] [L1] USER", text)
        self.assertIn("[U2] [L3] USER", text)
        self.assertNotIn("[L2]", text)
        # The same count the web reader reports for this conversation.
        self.assertEqual(sum(1 for turn in ag.extract_conversation(path)["turns"]
                             if turn["type"] == "user"), 2)

    def test_the_digest_header_names_the_source_and_the_abandoned_lines(self):
        path = self.write_transcript(REWOUND_ID, REWOUND_ROWS)
        header = at.digest_header(path, "antigravity")
        self.assertIn("Navigable transcript of a Antigravity session", header)
        self.assertIn("Abandoned by rewind: 3 raw lines at L1-L3", header)
        self.assertIn("open them at their [L#] as evidence", header)

    def test_the_digest_header_states_zero_when_nothing_was_rewound(self):
        header = at.digest_header(self.write_transcript(STRAIGHT_ID, STRAIGHT_ROWS),
                                  "antigravity")
        self.assertIn("Abandoned by rewind: 0 raw lines", header)
        self.assertNotIn("raw lines at L", header)

    def test_every_anchor_points_at_the_row_it_rendered(self):
        path = self.write_transcript(REWOUND_ID, REWOUND_ROWS)
        raw = path.read_text(encoding="utf-8").splitlines()
        anchors = {int(number) for number
                   in re.findall(r"\[L(\d+)\]", at.render_antigravity(path))}
        self.assertEqual(anchors, {4, 5})
        for anchor in anchors:
            self.assertIn(json.loads(raw[anchor - 1])["type"],
                          {"USER_INPUT", "PLANNER_RESPONSE"})


class CommandTests(TranscriptFileMixin, unittest.TestCase):
    """The Agent CLI reads Antigravity, and reports what a rewind took back."""

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, ag, "AG_BRAIN", ag.AG_BRAIN)
        ag.AG_BRAIN = Path(self.tmp.name) / "brain"
        for attribute, value in (("_cache", {}), ("_state", {}), ("_state_loaded", True)):
            self.addCleanup(setattr, server, attribute, getattr(server, attribute))
            setattr(server, attribute, value)
        self.rewound = self.write_transcript(REWOUND_ID, REWOUND_ROWS)
        self.straight = self.write_transcript(STRAIGHT_ID, STRAIGHT_ROWS)

    def test_antigravity_is_a_supported_cli_source(self):
        self.assertIn("antigravity", cli.SUPPORTED_SOURCES)
        self.assertEqual(cli.detect_source(self.rewound), "antigravity")

    def test_locate_resolves_a_conversation_id_to_its_transcript(self):
        self.assertEqual(cli.resolve_target(REWOUND_ID), self.rewound.resolve())
        self.assertEqual(cli.session_metadata(self.rewound)["source"], "antigravity")

    def test_status_counts_the_abandoned_rows_without_hiding_the_raw_file(self):
        status = cli.status_for(self.rewound)
        self.assertEqual(status["total_lines"], 5)
        self.assertEqual(status["next_cursor"], "L5")
        self.assertEqual(status["rewind_abandoned_rows"], 3)
        self.assertEqual(status["rewind_abandoned_lines"], "L1-L3")
        self.assertEqual(cli.status_for(self.straight)["rewind_abandoned_lines"], "none")

    def test_context_returns_the_live_history(self):
        text = cli.render_context(self.rewound)
        self.assertIn("[U1] [L4] USER", text)
        self.assertNotIn("Abandoned answer.", text)
        self.assertIn("# NEXT_CURSOR: L5", text)

    def test_follow_after_a_rewind_lands_names_the_lines_it_already_delivered(self):
        # A follower reads the opening branch, then a rewind is appended underneath it.
        path = self.write_transcript("eeeeeeee-9999-4999-8999-eeeeeeeeeeee", [
            user(0, "first question"), planner(1, "first answer"), result(2, "output")])
        self.assertIn("# NEXT_CURSOR: L3", cli.render_context(path))
        self.assertIn("# REMOVED_BEFORE_CURSOR: none", cli.render_context(path, after_line=3))

        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(user(0, "asked differently")) + "\n")

        text = cli.render_context(path, after_line=3)
        # The three rows the follower already holds left the conversation; it is told so
        # rather than silently keeping them.
        self.assertIn("# REMOVED_BEFORE_CURSOR: L1-L3", text)
        self.assertIn("# FOLLOW_MODE: live history after in-file rewinds", text)
        self.assertNotIn("first answer", text)
        self.assertIn("[U1] [L4] USER", text)

    def test_follow_reports_none_when_the_cursor_survived(self):
        self.assertIn("# REMOVED_BEFORE_CURSOR: none",
                      cli.render_context(self.straight, after_line=2))

    def test_a_message_that_quotes_an_anchor_does_not_move_the_cursor(self):
        # Messages are preserved in full, so one can contain "[L1]" -- real local history
        # does, from pasting an anchored transcript into a conversation. Reading that as the
        # line's own anchor would drop the rest of the message from a follow.
        path = self.write_transcript("eeeeeeee-2222-4222-8222-ffffffffffff", [
            user(0, "q"),
            planner(1, "first paragraph\nsee [L1] for the earlier note\nlast paragraph")])
        text = cli.render_context(path, after_line=2)
        self.assertIn("first paragraph", text)
        self.assertIn("see [L1] for the earlier note", text)
        self.assertIn("last paragraph", text)

    def test_follow_does_not_report_lines_abandoned_after_the_cursor(self):
        # The rewind on L5 abandons L3-L4, which are past a follower sitting at L2. It
        # never received them, so naming them would ask it to retire anchors it never had.
        path = self.write_transcript("eeeeeeee-1111-4111-8111-ffffffffffff", [
            user(0, "opening"), planner(1, "answer one"),
            user(2, "detour"), planner(3, "side branch"),
            user(2, "asked again"), planner(3, "new answer")])
        self.assertEqual(ag.abandoned_line_numbers(path), {3, 4})
        text = cli.render_context(path, after_line=2)
        self.assertIn("# REMOVED_BEFORE_CURSOR: none", text)
        self.assertNotIn("side branch", text)
        self.assertIn("[L6] ASSISTANT: new answer", text)

    def test_evidence_still_reads_an_abandoned_line_through_a_resolved_target(self):
        self.assertIn("Abandoned answer.",
                      cli.read_evidence(cli.resolve_target(REWOUND_ID), line=2, context=0))

    def test_search_matches_the_live_branch_only(self):
        self.assertEqual([hit["id"] for hit in
                          cli.search_sessions("live question", source="antigravity")],
                         [REWOUND_ID])
        self.assertEqual(cli.search_sessions("abandoned first", source="antigravity"), [])


if __name__ == "__main__":
    unittest.main()
