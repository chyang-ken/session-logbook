"""Codex source tests."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from sources import codex

FIXTURES = Path(__file__).parent / "fixtures" / "codex"


class FixturesPresentTests(unittest.TestCase):
    def test_all_fixtures_present(self):
        expected = {
            "basic_main", "guardian", "worker_thread_spawn",
            "with_thread_name", "with_compacted", "with_subagent_spawn",
            "with_aborted", "auto_review_fallback",
        }
        actual = {p.stem for p in FIXTURES.glob("*.jsonl")}
        self.assertEqual(expected, actual)

    def test_codex_module_importable(self):
        self.assertTrue(hasattr(codex, "CODEX_ROOT"))


class ScanFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        target = self.tmp / "2026" / "05" / "14"
        target.mkdir(parents=True)
        for fname in ["basic_main", "guardian", "worker_thread_spawn",
                      "with_thread_name", "auto_review_fallback"]:
            shutil.copy(FIXTURES / f"{fname}.jsonl", target / f"rollout-{fname}.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_scan_filters_guardian(self):
        names = {p.stem for p in codex.scan_sessions(self.tmp)}
        self.assertNotIn("rollout-guardian", names)

    def test_scan_filters_worker_thread_spawn(self):
        names = {p.stem for p in codex.scan_sessions(self.tmp)}
        self.assertNotIn("rollout-worker_thread_spawn", names)

    def test_scan_filters_codex_auto_review(self):
        names = {p.stem for p in codex.scan_sessions(self.tmp)}
        self.assertNotIn("rollout-auto_review_fallback", names)

    def test_scan_keeps_main_threads(self):
        names = {p.stem for p in codex.scan_sessions(self.tmp)}
        self.assertIn("rollout-basic_main", names)
        self.assertIn("rollout-with_thread_name", names)

    def test_scan_handles_nonexistent_root(self):
        gone = self.tmp / "does_not_exist"
        self.assertEqual(list(codex.scan_sessions(gone)), [])


class ArchivedSessionsTests(unittest.TestCase):
    """Archived sessions: flat archived_sessions scan, codex_archived flag, and dual-root lookup.

    When Codex archives a session, it moves the rollout file from sessions/YYYY/MM/DD/ to
    the flat archived_sessions/ directory. The dashboard must scan both roots and make source
    detection / path lookup recognize both roots, otherwise archived sessions disappear from
    the board or conversation endpoints use the wrong parser after misclassifying them as Claude.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # Active root with YYYY/MM/DD nesting
        self.active_root = self.tmp / "sessions"
        active_day = self.active_root / "2026" / "05" / "14"
        active_day.mkdir(parents=True)
        self.active_file = active_day / "rollout-basic_main.jsonl"
        shutil.copy(FIXTURES / "basic_main.jsonl", self.active_file)
        # Archived root: flat, no date nesting
        self.archived_root = self.tmp / "archived_sessions"
        self.archived_root.mkdir(parents=True)
        self.archived_file = self.archived_root / "rollout-archived.jsonl"
        shutil.copy(FIXTURES / "with_thread_name.jsonl", self.archived_file)
        self.archived_id = "019daa74-eeee-eeee-eeee-eeeeeeeeeeee"  # with_thread_name id
        self._old_root = codex.CODEX_ROOT
        self._old_arch = codex.CODEX_ARCHIVED_ROOT
        codex.CODEX_ROOT = self.active_root
        codex.CODEX_ARCHIVED_ROOT = self.archived_root

    def tearDown(self):
        codex.CODEX_ROOT = self._old_root
        codex.CODEX_ARCHIVED_ROOT = self._old_arch
        shutil.rmtree(self.tmp)

    def test_scan_default_includes_flat_archived_dir(self):
        # Default scan without root argument includes both active and archived roots
        names = {p.stem for p in codex.scan_sessions()}
        self.assertIn("rollout-basic_main", names)
        self.assertIn("rollout-archived", names)

    def test_is_codex_path_recognizes_both_roots(self):
        self.assertTrue(codex.is_codex_path(self.active_file))
        self.assertTrue(codex.is_codex_path(self.archived_file))
        self.assertFalse(
            codex.is_codex_path("/Users/alice/.claude/projects/x/abc.jsonl"))

    def test_extract_metadata_flags_archived(self):
        self.assertTrue(codex.extract_metadata(self.archived_file)["codex_archived"])
        self.assertFalse(codex.extract_metadata(self.active_file)["codex_archived"])

    def test_find_rollout_locates_archived_session(self):
        # The archived fixture filename is rollout-archived.jsonl and contains no uuid, so this
        # also locks the phase-2 fallback: when filename prefilter misses, the full scan still
        # matches by session_meta.id. Correctness must not depend on filenames containing ids.
        self.assertNotIn(self.archived_id, self.archived_file.name)
        p, forked = codex.find_rollout_by_session_id(self.archived_id)
        self.assertEqual(p, self.archived_file)
        self.assertIsNone(forked)

    def test_is_codex_path_rejects_sibling_prefix_dirs(self):
        # Directory-boundary check: sibling directories such as sessions_backup or
        # archived_sessions_old share a prefix but are not Codex roots and must not match.
        self.assertFalse(codex.is_codex_path(self.tmp / "sessions_backup" / "x.jsonl"))
        self.assertFalse(
            codex.is_codex_path(self.tmp / "archived_sessions_old" / "y.jsonl"))


