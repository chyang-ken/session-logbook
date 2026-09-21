"""Turn-level slicing of an anchored transcript, on every source that renders `[U#]`.

The renderer already decides what a human turn is and prints that decision as `[U#]`.
These tests hold the other half of the contract: a caller can ask for those turns back in
the same spelling the transcript printed, on every source, and is always told when what it
received is a slice. A parameter that is accepted and then ignored is the failure mode
this file exists to prevent, so every rejection below asserts an error rather than a
quietly complete transcript.
"""

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import server
import session_logbook_cli as cli
from sources import anchored_transcript as at
from sources import codex as codex_source
from sources import devin as devin_source
from sources import kimi as kimi_source
from sources import pi as pi_source

PI_FIXTURES = Path(__file__).parent / "fixtures" / "pi"
AG_ID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"


def _write_jsonl(path: Path, rows: list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


class TurnSliceRuleTests(unittest.TestCase):
    """The rule itself, independent of any source."""

    # One line per spelling the renderers produce, so a new renderer that invents a fourth
    # shape fails here rather than silently becoming unsliceable.
    BODY = "\n".join([
        "━━━━━━━━━━ [U1] [L4] USER 2026-01-01T00:00:00 ━━━━━━━━━━",
        "first question",
        "[L5] ASSISTANT: first answer",
        "[U2] [L9] USER 2026-01-01T00:01:00",
        "second question",
        "## [U3] [N7] USER",
        "third question",
    ])

    def test_every_rendered_spelling_of_a_user_turn_is_addressable(self):
        self.assertEqual(at.turn_anchors(self.BODY), [(1, 0), (2, 3), (3, 5)])

    def test_no_options_returns_the_body_untouched_and_says_nothing(self):
        self.assertEqual(at.slice_from_turn(self.BODY), (self.BODY, []))

    def test_last_turns_keeps_the_tail_and_reports_what_it_dropped(self):
        body, notes = at.slice_from_turn(self.BODY, last_turns=2)
        self.assertTrue(body.startswith("[U2]"))
        self.assertNotIn("first question", body)
        self.assertIn("third question", body)
        self.assertIn("# TURN_SLICE: U2-U3 (2 of 3 rendered turns)", notes)
        self.assertIn("# OMITTED_BEFORE_SLICE: U1-U1", notes)

    def test_from_turn_starts_exactly_there(self):
        body, notes = at.slice_from_turn(self.BODY, first_turn=3)
        self.assertNotIn("second question", body)
        self.assertIn("# TURN_SLICE: U3-U3 (1 of 3 rendered turns)", notes)

    def test_a_slice_never_claims_to_be_a_durable_cursor(self):
        # Verified against real history: each record of one conversation numbers from U1,
        # so a turn number cannot be carried across a rewind the way a cursor is.
        _, notes = at.slice_from_turn(self.BODY, last_turns=1)
        self.assertTrue(any("not a durable cursor" in note for note in notes))

    def test_asking_for_more_turns_than_exist_returns_all_and_says_nothing_was_dropped(self):
        body, notes = at.slice_from_turn(self.BODY, last_turns=99)
        self.assertEqual(body, self.BODY)
        self.assertIn("# TURN_SLICE: U1-U3 (3 of 3 rendered turns)", notes)
        self.assertIn("# OMITTED_BEFORE_SLICE: none; the request covered every rendered turn", notes)

    def test_a_turn_past_the_end_is_an_error_not_a_full_transcript(self):
        with self.assertRaisesRegex(ValueError, r"U9 is beyond .* last turn U3"):
            at.slice_from_turn(self.BODY, first_turn=9)

    def test_nonsense_bounds_are_errors(self):
        with self.assertRaisesRegex(ValueError, "1 or more"):
            at.slice_from_turn(self.BODY, last_turns=0)
        with self.assertRaisesRegex(ValueError, "U1 or later"):
            at.slice_from_turn(self.BODY, first_turn=0)

    def test_a_transcript_with_no_human_turn_refuses_instead_of_returning_everything(self):
        with self.assertRaisesRegex(ValueError, "renders no \\[U#\\] human turn"):
            at.slice_from_turn("[L1] ASSISTANT: nobody spoke", last_turns=1)

    def test_a_cursor_trimmed_body_slices_against_the_numbers_it_actually_shows(self):
        # Claude's delta follow keeps the original numbering, so a body may begin at U7.
        trimmed = "\n".join([
            "━━ [U7] [L70] USER t ━━", "seven",
            "━━ [U8] [L80] USER t ━━", "eight",
        ])
        body, notes = at.slice_from_turn(trimmed, last_turns=5)
        self.assertEqual(body, trimmed)
        self.assertIn("# TURN_SLICE: U7-U8 (2 of 2 rendered turns)", notes)
        body, _ = at.slice_from_turn(trimmed, first_turn=1)
        self.assertEqual(body, trimmed)

    def test_turn_references_are_read_in_either_spelling(self):
        self.assertEqual(at.parse_turn_ref("U13"), 13)
        self.assertEqual(at.parse_turn_ref("13"), 13)
        self.assertEqual(at.parse_turn_ref(" u13 "), 13)
        with self.assertRaisesRegex(ValueError, "not a turn reference"):
            at.parse_turn_ref("banana")
        with self.assertRaisesRegex(ValueError, "not a turn reference"):
            at.parse_turn_ref("U-3")


class _SliceAcrossSources(unittest.TestCase):
    """Every source renders `[U#]`; every source must therefore honour a turn slice."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.claude_root = root / "claude-projects"
        self.codex_root = root / "codex-sessions"
        self.kimi_root = root / "kimi-home" / "sessions"
        self.devin_root = root / "devin-data"
        self.ag_root = root / "antigravity-brain"
        self.pi_root = root / "pi-sessions"

        def turns(n, at_second):
            return [f"question {i}" for i in range(1, n + 1)]

        self.claude = _write_jsonl(
            self.claude_root / "-Users-alice-my-app" / "dddddddd-dddd-dddd-dddd-dddddddddddd.jsonl",
            [row for i in (1, 2, 3) for row in (
                {"type": "user", "timestamp": f"2026-08-20T10:0{i}:00Z", "cwd": "/Users/alice/my-app",
                 "message": {"content": f"question {i}"}},
                {"type": "assistant", "timestamp": f"2026-08-20T10:0{i}:01Z", "cwd": "/Users/alice/my-app",
                 "message": {"content": [{"type": "text", "text": f"answer {i}"}], "stop_reason": "end_turn"}},
            )])

        self.codex = _write_jsonl(
            self.codex_root / "2026" / "08" / "20" /
            "rollout-2026-08-20T10-00-00-cccccccc-1111-2222-3333-cccccccccccc.jsonl",
            [{"timestamp": "2026-08-20T10:00:00Z", "type": "session_meta", "payload": {
                "id": "cccccccc-1111-2222-3333-cccccccccccc",
                "cwd": "/Users/alice/other-app", "thread_source": "user"}}]
            + [row for i in (1, 2, 3) for row in (
                {"timestamp": f"2026-08-20T10:0{i}:01Z", "type": "response_item", "payload": {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": f"question {i}"}]}},
                {"timestamp": f"2026-08-20T10:0{i}:02Z", "type": "response_item", "payload": {
                    "type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": f"answer {i}"}]}},
            )])

        kimi_session = self.kimi_root / "wd_third-app_0123456789ab" / "session_dddddddd-1111-2222-3333-dddddddddddd"
        kimi_session.mkdir(parents=True)
        (kimi_session / "state.json").write_text(json.dumps({
            "createdAt": "2026-08-20T10:00:00.000Z", "updatedAt": "2026-08-20T10:05:00.000Z",
            "title": "Rotate the token", "isCustomTitle": False, "workDir": "/Users/alice/third-app",
            "agents": {"main": {}}, "custom": {},
        }), encoding="utf-8")
        self.kimi = _write_jsonl(kimi_session / "agents" / "main" / "wire.jsonl", [
            {"type": "metadata", "protocol_version": "1.5", "created_at": 1755684000000, "time": 1755684000000}]
            + [row for i in (1, 2, 3) for row in (
                {"type": "turn.prompt", "time": 1755684000000 + i * 1000, "origin": {"kind": "user"},
                 "input": [{"type": "text", "text": f"question {i}"}]},
                {"type": "context.append_message", "time": 1755684000000 + i * 1000, "message": {
                    "role": "user", "origin": {"kind": "user"},
                    "content": [{"type": "text", "text": f"question {i}"}]}},
                {"type": "context.append_loop_event", "time": 1755684000500 + i * 1000, "event": {
                    "type": "content.part", "stepUuid": f"s{i}",
                    "part": {"type": "text", "text": f"answer {i}"}}},
            )])

        self.antigravity = _write_jsonl(
            self.ag_root / AG_ID / ".system_generated" / "logs" / "transcript.jsonl",
            [row for i in (1, 2, 3) for row in (
                {"step_index": (i - 1) * 2, "source": "USER_EXPLICIT", "type": "USER_INPUT",
                 "status": "DONE", "created_at": f"2026-08-20T09:0{i}:00Z",
                 "content": f"<USER_REQUEST>question {i}</USER_REQUEST>"},
                {"step_index": (i - 1) * 2 + 1, "source": "MODEL", "type": "PLANNER_RESPONSE",
                 "status": "DONE", "created_at": f"2026-08-20T09:0{i}:01Z",
                 "content": f"answer {i}"},
            )])

        shutil.copytree(PI_FIXTURES, self.pi_root)
        self.pi = next(self.pi_root.glob("*/*.jsonl"))

        db = self.devin_root / "sessions.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(devin_source, "DEVIN_ROOT", self.devin_root):
            db = devin_source.database_path()
            db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        self.addCleanup(conn.close)
        conn.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE sessions (id TEXT PRIMARY KEY, working_directory TEXT,
              model TEXT, title TEXT, created_at INTEGER, last_activity_at INTEGER,
              main_chain_id INTEGER, hidden INTEGER);
            CREATE TABLE message_nodes (row_id INTEGER PRIMARY KEY, session_id TEXT,
              node_id INTEGER, parent_node_id INTEGER, chat_message TEXT, created_at INTEGER);
            INSERT INTO sessions VALUES ('alpha', '/Users/alice/my-app', 'example-model',
              'Synthetic review', 1700000000, 1700000010, 60, 0);
        ''')
        parent = None
        for i in (1, 2, 3):
            for role, text in (("user", f"question {i}"), ("assistant", f"answer {i}")):
                node = i * 20 + (0 if role == "user" else 10)
                message = {"role": role, "content": text}
                if role == "user":
                    message["metadata"] = {"is_user_input": True}
                conn.execute('INSERT INTO message_nodes VALUES (?,?,?,?,?,?)',
                             (node, 'alpha', node, parent, json.dumps(message), 1700000000 + node))
                parent = node
        conn.commit()

        self.patches = [
            mock.patch.object(server, "PROJECTS_DIR", self.claude_root),
            mock.patch.object(server, "STATE_FILE", root / "state.json"),
            mock.patch.object(server, "SCAN_CACHE_FILE", root / "cache.json"),
            mock.patch.object(server, "SCAN_CACHE_BACKUP_DIR", root / "backups"),
            mock.patch.object(codex_source, "CODEX_ROOT", self.codex_root),
            mock.patch.object(codex_source, "CODEX_ARCHIVED_ROOT", root / "codex-archived"),
            mock.patch.object(codex_source, "SESSION_INDEX_PATH", root / "session_index.jsonl"),
            mock.patch.object(codex_source, "_INDEX_CACHE", {"mtime_ns": None, "data": {}}),
            mock.patch.object(devin_source, "DEVIN_ROOT", self.devin_root),
            mock.patch.object(pi_source, "PI_SESSIONS_ROOT", self.pi_root),
            mock.patch.object(cli.ag_source, "AG_BRAIN", self.ag_root),
            mock.patch.object(cli.ag_source, "AG_SUMMARIES", root / "no-summaries.pb"),
            mock.patch.object(cli.ag_source, "_TITLE_CACHE", {"mtime": 0.0, "data": {}}),
            mock.patch.object(kimi_source, "KIMI_SESSIONS_ROOT", self.kimi_root),
            mock.patch.object(kimi_source, "SESSION_INDEX_PATH", self.kimi_root.parent / "index.jsonl"),
            mock.patch.object(kimi_source, "_INDEX_CACHE", {"mtime": 0.0, "data": {}}),
            mock.patch.object(server, "_cache", {}),
            mock.patch.object(server, "_state", {}),
            mock.patch.object(server, "_state_loaded", True),
            mock.patch.object(server, "_CWD_TRUTH_MAP", {}),
            mock.patch.object(server, "_CWD_INDEX_SEEN", set()),
            mock.patch.object(server, "_CWD_SEQ", {}),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.devin = devin_source.reference('alpha')

    def targets(self):
        return {"claude": self.claude, "codex": self.codex, "kimi": self.kimi,
                "antigravity": self.antigravity, "pi": self.pi, "devin": self.devin}


class SliceAcrossSourcesTests(_SliceAcrossSources):
    def test_every_source_renders_addressable_turns(self):
        for name, target in self.targets().items():
            with self.subTest(source=name):
                anchors = at.turn_anchors(cli.render_context(target))
                self.assertTrue(anchors, f"{name} renders no [U#] a caller could ask for")

    def test_every_source_keeps_only_the_last_turn_when_asked(self):
        for name, target in self.targets().items():
            with self.subTest(source=name):
                full = cli.render_context(target)
                total = at.turn_anchors(full)[-1][0]
                sliced = cli.render_context(target, last_turns=1)
                self.assertIn(f"# TURN_SLICE: U{total}-U{total} (1 of", sliced)
                kept = at.turn_anchors(sliced.split("# TURN_SLICE")[0] + sliced.split("\n\n", 1)[-1])
                self.assertEqual([number for number, _ in at.turn_anchors(sliced)], [total])
                self.assertNotIn("question 1", sliced.split("# A [U#]")[-1])

    def test_a_slice_keeps_the_header_that_says_it_is_one(self):
        for name, target in self.targets().items():
            with self.subTest(source=name):
                sliced = cli.render_context(target, last_turns=1)
                self.assertIn("# SESSION_ID:", sliced)
                self.assertIn("# NEXT_CURSOR:", sliced)
                self.assertIn("# TURN_SLICE:", sliced)
                self.assertIn("# OMITTED_BEFORE_SLICE:", sliced)

    def test_slicing_never_moves_the_cursor_a_follower_would_use_next(self):
        for name, target in self.targets().items():
            with self.subTest(source=name):
                def cursor(text):
                    return [line for line in text.splitlines() if line.startswith("# NEXT_CURSOR:")]
                self.assertEqual(cursor(cli.render_context(target)),
                                 cursor(cli.render_context(target, last_turns=1)))

    def test_an_out_of_range_turn_is_refused_on_every_source(self):
        for name, target in self.targets().items():
            with self.subTest(source=name):
                with self.assertRaises(ValueError):
                    cli.render_context(target, first_turn=999)

    def test_from_turn_and_last_turns_agree_on_every_source(self):
        for name, target in self.targets().items():
            with self.subTest(source=name):
                total = at.turn_anchors(cli.render_context(target))[-1][0]
                self.assertEqual(cli.render_context(target, first_turn=total),
                                 cli.render_context(target, last_turns=1))


class TurnSliceCommandTests(_SliceAcrossSources):
    def test_context_accepts_the_turn_reference_a_transcript_prints(self):
        args = cli.parse_args(["context", "x", "--from-turn", "U13"])
        self.assertEqual(args.from_turn, "U13")
        self.assertIsNone(args.last_turns)

    def test_the_two_bounds_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            cli.parse_args(["context", "x", "--from-turn", "U2", "--last-turns", "2"])

    def test_follow_refuses_turn_options_rather_than_ignoring_them(self):
        # follow's cursor is a continuation point; a turn number is not one, and accepting
        # it silently would teach a follower to trust a bound that was never applied.
        with self.assertRaises(SystemExit):
            cli.parse_args(["follow", "x", "--cursor-line", "5", "--last-turns", "2"])

    def test_the_command_prints_only_the_requested_tail(self):
        import io, contextlib
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli.main(["context", str(self.claude), "--last-turns", "1"])
        printed = buffer.getvalue()
        self.assertIn("# TURN_SLICE: U3-U3 (1 of 3 rendered turns)", printed)
        self.assertIn("question 3", printed)
        self.assertNotIn("question 1", printed.split("# A [U#]")[-1])


if __name__ == "__main__":
    unittest.main()
