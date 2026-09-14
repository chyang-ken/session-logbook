"""Kimi Code CLI source tests (synthetic fixtures only)."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sources import kimi

FIXTURES = Path(__file__).parent / "fixtures" / "kimi"
SESSIONS = FIXTURES / "sessions"
A_ID = "session_aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
B_ID = "session_bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
A_DIR = SESSIONS / "wd_my-app_0123456789ab" / A_ID
B_DIR = SESSIONS / "wd_other-app_fedcba987654" / B_ID
A_MAIN = A_DIR / "agents" / "main" / "wire.jsonl"
A_SUB = A_DIR / "agents" / "agent-0" / "wire.jsonl"
B_MAIN = B_DIR / "agents" / "main" / "wire.jsonl"


class FixturesPresentTests(unittest.TestCase):
    def test_fixtures_present(self):
        for p in (A_MAIN, A_SUB, B_MAIN, A_DIR / "state.json", B_DIR / "state.json",
                  FIXTURES / "session_index.jsonl"):
            self.assertTrue(p.exists(), p)

    def test_module_globals(self):
        self.assertTrue(hasattr(kimi, "KIMI_HOME"))
        self.assertTrue(hasattr(kimi, "KIMI_SESSIONS_ROOT"))
        self.assertTrue(hasattr(kimi, "SESSION_INDEX_PATH"))


class PathTests(unittest.TestCase):
    def test_session_id_and_agent_from_path(self):
        self.assertEqual(kimi.session_id_for_path(A_MAIN), A_ID)
        self.assertEqual(kimi.session_id_for_path(A_SUB), A_ID)
        self.assertEqual(kimi.agent_id_for_path(A_MAIN), "main")
        self.assertEqual(kimi.agent_id_for_path(A_SUB), "agent-0")
        self.assertFalse(kimi.is_subagent_path(A_MAIN))
        self.assertTrue(kimi.is_subagent_path(A_SUB))

    def test_is_kimi_path_honours_directory_boundary(self):
        with mock.patch.object(kimi, "KIMI_SESSIONS_ROOT", Path("/tmp/kimi-home/sessions")):
            self.assertTrue(kimi.is_kimi_path("/tmp/kimi-home/sessions/wd_x_000000000000/session_1/agents/main/wire.jsonl"))
            self.assertTrue(kimi.is_kimi_path("/tmp/kimi-home/sessions"))
            self.assertFalse(kimi.is_kimi_path("/tmp/kimi-home/sessions_backup/session_1/agents/main/wire.jsonl"))
            self.assertFalse(kimi.is_kimi_path("/tmp/kimi-home/logs/wire.jsonl"))

    def test_home_honours_env_var(self):
        self.assertEqual(kimi._home_from_env({"KIMI_CODE_HOME": "/tmp/custom-kimi-home"}),
                         Path("/tmp/custom-kimi-home"))
        self.assertEqual(kimi._home_from_env({}), Path.home() / ".kimi-code")
        self.assertEqual(kimi.KIMI_SESSIONS_ROOT, kimi.KIMI_HOME / "sessions")
        self.assertEqual(kimi.SESSION_INDEX_PATH, kimi.KIMI_HOME / "session_index.jsonl")


class ScanTests(unittest.TestCase):
    def test_scan_yields_main_wires_only(self):
        found = sorted(kimi.scan_sessions(SESSIONS))
        self.assertEqual(found, sorted([A_MAIN, B_MAIN]))
        self.assertNotIn(A_SUB, found)

    def test_scan_handles_nonexistent_root(self):
        self.assertEqual(list(kimi.scan_sessions(SESSIONS / "nope")), [])

    def test_scan_ignores_stray_files(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            shutil.copytree(SESSIONS, tmp / "sessions")
            (tmp / "sessions" / "stray.txt").write_text("x")
            (tmp / "sessions" / "wd_empty_000000000000").mkdir()
            (tmp / "sessions" / "wd_empty_000000000000" / "session_no-wire").mkdir()
            ids = {p.parents[2].name for p in kimi.scan_sessions(tmp / "sessions")}
            self.assertEqual(ids, {A_ID, B_ID})
        finally:
            shutil.rmtree(tmp)


class FindTests(unittest.TestCase):
    def test_find_by_session_id_walks_root(self):
        self.assertEqual(kimi.find_wire_by_session_id(A_ID, SESSIONS), A_MAIN)
        self.assertEqual(kimi.find_wire_by_session_id(B_ID, SESSIONS), B_MAIN)
        self.assertIsNone(kimi.find_wire_by_session_id("session_missing", SESSIONS))

    def test_find_rejects_path_like_ids(self):
        self.assertIsNone(kimi.find_wire_by_session_id("../etc", SESSIONS))
        self.assertIsNone(kimi.find_wire_by_session_id("", SESSIONS))

    def test_find_uses_roster_before_walking(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            home = tmp / "kimi-home"
            shutil.copytree(SESSIONS, home / "sessions")
            index = home / "session_index.jsonl"
            index.write_text(json.dumps({
                "sessionId": A_ID,
                "sessionDir": str(home / "sessions" / "wd_my-app_0123456789ab" / A_ID),
                "workDir": "/Users/alice/my-app",
            }) + "\n")
            with mock.patch.object(kimi, "KIMI_SESSIONS_ROOT", home / "sessions"), \
                    mock.patch.object(kimi, "SESSION_INDEX_PATH", index), \
                    mock.patch.object(kimi, "_INDEX_CACHE", {"mtime": 0.0, "data": {}}):
                with mock.patch.object(Path, "iterdir", side_effect=AssertionError("walked the tree")):
                    self.assertEqual(
                        kimi.find_wire_by_session_id(A_ID),
                        home / "sessions" / "wd_my-app_0123456789ab" / A_ID / "agents" / "main" / "wire.jsonl")
        finally:
            shutil.rmtree(tmp)


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.meta = kimi.extract_metadata(A_MAIN)

    def test_identity_fields(self):
        self.assertEqual(self.meta["id"], A_ID)
        self.assertEqual(self.meta["source"], "kimi")
        self.assertEqual(self.meta["jsonl_path"], str(A_MAIN))
        self.assertIsNone(self.meta["cli_version"])
        self.assertEqual(self.meta["size"], A_MAIN.stat().st_size)
        self.assertIn("mtime_iso", self.meta)

    def test_project_path_from_state_json(self):
        self.assertEqual(self.meta["project_path"], "/Users/alice/my-app")

    def test_model_from_last_llm_request(self):
        self.assertEqual(self.meta["model"], "kimi-k2-turbo")

    def test_title_kept_even_when_not_custom(self):
        self.assertEqual(self.meta["custom_title"], "Explain the project")

    def test_user_turns_count_only_human_prompts(self):
        # Two origin=user prompts; the origin=task prompt does not count
        self.assertEqual(self.meta["user_turn_count"], 2)

    def test_last_stop_reason_from_last_turn_ended(self):
        self.assertEqual(self.meta["last_stop_reason"], "completed")

    def test_recent_msgs_and_first_user(self):
        roles = [m["role"] for m in self.meta["recent_msgs"]]
        self.assertEqual(roles, ["user", "assistant", "assistant", "user", "assistant"])
        self.assertEqual(self.meta["recent_msgs"][2]["text"], "The backend serves sessions over a local HTTP API.")
        self.assertEqual(self.meta["first_user_msg"], "List the files in the project and tell me what it does")
        for m in self.meta["recent_msgs"]:
            self.assertNotIn("injected", m["text"])
            self.assertNotIn("Background task", m["text"])

    def test_v14_falls_back_to_config_update_cwd_and_string_content(self):
        meta = kimi.extract_metadata(B_MAIN)
        self.assertEqual(meta["project_path"], "/Users/alice/other-app")
        self.assertEqual(meta["model"], "kimi-k2")
        self.assertEqual(meta["custom_title"], "")
        self.assertIsNone(meta["last_stop_reason"])
        self.assertEqual(meta["user_turn_count"], 1)
        self.assertEqual(meta["first_user_msg"], "Run the tests")

    def test_project_path_falls_back_to_roster_workdir(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            home = tmp / "kimi-home"
            shutil.copytree(SESSIONS, home / "sessions")
            sess = home / "sessions" / "wd_other-app_fedcba987654" / B_ID
            wire = sess / "agents" / "main" / "wire.jsonl"
            rows = [json.loads(l) for l in wire.read_text().splitlines() if l.strip()]
            for r in rows:
                r.pop("cwd", None)
            wire.write_text("".join(json.dumps(r) + "\n" for r in rows))
            index = home / "session_index.jsonl"
            index.write_text(json.dumps({"sessionId": B_ID, "sessionDir": str(sess),
                                         "workDir": "/Users/alice/from-roster"}) + "\n")
            with mock.patch.object(kimi, "SESSION_INDEX_PATH", index), \
                    mock.patch.object(kimi, "_INDEX_CACHE", {"mtime": 0.0, "data": {}}):
                self.assertEqual(kimi.extract_metadata(wire)["project_path"], "/Users/alice/from-roster")
        finally:
            shutil.rmtree(tmp)

    def test_missing_file_returns_none(self):
        self.assertIsNone(kimi.extract_metadata(A_DIR / "agents" / "main" / "missing.jsonl"))


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.conv = kimi.extract_conversation(A_MAIN)
        self.turns = self.conv["turns"]

    def test_envelope(self):
        self.assertEqual(self.conv["id"], A_ID)
        self.assertEqual(self.conv["source"], "kimi")
        self.assertEqual(self.conv["project_path"], "/Users/alice/my-app")
        self.assertEqual(self.conv["custom_title"], "Explain the project")
        self.assertEqual(self.conv["total_lines"], 35)

    def test_turn_sequence(self):
        self.assertEqual(
            [t["type"] for t in self.turns],
            ["user", "tool", "assistant", "qa", "assistant", "tool", "user", "assistant"])

    def test_user_turns_exclude_injection_and_task_messages(self):
        users = [t["text"] for t in self.turns if t["type"] == "user"]
        self.assertEqual(users, ["List the files in the project and tell me what it does",
                                 "Thanks, summarize in one line"])

    def test_assistant_text_parts_concatenated_per_step_and_think_dropped(self):
        asts = [t["text"] for t in self.turns if t["type"] == "assistant"]
        self.assertEqual(asts, ["It is a small web dashboard.",
                                "The backend serves sessions over a local HTTP API.",
                                "A local dashboard for agent sessions."])
        blob = json.dumps(self.turns)
        self.assertNotIn("I should list the files first", blob)

    def test_tool_calls_paired_with_results_by_id(self):
        tools = [t for t in self.turns if t["type"] == "tool"]
        self.assertEqual(tools[0]["name"], "Bash")
        self.assertEqual(tools[0]["summary"], "ls -la")
        self.assertEqual(tools[0]["result"], "README.md\nserver.py")
        self.assertFalse(tools[0]["is_error"])
        self.assertEqual(tools[1]["name"], "Grep")
        self.assertEqual(tools[1]["summary"], "def main in /Users/alice/my-app")
        self.assertEqual(tools[1]["result"], "grep: permission denied")
        self.assertTrue(tools[1]["is_error"])
        self.assertNotIn("AskUserQuestion", [t["name"] for t in tools])

    def test_qa_turn_matches_claude_shape(self):
        qa = [t for t in self.turns if t["type"] == "qa"][0]
        self.assertTrue(qa["ts"])
        q = qa["questions"][0]
        self.assertEqual(q["question"], "Which area should I explain first?")
        self.assertEqual(q["header"], "Focus")
        self.assertFalse(q["multiSelect"])
        self.assertEqual([o["label"] for o in q["options"]], ["Backend", "Frontend"])
        self.assertEqual(set(q["options"][0].keys()), {"label", "description", "preview"})
        self.assertEqual(q["answer"], "Backend")
        self.assertEqual(q["matched_option"], 0)

    def test_qa_other_answer_has_no_matched_option(self):
        rows = [
            {"type": "metadata", "protocol_version": "1.5", "created_at": 1, "time": 1000},
            {"type": "context.append_loop_event", "time": 2000, "event": {
                "type": "tool.call", "toolCallId": "c1", "name": "AskUserQuestion",
                "args": {"questions": [{"question": "Pick", "options": [{"label": "A"}, {"label": "B"}]}]}}},
            {"type": "interaction.request", "id": "i1", "toolCallId": "c1", "time": 3000,
             "request": {"questions": [{"question": "Pick", "options": [{"label": "A"}, {"label": "B"}]}]}},
            {"type": "interaction.resolved", "id": "i1", "time": 4000, "response": {"answers": {"Pick": "something else"}}},
            {"type": "context.append_loop_event", "time": 5000, "event": {
                "type": "tool.result", "toolCallId": "c1", "result": {"output": "ok"}}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            wire = Path(tmp) / "sessions" / "wd_x_000000000000" / "session_x" / "agents" / "main" / "wire.jsonl"
            wire.parent.mkdir(parents=True)
            wire.write_text("".join(json.dumps(r) + "\n" for r in rows))
            turns = kimi.extract_conversation(wire)["turns"]
        self.assertEqual([t["type"] for t in turns], ["qa"])
        self.assertEqual(turns[0]["questions"][0]["answer"], "something else")
        self.assertIsNone(turns[0]["questions"][0]["matched_option"])

    def test_v14_string_content_and_description_summary(self):
        turns = kimi.extract_conversation(B_MAIN)["turns"]
        self.assertEqual([t["type"] for t in turns], ["user", "tool", "assistant"])
        self.assertEqual(turns[0]["text"], "Run the tests")
        self.assertEqual(turns[1]["summary"], "python3 -m unittest")
        self.assertEqual(turns[1]["result"], "OK")
        self.assertEqual(turns[2]["text"], "All tests pass.")

    def test_truncation_applied(self):
        rows = [
            {"type": "metadata", "protocol_version": "1.5", "created_at": 1, "time": 1000},
            {"type": "context.append_message", "time": 2000, "message": {
                "role": "user", "origin": {"kind": "user"}, "content": "x" * (kimi.CONV_USER_MAX + 50)}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            wire = Path(tmp) / "sessions" / "wd_x_000000000000" / "session_x" / "agents" / "main" / "wire.jsonl"
            wire.parent.mkdir(parents=True)
            wire.write_text("".join(json.dumps(r) + "\n" for r in rows))
            turns = kimi.extract_conversation(wire)["turns"]
        self.assertIn("…(+50 chars)", turns[0]["text"])


class TranscriptTests(unittest.TestCase):
    def test_transcript_blocks(self):
        text = kimi.extract_transcript(A_MAIN)
        self.assertTrue(text.startswith(f"# Session {A_ID}\nProject: /Users/alice/my-app\nRaw lines: 35\n"))
        self.assertIn("## USER\nList the files in the project and tell me what it does\n", text)
        self.assertIn("[tool: Bash(ls -la)]\nREADME.md\nserver.py\n", text)
        self.assertIn("## ASSISTANT\nIt is a small web dashboard.\n", text)
        self.assertIn("## QA\nQ: Which area should I explain first?\n  [A] Backend\n  [B] Frontend\n→ A (selected): Backend", text)
        self.assertIn("[tool: Grep(def main in /Users/alice/my-app)]\ngrep: permission denied\n", text)
        self.assertNotIn("I should list the files first", text)
        self.assertNotIn("injected environment notes", text)

    def test_transcript_truncates_long_tool_result_only(self):
        long_out = "y" * 500
        rows = [
            {"type": "metadata", "protocol_version": "1.5", "created_at": 1, "time": 1000},
            {"type": "context.append_message", "time": 2000, "message": {
                "role": "user", "origin": {"kind": "user"}, "content": "z" * 6000}},
            {"type": "context.append_loop_event", "time": 3000, "event": {
                "type": "tool.call", "toolCallId": "c1", "name": "Bash", "args": {"command": "cat big"}}},
            {"type": "context.append_loop_event", "time": 4000, "event": {
                "type": "tool.result", "toolCallId": "c1", "result": {"output": long_out}}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            wire = Path(tmp) / "sessions" / "wd_x_000000000000" / "session_x" / "agents" / "main" / "wire.jsonl"
            wire.parent.mkdir(parents=True)
            wire.write_text("".join(json.dumps(r) + "\n" for r in rows))
            text = kimi.extract_transcript(wire)
        self.assertIn("z" * 6000, text)
        self.assertNotIn(long_out, text)
        self.assertIn("…[+300 chars, ~1 lines]", text)


class SearchTextTests(unittest.TestCase):
    def test_search_text_from_line(self):
        rows = [json.loads(l) for l in A_MAIN.read_text().splitlines() if l.strip()]
        texts = [kimi.search_text_from_line(r) for r in rows]
        self.assertIn("List the files in the project and tell me what it does", texts)
        self.assertIn("It is a small web dashboard.", texts)
        joined = "\n".join(texts)
        self.assertNotIn("injected environment notes", joined)
        self.assertNotIn("I should list the files first", joined)
        self.assertNotIn("Background task finished", joined)

    def test_message_role_from_line(self):
        rows = [json.loads(l) for l in A_MAIN.read_text().splitlines() if l.strip()]
        roles = {kimi.message_role_from_line(r)[0] for r in rows}
        self.assertEqual(roles, {None, "user", "assistant"})


if __name__ == "__main__":
    unittest.main()
