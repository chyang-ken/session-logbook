"""Antigravity source tests."""
import unittest
from pathlib import Path
from sources import antigravity as ag

FIXTURES = Path(__file__).parent / "fixtures" / "antigravity"
BRAIN = FIXTURES / "brain"
CONV_A = BRAIN / "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" / ".system_generated" / "logs" / "transcript.jsonl"
CONV_B = BRAIN / "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb" / ".system_generated" / "logs" / "transcript.jsonl"
CONV_D = BRAIN / "dddddddd-dddd-dddd-dddd-dddddddddddd" / ".system_generated" / "logs" / "transcript.jsonl"


def _encode_summaries(mapping):
    """Build a minimal agyhub_summaries_proto.pb: top-level repeated field1=entry,
    entry = field1(uuid string) + field2(title string). Used by the _load_titles tests."""
    def varint(n):
        out = bytearray()
        while True:
            b = n & 0x7f
            n >>= 7
            if n:
                out.append(b | 0x80)
            else:
                out.append(b)
                break
        return bytes(out)

    def ld(field_no, data):  # length-delimited field (wire type 2)
        return bytes([field_no << 3 | 2]) + varint(len(data)) + data

    blob = bytearray()
    for uid, title in mapping.items():
        entry = ld(1, uid.encode("utf-8")) + ld(2, title.encode("utf-8"))
        blob += ld(1, entry)
    return bytes(blob)


class ModuleTests(unittest.TestCase):
    def test_module_importable(self):
        self.assertTrue(hasattr(ag, "AG_ROOT"))
        self.assertTrue(hasattr(ag, "AG_BRAIN"))

    def test_fixtures_present(self):
        self.assertTrue(CONV_A.exists())
        self.assertTrue(CONV_B.exists())