class ExtractMetadataTests(unittest.TestCase):
    def test_basic_fields(self):
        m = codex.extract_metadata(FIXTURES / "basic_main.jsonl")
        self.assertIsNotNone(m)
        self.assertEqual(m["id"], "019e2900-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        self.assertEqual(m["project_path"], "/Users/alice/test-proj")
        self.assertEqual(m["model"], "gpt-5.3-codex")
        self.assertEqual(m["cli_version"], "0.131.0-alpha.9")
        self.assertEqual(m["source"], "codex")
        self.assertEqual(m["jsonl_path"], str(FIXTURES / "basic_main.jsonl"))
        self.assertIn("mtime", m)
        self.assertIn("size", m)

    def test_custom_title_takes_last(self):
        m = codex.extract_metadata(FIXTURES / "with_thread_name.jsonl")
        self.assertEqual(m["custom_title"], "Final title")

    def test_custom_title_default_empty(self):
        m = codex.extract_metadata(FIXTURES / "basic_main.jsonl")
        self.assertEqual(m["custom_title"], "")

    def test_user_turn_count_skips_environment_context(self):
        m = codex.extract_metadata(FIXTURES / "basic_main.jsonl")
        # basic_main has two input_text blocks in the user message: one environment_context
        # and one 'hello codex'. The whole message counts as one user turn; the environment
        # injection must not make it 0 or 2.
        self.assertEqual(m["user_turn_count"], 1)

    def test_last_stop_reason_complete(self):
        m = codex.extract_metadata(FIXTURES / "basic_main.jsonl")
        self.assertEqual(m["last_stop_reason"], "complete")

    def test_last_stop_reason_aborted(self):
        m = codex.extract_metadata(FIXTURES / "with_aborted.jsonl")
        self.assertEqual(m["last_stop_reason"], "aborted")

    def test_last_stop_reason_dangling_none(self):
        m = codex.extract_metadata(FIXTURES / "with_thread_name.jsonl")
        # with_thread_name has no task_complete / aborted / error
        self.assertIsNone(m["last_stop_reason"])

    def test_top_level_cwd_preserved_for_frontend_grouping(self):
        # cwd is preserved as-is; frontend projectKey handles grouping so Claude and Codex with the same cwd merge
        m = codex.extract_metadata(FIXTURES / "with_subagent_spawn.jsonl")
        self.assertEqual(m["project_path"], "/Users/alice")

    def test_normalize_path_passthrough(self):
        # Real paths are returned as-is, only stripping a trailing slash
        self.assertEqual(codex._normalize_project_path("/root"), "/root")
        self.assertEqual(codex._normalize_project_path("/root/"), "/root")
        self.assertEqual(codex._normalize_project_path("/root/some-proj"), "/root/some-proj")
        self.assertEqual(codex._normalize_project_path("/Users/alice"), "/Users/alice")
        self.assertEqual(codex._normalize_project_path("/home/alice"), "/home/alice")
        self.assertEqual(
            codex._normalize_project_path("/Users/alice/my-app-a"),
            "/Users/alice/my-app-a",
        )
        # Missing cwd falls back to "~", equivalent to running in home
        self.assertEqual(codex._normalize_project_path(""), "~")
        self.assertEqual(codex._normalize_project_path("/"), "~")
        self.assertEqual(codex._normalize_project_path("~"), "~")

    def test_recent_msgs_track_user_and_assistant(self):
        m = codex.extract_metadata(FIXTURES / "basic_main.jsonl")
        roles = [msg["role"] for msg in m["recent_msgs"]]
        self.assertIn("user", roles)
        self.assertIn("assistant", roles)

    def test_first_user_msg_skips_environment_context(self):
        m = codex.extract_metadata(FIXTURES / "basic_main.jsonl")
        self.assertEqual(m["first_user_msg"], "hello codex")

    def test_agents_md_instructions_filtered_from_user_text(self):
        """The first user message in Codex sessions often includes '# AGENTS.md instructions ...'.
        Previously missing this filter added +1 to user_turn_count, showed AGENTS in
        first_user_msg, and let pasted AGENTS text match search. It is now stripped like
        environment_context.
        """
        import os
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            lines = [
                {"timestamp": "2026-05-14T10:00:00Z", "type": "session_meta",
                 "payload": {"id": "sid-agents", "cwd": "/Users/alice/my-app",
                             "originator": "Codex Desktop", "cli_version": "0.131.0",
                             "source": "cli"}},
                # Real Codex shape: the first user message is [AGENTS.md injection] + [real question]
                {"timestamp": "2026-05-14T10:00:01Z", "type": "response_item",
                 "payload": {"type": "message", "role": "user", "content": [
                     {"type": "input_text", "text": "# AGENTS.md instructions for /Users/alice/my-app\n\n<INSTRUCTIONS>\nUse plain language.\n</INSTRUCTIONS>"},
                     {"type": "input_text", "text": "<environment_context><cwd>/x</cwd></environment_context>"},
                     {"type": "input_text", "text": "write me a script"},
                 ]}},
                {"timestamp": "2026-05-14T10:00:02Z", "type": "response_item",
                 "payload": {"type": "message", "role": "assistant", "content": [
                     {"type": "output_text", "text": "OK"},
                 ]}},
            ]
            f.write("\n".join(json.dumps(x) for x in lines) + "\n")
            path = f.name
        try:
            m = codex.extract_metadata(Path(path))
            # user_turn_count = 1: one message; injection makes it neither 0 nor doubled
            self.assertEqual(m["user_turn_count"], 1)
            # first_user is the real question, not the AGENTS injection
            self.assertEqual(m["first_user_msg"], "write me a script")
            self.assertNotIn("AGENTS.md", m["first_user_msg"])
            # user entries in recent_msgs should not be AGENTS either
            user_msgs = [msg for msg in m["recent_msgs"] if msg["role"] == "user"]
            self.assertEqual(len(user_msgs), 1)
            self.assertEqual(user_msgs[0]["text"], "write me a script")
            # conversation view is consistent
            conv = codex.extract_conversation(Path(path))
            users = [t for t in conv["turns"] if t["type"] == "user"]
            self.assertEqual(len(users), 1)
            self.assertEqual(users[0]["text"], "write me a script")
        finally:
            os.unlink(path)

    def test_recent_msgs_preserves_chronological_order(self):
        """recent_msgs used to be all users first, then all assistants.
        A real user->assistant->user->assistant sequence was flattened into user user user
        assistant assistant, making card previews show a fake conversation order. It now merges by ts.
        """
        import os
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            lines = [
                {"timestamp": "2026-05-14T10:00:00Z", "type": "session_meta",
                 "payload": {"id": "sid-order", "cwd": "/x",
                             "originator": "codex-tui", "cli_version": "0.131.0",
                             "source": "cli"}},
                {"timestamp": "2026-05-14T10:01:00Z", "type": "response_item",
                 "payload": {"type": "message", "role": "user",
                             "content": [{"type": "input_text", "text": "u1"}]}},
                {"timestamp": "2026-05-14T10:01:10Z", "type": "response_item",
                 "payload": {"type": "message", "role": "assistant",
                             "content": [{"type": "output_text", "text": "a1"}]}},
                {"timestamp": "2026-05-14T10:02:00Z", "type": "response_item",
                 "payload": {"type": "message", "role": "user",
                             "content": [{"type": "input_text", "text": "u2"}]}},
                {"timestamp": "2026-05-14T10:02:10Z", "type": "response_item",
                 "payload": {"type": "message", "role": "assistant",
                             "content": [{"type": "output_text", "text": "a2"}]}},
            ]
            f.write("\n".join(json.dumps(x) for x in lines) + "\n")
            path = f.name
        try:
            m = codex.extract_metadata(Path(path))
            # Real order: u1, a1, u2, a2
            actual = [(msg["role"], msg["text"]) for msg in m["recent_msgs"]]
            self.assertEqual(actual, [
                ("user", "u1"), ("assistant", "a1"),
                ("user", "u2"), ("assistant", "a2"),
            ], f"recent_msgs should be sorted by ascending ts, got: {actual}")
        finally:
            os.unlink(path)


class ExtractConversationTests(unittest.TestCase):
    def test_pairs_function_call(self):
        conv = codex.extract_conversation(FIXTURES / "basic_main.jsonl")
        tools = [t for t in conv["turns"] if t["type"] == "tool"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], "exec_command")
        self.assertIn("ls", tools[0]["summary"])
        self.assertIn("file1", tools[0]["result"])

    def test_filters_developer_reasoning_and_event_duplicates(self):
        conv = codex.extract_conversation(FIXTURES / "basic_main.jsonl")
        # Should contain only: 1 user + 1 assistant + 1 tool
        # No developer message / reasoning / event_msg.agent_message duplicate
        types = sorted([t["type"] for t in conv["turns"]])
        self.assertEqual(types, ["assistant", "tool", "user"])

    def test_user_text_skips_environment_context(self):
        conv = codex.extract_conversation(FIXTURES / "basic_main.jsonl")
        users = [t for t in conv["turns"] if t["type"] == "user"]
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["text"], "hello codex")

    def test_subagent_spawn_translated(self):
        conv = codex.extract_conversation(FIXTURES / "with_subagent_spawn.jsonl")
        spawns = [t for t in conv["turns"] if t["type"] == "subagent_spawn"]
        self.assertEqual(len(spawns), 1)
        self.assertEqual(spawns[0]["name"], "Hypatia")
        self.assertIn("OCR", spawns[0]["description"])

    def test_skips_compacted(self):
        conv = codex.extract_conversation(FIXTURES / "with_compacted.jsonl")
        users = [t for t in conv["turns"] if t["type"] == "user"]
        self.assertEqual([u["text"] for u in users], ["q1", "q2"])
        for u in users:
            self.assertNotEqual(u["text"], "old q")

    def test_conversation_meta_fields(self):
        conv = codex.extract_conversation(FIXTURES / "basic_main.jsonl")
        self.assertEqual(conv["source"], "codex")
        self.assertEqual(conv["id"], "019e2900-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        self.assertEqual(conv["project_path"], "/Users/alice/test-proj")
        self.assertGreater(conv["total_lines"], 0)

    def test_conversation_custom_title_from_thread_name(self):
        conv = codex.extract_conversation(FIXTURES / "with_thread_name.jsonl")
        self.assertEqual(conv["custom_title"], "Final title")

    def test_skips_spawn_agent_function_call(self):
        """spawn_agent function_call appears with collab_agent_spawn_end.
        The former is a low-level hook while the latter has friendly fields; the frontend only
        shows the translated subagent_spawn turn from the latter.
        """
        import tempfile, os
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            lines = [
                {"timestamp": "2026-04-22T22:30:00Z", "type": "session_meta",
                 "payload": {"id": "sid", "timestamp": "2026-04-22T22:30:00Z",
                             "cwd": "/x", "originator": "Codex Desktop",
                             "cli_version": "0.122.0", "source": "cli"}},
                {"timestamp": "2026-04-22T22:30:01Z", "type": "response_item",
                 "payload": {"type": "function_call", "name": "spawn_agent",
                             "arguments": '{"agent_type":"worker","message":"x"}',
                             "call_id": "c1"}},
                {"timestamp": "2026-04-22T22:30:02Z", "type": "response_item",
                 "payload": {"type": "function_call_output",
                             "call_id": "c1", "output": "ok"}},
                {"timestamp": "2026-04-22T22:30:03Z", "type": "event_msg",
                 "payload": {"type": "collab_agent_spawn_end",
                             "new_agent_nickname": "Hypatia",
                             "new_agent_role": "worker",
                             "prompt": "Run OCR"}},
            ]
            f.write("\n".join(json.dumps(x) for x in lines) + "\n")
            path = f.name
        try:
            import json as _json  # avoid shadowing
            conv = codex.extract_conversation(Path(path))
            tools = [t for t in conv["turns"] if t["type"] == "tool"]
            spawns = [t for t in conv["turns"] if t["type"] == "subagent_spawn"]
            # spawn_agent should not become a tool turn
            self.assertEqual(len(tools), 0,
                             f"unexpected tool turns: {tools}")
            # collab_agent_spawn_end should be the only spawn signal
            self.assertEqual(len(spawns), 1)
            self.assertEqual(spawns[0]["name"], "Hypatia")
        finally:
            os.unlink(path)

    def test_tool_output_list_robust(self):
        """Tools such as view_image return output as list[dict], not string.
        Previously .strip() raised AttributeError, broke the loop, and truncated the transcript
        at that line. It is now normalized to the [image] placeholder.
        """
        self.assertEqual(codex._normalize_tool_output(None), "")
        self.assertEqual(codex._normalize_tool_output(""), "")
        self.assertEqual(codex._normalize_tool_output("hello"), "hello")
        # list of input_image
        out = codex._normalize_tool_output([
            {"type": "input_image", "image_url": "data:image/jpeg;base64,XXX"},
            {"type": "input_image", "image_url": "data:image/jpeg;base64,YYY"},
        ])
        self.assertEqual(out, "[image]\n[image]")
        # list with text dict
        self.assertEqual(
            codex._normalize_tool_output([{"text": "hi"}]),
            "hi",
        )
        # dict
        out = codex._normalize_tool_output({"foo": "bar"})
        self.assertIn("foo", out)

    def test_extract_metadata_uses_tail_buffer(self):
        """Large files should not trigger full scans; head + tail reads decouple runtime from file size."""
        import os, time
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            # session_meta is in the head
            f.write(json.dumps({
                "timestamp": "2026-05-14T10:00:00Z", "type": "session_meta",
                "payload": {"id": "huge-session", "cwd": "/Users/alice/test",
                            "originator": "codex-tui", "cli_version": "0.131.0", "source": "cli"},
            }) + "\n")
            f.write(json.dumps({
                "timestamp": "2026-05-14T10:00:01Z", "type": "turn_context",
                "payload": {"turn_id": "t1", "cwd": "/Users/alice/test", "model": "gpt-5.5"},
            }) + "\n")
            # Fill the middle with many base64 image_url placeholders, about 10KB per line, to make a large file
            big_line = json.dumps({
                "timestamp": "2026-05-14T10:00:02Z", "type": "response_item",
                "payload": {"type": "function_call_output", "call_id": "c0",
                            "output": [{"type": "input_image", "image_url": "data:img;base64," + "X" * 9000}]},
            })
            for _ in range(200):  # ~2MB
                f.write(big_line + "\n")
            # Put real signals in the tail
            f.write(json.dumps({
                "timestamp": "2026-05-14T10:30:00Z", "type": "response_item",
                "payload": {"type": "message", "role": "user",
                            "content": [{"type": "input_text", "text": "last user input"}]},
            }) + "\n")
            f.write(json.dumps({
                "timestamp": "2026-05-14T10:30:01Z", "type": "event_msg",
                "payload": {"type": "thread_name_updated", "thread_name": "Tail title"},
            }) + "\n")
            f.write(json.dumps({
                "timestamp": "2026-05-14T10:30:02Z", "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "t1"},
            }) + "\n")
            path = f.name
        try:
            size_kb = os.path.getsize(path) / 1024
            self.assertGreater(size_kb, 1500, f"fixture should be >1.5MB, got {size_kb}KB")
            # Performance: head+tail should be fast; a full scan of this file would approach seconds
            t0 = time.perf_counter()
            m = codex.extract_metadata(Path(path))
            elapsed = time.perf_counter() - t0
            self.assertLess(elapsed, 0.5,
                            f"extract_metadata should be <0.5s with head+tail, got {elapsed*1000:.1f}ms")
            # Field correctness
            self.assertEqual(m["id"], "huge-session")
            self.assertEqual(m["model"], "gpt-5.5")  # read from head
            self.assertEqual(m["custom_title"], "Tail title")  # read from tail
            self.assertEqual(m["last_stop_reason"], "complete")  # read from tail
            # Large-file user_turn_count clamps to at least 2, even if tail contains only one user
            self.assertGreaterEqual(m["user_turn_count"], 1)
            # recent_msgs uses the tail
            user_texts = [msg["text"] for msg in m["recent_msgs"] if msg["role"] == "user"]
            self.assertIn("last user input", user_texts)
        finally:
            os.unlink(path)

    def test_extract_transcript_handles_view_image_output(self):
        """Run one real file to confirm view_image does not stop total_lines early.
        Skip when this fixture is not present under ~/.codex/sessions, such as in CI.
        """
        target = Path.home() / ".codex" / "sessions" / "2026" / "04" / "22" / "rollout-2026-04-22T22-22-13-019db8c9-e83f-7850-a705-f82a7f8bd8e2.jsonl"
        if not target.exists():
            self.skipTest("real session fixture not present locally")
        text = codex.extract_transcript(target)
        # Raw lines should be close to the real line count, around 3650, not the view_image line 71
        import re
        m = re.search(r"Raw lines: (\d+)", text)
        self.assertIsNotNone(m)
        self.assertGreater(int(m.group(1)), 1000, "transcript stopped early on view_image")
        # SUBAGENT_SPAWN should appear; this session has Averroes and Hypatia
        self.assertEqual(text.count("## SUBAGENT_SPAWN"), 2)

    def test_extract_transcript_basic(self):
        text = codex.extract_transcript(FIXTURES / "basic_main.jsonl")
        # Header info
        self.assertIn("# Session 019e2900", text)
        self.assertIn("Project: /Users/alice/test-proj", text)
        self.assertIn("Raw lines:", text)
        # user / assistant sections are present
        self.assertIn("## USER", text)
        self.assertIn("hello codex", text)
        self.assertIn("## ASSISTANT", text)
        self.assertIn("done", text)
        # tool pairing: function_call exec_command + output
        self.assertIn("[tool: exec_command(", text)
        self.assertIn("file1", text)

    def test_extract_transcript_skips_developer_reasoning_and_event_duplicates(self):
        text = codex.extract_transcript(FIXTURES / "basic_main.jsonl")
        # developer permissions injection should not appear
        self.assertNotIn("permissions instructions", text)
        # reasoning summary should not appear
        self.assertNotIn("thinking", text)
        # event_msg.agent_message is a response_item.message duplicate and should not duplicate 'done'
        self.assertEqual(text.count("## ASSISTANT\ndone"), 1)

    def test_extract_transcript_subagent_spawn(self):
        text = codex.extract_transcript(FIXTURES / "with_subagent_spawn.jsonl")
        self.assertIn("## SUBAGENT_SPAWN", text)
        self.assertIn("Spawned subagent: Hypatia", text)
        self.assertIn("OCR", text)

    def test_search_uses_codex_session_meta_id(self):
        """_search_session must use session_meta.id for Codex files, not stem.
        Codex stem is 'rollout-<date>-<uuid>' and differs from the card ID; searching a pasted
        card ID should hit, and fallback snippets should show the correct ID.
        """
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import server
        # Copy fixtures into a simulated Codex root
        tmp_root = Path(tempfile.mkdtemp())
        try:
            target = tmp_root / "2026" / "05" / "14"
            target.mkdir(parents=True)
            src = FIXTURES / "basic_main.jsonl"
            f = target / "rollout-anything-not-the-uuid.jsonl"
            f.write_text(src.read_text())
            real_id = "019e2900-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
            # Patch both roots: archived points to a missing child directory to isolate the real
            # ~/.codex/archived_sessions and keep the test hermetic.
            old_root = codex.CODEX_ROOT
            old_arch = codex.CODEX_ARCHIVED_ROOT
            codex.CODEX_ROOT = tmp_root
            codex.CODEX_ARCHIVED_ROOT = tmp_root / "__no_archive__"
            try:
                # Search the real session_meta.id; stem would miss, session_meta.id should hit
                snippets = server._search_session(f, [real_id.lower()])
                self.assertGreater(len(snippets), 0,
                                   "pasted real Codex session id should hit id_hit")
                # snippet text contains the real id, not stem
                texts = " ".join(s.get("text", "") for s in snippets)
                self.assertIn(real_id, texts,
                              f"fallback snippet should show real session id, got: {texts}")
            finally:
                codex.CODEX_ROOT = old_root
                codex.CODEX_ARCHIVED_ROOT = old_arch
        finally:
            import shutil
            shutil.rmtree(tmp_root)

    def test_extract_transcript_skips_spawn_agent(self):
        # Same as conversation: skip spawn_agent function_call and keep only collab_agent_spawn_end
        import os
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            lines = [
                {"timestamp": "2026-04-22T22:30:00Z", "type": "session_meta",
                 "payload": {"id": "sid", "cwd": "/x", "originator": "Codex Desktop",
                             "cli_version": "0.122.0", "source": "cli"}},
                {"timestamp": "2026-04-22T22:30:01Z", "type": "response_item",
                 "payload": {"type": "function_call", "name": "spawn_agent",
                             "arguments": '{"agent_type":"worker"}', "call_id": "c1"}},
                {"timestamp": "2026-04-22T22:30:02Z", "type": "event_msg",
                 "payload": {"type": "collab_agent_spawn_end",
                             "new_agent_nickname": "Hypatia", "prompt": "OCR"}},
            ]
            f.write("\n".join(json.dumps(x) for x in lines) + "\n")
            path = f.name
        try:
            text = codex.extract_transcript(Path(path))
            # [tool: spawn_agent(...)] does not appear
            self.assertNotIn("spawn_agent", text)
            # Exactly one ## SUBAGENT_SPAWN appears
            self.assertEqual(text.count("## SUBAGENT_SPAWN"), 1)
        finally:
            os.unlink(path)

    def test_invalid_file_returns_none(self):
        import tempfile, os
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write('{"type": "not_session_meta"}\n')
            path = f.name
        try:
            self.assertIsNone(codex.extract_conversation(Path(path)))
        finally:
            os.unlink(path)


class ForkResolutionTests(unittest.TestCase):
    """Codex Desktop fork-session lookup.

    Codex Desktop creates a thread root first, which is the card-visible ID, then forks child
    sessions. Each child has its own rollout file named with child uuid and timestamp, while
    the parent thread root has no rollout file. Constructing `rollout-<date>-<id>.jsonl` from
    the parent id or matching only session_meta.id will 404; lookup must follow fork lineage
    down to child sessions.
    """

    def _meta_line(self, ts, sid, parent=None):
        payload = {"id": sid, "cwd": "/Users/alice/my-app",
                   "originator": "Codex Desktop", "cli_version": "0.131.0",
                   "source": "cli"}
        if parent is not None:
            payload["parent_thread_id"] = parent
        return {"timestamp": ts, "type": "session_meta", "payload": payload}

    def _write_rollout(self, root, fname, ts, sid, parent=None):
        p = root / fname
        p.write_text(json.dumps(self._meta_line(ts, sid, parent)) + "\n")
        return p

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        day = self.root / "2026" / "06" / "20"
        day.mkdir(parents=True)
        # Parent thread root 019ee450 has no rollout file of its own
        self.parent_id = "019ee450-0a26-75b1-8032-640e2d830432"
        # Two forked child sessions both point parent_thread_id to the parent
        self.child_early = self._write_rollout(
            day, "rollout-2026-06-20T02-17-23-019ee452-0eac-7873-af65-56ae20da405f.jsonl",
            "2026-06-20T02:17:23Z", "019ee452-0eac-7873-af65-56ae20da405f", self.parent_id)
        self.child_late = self._write_rollout(
            day, "rollout-2026-06-20T02-23-40-019ee457-cf2c-7463-8dcd-62ef8389e288.jsonl",
            "2026-06-20T02:23:40Z", "019ee457-cf2c-7463-8dcd-62ef8389e288", self.parent_id)
        # An unrelated independent session that should not match the parent id
        self._write_rollout(
            day, "rollout-2026-06-20T01-00-00-019ee400-0000-0000-0000-000000000000.jsonl",
            "2026-06-20T01:00:00Z", "019ee400-0000-0000-0000-000000000000")

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_direct_id_hit_still_works(self):
        # Existing direct hit for the child session id must keep working
        p, forked = codex.find_rollout_by_session_id(
            "019ee452-0eac-7873-af65-56ae20da405f", self.root)
        self.assertEqual(p, self.child_early)
        self.assertIsNone(forked)

    def test_parent_id_resolves_to_existing_child(self):
        # Parent id has no file; lookup should follow parent_thread_id to an existing child file
        p, forked = codex.find_rollout_by_session_id(self.parent_id, self.root)
        self.assertIsNotNone(p, "parent thread id should resolve to a fork child session, not None")
        self.assertTrue(p.exists(), "resolved path must be a real existing file")
        # Pick the newest child session as the main line: 019ee457 is later than 019ee452
        self.assertEqual(p, self.child_late)
        self.assertEqual(forked, "019ee457-cf2c-7463-8dcd-62ef8389e288")

    def test_unknown_id_returns_none(self):
        # Neither any session_meta.id nor any parent_thread_id: report no corresponding file
        p, forked = codex.find_rollout_by_session_id("ffffffff-0000-0000-0000-000000000000", self.root)
        self.assertIsNone(p)
        self.assertIsNone(forked)

    def test_never_constructs_nonexistent_path(self):
        # Key regression: parent id must not fall back to a guaranteed nonexistent constructed rollout path
        constructed = self.root / "2026" / "06" / "20" / f"rollout-2026-06-20T02-15-11-{self.parent_id}.jsonl"
        self.assertFalse(constructed.exists())  # sanity: this constructed path really does not exist
        p, _ = codex.find_rollout_by_session_id(self.parent_id, self.root)
        self.assertNotEqual(p, constructed)


class FindJsonlForkTests(unittest.TestCase):
    """server._find_jsonl end to end: parent thread id should locate the fork child file."""

    def setUp(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        self.root = Path(tempfile.mkdtemp())
        day = self.root / "2026" / "06" / "20"
        day.mkdir(parents=True)
        self.parent_id = "019ee450-0a26-75b1-8032-640e2d830432"
        meta = {"timestamp": "2026-06-20T02:23:40Z", "type": "session_meta",
                "payload": {"id": "019ee457-cf2c-7463-8dcd-62ef8389e288",
                            "parent_thread_id": self.parent_id,
                            "cwd": "/Users/alice/my-app",
                            "originator": "Codex Desktop",
                            "cli_version": "0.131.0", "source": "cli"}}
        self.child = day / "rollout-2026-06-20T02-23-40-019ee457-cf2c-7463-8dcd-62ef8389e288.jsonl"
        self.child.write_text(json.dumps(meta) + "\n")

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_find_jsonl_follows_fork(self):
        import server
        old = codex.CODEX_ROOT
        old_arch = codex.CODEX_ARCHIVED_ROOT
        codex.CODEX_ROOT = self.root
        # Archived root points to a missing child directory to isolate real ~/.codex/archived_sessions
        codex.CODEX_ARCHIVED_ROOT = self.root / "__no_archive__"
        # Ensure _cache has no stale parent-id data interfering
        old_cache = dict(server._cache)
        server._cache.clear()
        try:
            p = server._find_jsonl(self.parent_id)
            self.assertEqual(p, self.child)
        finally:
            codex.CODEX_ROOT = old
            codex.CODEX_ARCHIVED_ROOT = old_arch
            server._cache.clear()
            server._cache.update(old_cache)


if __name__ == "__main__":
    unittest.main()