class ConvIdTests(unittest.TestCase):
    def test_conv_id_from_path(self):
        self.assertEqual(ag._conv_id(CONV_A), "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        self.assertEqual(ag._conv_id(CONV_B), "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


class ScanTests(unittest.TestCase):
    def test_scan_finds_transcripts(self):
        ids = {ag._conv_id(p) for p in ag.scan_sessions(BRAIN)}
        self.assertIn("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", ids)
        self.assertIn("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", ids)

    def test_scan_skips_dir_without_transcript(self):
        ids = {ag._conv_id(p) for p in ag.scan_sessions(BRAIN)}
        self.assertNotIn("cccccccc-cccc-cccc-cccc-cccccccccccc", ids)

    def test_scan_handles_nonexistent_root(self):
        self.assertEqual(list(ag.scan_sessions(BRAIN / "nope")), [])


class CleaningHelperTests(unittest.TestCase):
    def test_clean_user_input_strips_request_wrapper(self):
        c = "<USER_REQUEST>\nDo something\n</USER_REQUEST>\n<ADDITIONAL_METADATA>x</ADDITIONAL_METADATA>"
        self.assertEqual(ag._clean_user_input(c), "Do something")

    def test_clean_user_input_strips_meta_when_no_wrapper(self):
        c = "Plain text\n<USER_SETTINGS_CHANGE>changed</USER_SETTINGS_CHANGE>"
        self.assertEqual(ag._clean_user_input(c), "Plain text")

    def test_strip_result_header(self):
        c = "Created At: 2026-06-04T03:00:03Z\nCompleted At: 2026-06-04T03:00:03Z\nactual content"
        self.assertEqual(ag._strip_result_header(c), "actual content")

    def test_tool_summary_prefers_summary(self):
        s = ag._tool_summary("list_dir", {"toolSummary": '"List dir"', "DirectoryPath": '"/x"'})
        self.assertEqual(s, "List dir")

    def test_tool_summary_falls_back_to_path(self):
        s = ag._tool_summary("view_file", {"AbsolutePath": '"/Users/alice/a.md"'})
        self.assertEqual(s, "/Users/alice/a.md")

    def test_search_text_from_line(self):
        self.assertEqual(
            ag.search_text_from_line({"type": "USER_INPUT", "content": "<USER_REQUEST>hi</USER_REQUEST>"}),
            "hi")
        self.assertEqual(
            ag.search_text_from_line({"type": "PLANNER_RESPONSE", "content": "answer"}),
            "answer")
        self.assertEqual(ag.search_text_from_line({"type": "CHECKPOINT", "content": "x"}), "")


class ProjectPathTests(unittest.TestCase):
    def test_workspace_uris_preferred(self):
        lines = [{"content": 'stuff "workspaceUris": ["file:///Users/alice/proj"] more'}]
        self.assertEqual(ag._project_path(lines), "/Users/alice/proj")

    def test_workspace_uris_skips_internal_brain_path(self):
        # logAbsoluteUri comes first (internal brain); workspaceUris is the real project
        lines = [{"content": (
            '"logAbsoluteUri":"file:///Users/alice/.gemini/antigravity/brain/x/log.jsonl" '
            '"workspaceUris":["file:///Users/alice/realproj"]')}]
        self.assertEqual(ag._project_path(lines), "/Users/alice/realproj")

    def test_common_prefix_fallback(self):
        lines = [
            {"content": "/Users/alice/proj/a.md"},
            {"content": "/Users/alice/proj/sub/b.md"},
        ]
        self.assertEqual(ag._project_path(lines), "/Users/alice/proj")

    def test_excludes_internal_paths_from_prefix(self):
        lines = [
            {"content": "/Users/alice/proj/a.md"},
            {"content": "/Users/alice/.gemini/antigravity/brain/x/y.md"},
        ]
        self.assertEqual(ag._project_path(lines), "/Users/alice/proj")

    def test_empty_when_no_paths(self):
        self.assertEqual(ag._project_path([{"content": "no paths here"}]), "")


class ExtractMetadataTests(unittest.TestCase):
    def test_basic_fields(self):
        m = ag.extract_metadata(CONV_A)
        self.assertIsNotNone(m)
        self.assertEqual(m["id"], "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        self.assertEqual(m["source"], "antigravity")
        self.assertEqual(m["project_path"], "/Users/alice/proj")
        self.assertEqual(m["jsonl_path"], str(CONV_A))
        self.assertIsNone(m["cli_version"])
        self.assertIsNone(m["last_stop_reason"])
        self.assertIn("mtime", m)
        self.assertIn("size", m)

    def test_user_turn_count(self):
        m = ag.extract_metadata(CONV_A)
        self.assertEqual(m["user_turn_count"], 2)

    def test_model_extracted_from_settings_change(self):
        m = ag.extract_metadata(CONV_A)
        self.assertEqual(m["model"], "Gemini 3.1 Pro (High)")

    def test_first_user_msg(self):
        m = ag.extract_metadata(CONV_A)
        self.assertEqual(m["first_user_msg"], "Please analyze the file /Users/alice/proj/a.md")

    def test_recent_msgs_shape(self):
        m = ag.extract_metadata(CONV_A)
        self.assertTrue(all(set(x.keys()) == {"role", "text"} for x in m["recent_msgs"]))
        roles = {x["role"] for x in m["recent_msgs"]}
        self.assertTrue(roles <= {"user", "assistant"})

    def test_missing_file_returns_none(self):
        self.assertIsNone(ag.extract_metadata(BRAIN / "zzz" / "transcript.jsonl"))


class ExtractConversationTests(unittest.TestCase):
    def setUp(self):
        self.conv = ag.extract_conversation(CONV_A)

    def test_basic_shape(self):
        self.assertEqual(self.conv["id"], "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        self.assertEqual(self.conv["source"], "antigravity")
        self.assertEqual(self.conv["project_path"], "/Users/alice/proj")

    def test_user_turns(self):
        users = [t for t in self.conv["turns"] if t["type"] == "user"]
        self.assertEqual(len(users), 2)
        self.assertEqual(users[0]["text"], "Please analyze the file /Users/alice/proj/a.md")
        self.assertEqual(users[1]["text"], "Continue")

    def test_assistant_turns(self):
        # Only PLANNER_RESPONSE with content counts as an assistant turn (step 5 / step 10);
        # PLANNER_RESPONSE that only has tool_calls (step 3 / step 6) does not
        asts = [t for t in self.conv["turns"] if t["type"] == "assistant"]
        self.assertEqual(len(asts), 2)
        self.assertIn("I finished reading a.md", asts[0]["text"])

    def test_tool_pairing(self):
        tools = [t for t in self.conv["turns"] if t["type"] == "tool"]
        # list_dir + write_to_file + error
        names = [t["name"] for t in tools]
        self.assertIn("list_dir", names)
        self.assertIn("write_to_file", names)
        list_dir = next(t for t in tools if t["name"] == "list_dir")
        self.assertEqual(list_dir["summary"], "List proj directory")
        self.assertIn("a.md", list_dir["result"])  # paired to the LIST_DIRECTORY result
        self.assertFalse(list_dir.get("is_error"))

    def test_error_message_is_error(self):
        errs = [t for t in self.conv["turns"] if t.get("is_error")]
        self.assertEqual(len(errs), 1)
        self.assertIn("no such file", errs[0]["result"])

    def test_system_noise_skipped(self):
        # CONVERSATION_HISTORY / EPHEMERAL_MESSAGE should not appear in turns
        texts = " ".join(t.get("text", "") + t.get("result", "") for t in self.conv["turns"])
        self.assertNotIn("should be skipped", texts)
        self.assertNotIn("ephemeral", texts)

    def test_missing_file_returns_none(self):
        self.assertIsNone(ag.extract_conversation(BRAIN / "zzz" / "transcript.jsonl"))


class ResultTypePairingTests(unittest.TestCase):
    """Regression: the _RESULT_TYPES allowlist used to miss SEARCH_WEB/MCP_TOOL/GENERIC,
    which dropped results and drifted the pending FIFO (mispairing later results to an
    earlier tool_call)."""

    def setUp(self):
        self.conv = ag.extract_conversation(CONV_D)
        self.tools = [t for t in self.conv["turns"] if t["type"] == "tool"]

    def _tool(self, name):
        return next((t for t in self.tools if t["name"] == name), None)

    def test_no_tools_dumped_at_end(self):
        # Before the fix, unpaired tool_calls were all piled at the end via
        # turns.extend(pending) (with empty results). After the fix each tool follows
        # right after the assistant that issued it, and the last turn should not be an
        # empty-result tool.
        self.assertEqual(len(self.tools), 4)
        self.assertTrue(all(t.get("result") for t in self.tools),
                        "every tool should be paired to a non-empty result")

    def test_search_web_paired_with_own_result(self):
        sw = self._tool("search_web")
        self.assertIsNotNone(sw)
        self.assertIn("SEARCH_RESULT_FOO", sw["result"])  # not displaced by another tool's result
        self.assertFalse(sw.get("is_error"))

    def test_view_file_error_pairs_with_pending(self):
        # ERROR_MESSAGE as view_file's failure result must pair back to view_file,
        # not spawn a standalone error turn
        vf = self._tool("view_file")
        self.assertIsNotNone(vf)
        self.assertTrue(vf.get("is_error"))
        self.assertIn("VIEW_FILE_NOT_FOUND", vf["result"])
        self.assertIsNone(self._tool("error"))  # there should be no stray error turn

    def test_generic_is_schedule_result_not_skipped(self):
        sch = self._tool("schedule")
        self.assertIsNotNone(sch)
        self.assertIn("RUNNING_SCHED", sch["result"])

    def test_mcp_error_status_marks_is_error(self):
        mcp = self._tool("call_mcp_tool")
        self.assertIsNotNone(mcp)
        self.assertTrue(mcp.get("is_error"))  # inferred from status==ERROR
        self.assertIn("MCP_ERROR_BOOM", mcp["result"])

    def test_inline_order_preserved(self):
        # Tools should be inlined in the conversation flow: user -> [search_web] -> assistant -> [view_file] -> [schedule] -> [mcp]
        seq = [(t["type"], t.get("name")) for t in self.conv["turns"]]
        self.assertEqual(seq, [
            ("user", None),
            ("tool", "search_web"),
            ("assistant", None),
            ("tool", "view_file"),
            ("tool", "schedule"),
            ("tool", "call_mcp_tool"),
        ])

    def test_transcript_includes_all_results(self):
        t = ag.extract_transcript(CONV_D)
        for marker in ("SEARCH_RESULT_FOO", "VIEW_FILE_NOT_FOUND",
                       "RUNNING_SCHED", "MCP_ERROR_BOOM"):
            self.assertIn(marker, t)


class ExtractTranscriptTests(unittest.TestCase):
    def test_transcript_structure(self):
        t = ag.extract_transcript(CONV_A)
        self.assertIn("# Session aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", t)
        self.assertIn("Project: /Users/alice/proj", t)
        self.assertIn("## USER", t)
        self.assertIn("## ASSISTANT", t)
        self.assertIn("[tool: list_dir(", t)

    def test_transcript_skips_system_noise(self):
        t = ag.extract_transcript(CONV_A)
        self.assertNotIn("should be skipped", t)


class LoadTitlesTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.pb = self.tmp / "summaries.pb"
        self.pb.write_bytes(_encode_summaries({
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa": "Analysis Task Title",
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb": "Hello Title",
        }))
        self._orig = ag.AG_SUMMARIES
        ag.AG_SUMMARIES = self.pb
        ag._TITLE_CACHE["mtime"] = 0.0
        ag._TITLE_CACHE["data"] = {}

    def tearDown(self):
        import shutil
        ag.AG_SUMMARIES = self._orig
        ag._TITLE_CACHE["mtime"] = 0.0
        ag._TITLE_CACHE["data"] = {}
        shutil.rmtree(self.tmp)

    def test_titles_decoded(self):
        titles = ag._load_titles()
        self.assertEqual(titles["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"], "Analysis Task Title")
        self.assertEqual(titles["bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"], "Hello Title")

    def test_metadata_uses_title_index(self):
        m = ag.extract_metadata(CONV_A)
        self.assertEqual(m["custom_title"], "Analysis Task Title")

    def test_missing_summaries_returns_empty(self):
        ag.AG_SUMMARIES = self.tmp / "does_not_exist.pb"
        ag._TITLE_CACHE["mtime"] = 0.0
        ag._TITLE_CACHE["data"] = {}
        self.assertEqual(ag._load_titles(), {})


if __name__ == "__main__":
    unittest.main()
