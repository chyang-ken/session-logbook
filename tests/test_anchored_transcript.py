"""sources/anchored_transcript tests: dual-source anchored transcript rendering.

The anchored transcript is the standard reduced artifact meant for agents to read
(see docs/philosophy.md "context reduction for whom"). Core contract: every line carries
a [L#] origin line-number anchor, human turns carry [U#], User and Assistant text stays whole,
and reduced tool-process detail can be expanded from the original by anchor.
"""
import json
import tempfile
import unittest
from pathlib import Path

from sources import anchored_transcript as at

FIXTURES = Path(__file__).parent / "fixtures" / "codex"
KIMI_FIXTURES = Path(__file__).parent / "fixtures" / "kimi" / "sessions"
KIMI_MAIN = (KIMI_FIXTURES / "wd_my-app_0123456789ab" / "session_aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
             / "agents" / "main" / "wire.jsonl")


def _write_jsonl(lines):
    """Write a list of dicts to a temporary jsonl file and return its path."""
    f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
    for obj in lines:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    f.close()
    return f.name


class ClaudeRenderTests(unittest.TestCase):
    def setUp(self):
        # A minimal Claude session that still covers each turn type
        self.path = _write_jsonl([
            {"type": "user", "timestamp": "2026-06-11T10:00:00Z",
             "message": {"content": "hello world"}},
            {"type": "assistant", "timestamp": "2026-06-11T10:00:01Z",
             "message": {"content": [
                 {"type": "thinking", "thinking": "let me think"},
                 {"type": "text", "text": "hi there"},
                 {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}},
             ]}},
            {"type": "user", "timestamp": "2026-06-11T10:00:02Z",
             "message": {"content": [
                 {"type": "tool_result", "content": "file1\nfile2"},
             ]}},
        ])
        self.out = at.render_claude(self.path)

    def test_user_turn_has_U_and_L_anchor(self):
        # First human turn: [U1] + [L1] (line 1 of the original)
        self.assertIn("[U1] [L1] USER", self.out)
        self.assertIn("hello world", self.out)

    def test_assistant_text_thinking_tool(self):
        self.assertIn("ASSISTANT: hi there", self.out)
        self.assertIn("💭 THINK: let me think", self.out)
        self.assertIn("🔧 Bash: ls -la", self.out)

    def test_tool_result_anchored_to_origin_line(self):
        # tool_result is on line 3 of the original -> [L3]
        self.assertIn("[L3]   ⮑ TOOL_RESULT OK", self.out)
        self.assertIn("2 lines, 11 chars hidden", self.out)
        self.assertNotIn("file1", self.out)

    def test_tool_error_keeps_leading_error_text(self):
        error = "permission denied: " + "x" * 500
        path = _write_jsonl([
            {"type": "user", "timestamp": "2026-06-11T10:00:00Z",
             "message": {"content": [
                 {"type": "tool_result", "content": error, "is_error": True},
             ]}},
        ])
        out = at.render_claude(path)
        self.assertIn("TOOL_RESULT ERROR", out)
        self.assertIn("permission denied", out)
        self.assertIn("truncated", out)

    def test_L_anchor_points_to_real_line(self):
        # [L#] must be back-referenceable: line 1 really is that user message
        with open(self.path, encoding="utf-8") as f:
            line1 = json.loads(f.readline())
        self.assertEqual(line1["message"]["content"], "hello world")


class MessagePreservationTests(unittest.TestCase):
    def test_long_claude_user_and_assistant_text_stays_complete(self):
        user_text = "u" * 9000
        assistant_text = "a" * 4000
        path = _write_jsonl([
            {"type": "user", "timestamp": "2026-06-11T10:00:00Z",
             "message": {"content": user_text}},
            {"type": "assistant", "timestamp": "2026-06-11T10:00:01Z",
             "message": {"content": [{"type": "text", "text": assistant_text}]}},
        ])
        out = at.render_claude(path)
        self.assertIn(user_text, out)
        self.assertIn(assistant_text, out)

    def test_long_codex_user_and_assistant_text_stays_complete(self):
        user_text = "u" * 9000
        assistant_text = "a" * 4000
        path = _write_jsonl([
            {"type": "session_meta", "timestamp": "2026-06-11T10:00:00Z",
             "payload": {"cwd": "/tmp", "model": "gpt-5"}},
            {"type": "response_item", "timestamp": "2026-06-11T10:00:01Z",
             "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": user_text}]}},
            {"type": "response_item", "timestamp": "2026-06-11T10:00:02Z",
             "payload": {"type": "message", "role": "assistant",
                         "content": [{"type": "output_text", "text": assistant_text}]}},
        ])
        out = at.render_codex(path)
        self.assertIn(user_text, out)
        self.assertIn(assistant_text, out)

    def test_base64_blob_stripped(self):
        blob = "QUJD" * 800  # a long string that looks like base64
        path = _write_jsonl([
            {"type": "user", "timestamp": "2026-06-11T10:00:00Z",
             "message": {"content": [{"type": "tool_result", "content": blob}]}},
        ])
        out = at.render_claude(path)
        self.assertIn("[binary/base64 stripped]", out)


class CodexRenderTests(unittest.TestCase):
    def test_fixtures_render_nonempty_with_anchors(self):
        for fx in FIXTURES.glob("*.jsonl"):
            out = at.render_codex(fx)
            with self.subTest(fixture=fx.stem):
                self.assertTrue(out.strip(), f"{fx.stem} rendered empty")
                self.assertRegex(out, r"\[L\d+\]", f"{fx.stem} missing [L#] anchor")

    def test_session_meta_anchored(self):
        out = at.render_codex(FIXTURES / "basic_main.jsonl")
        self.assertIn("[SESSION_META]", out)

    def test_successful_output_keeps_status_and_size_not_body(self):
        out = at.render_codex(FIXTURES / "basic_main.jsonl")
        self.assertIn("OUTPUT OK", out)
        self.assertIn("2 lines, 11 chars hidden", out)
        self.assertNotIn("file1", out)

    def test_structured_failed_output_keeps_leading_error(self):
        path = _write_jsonl([
            {"type": "session_meta", "timestamp": "2026-06-11T10:00:00Z",
             "payload": {"cwd": "/tmp", "model": "gpt-5"}},
            {"type": "response_item", "timestamp": "2026-06-11T10:00:01Z",
             "payload": {"type": "function_call_output", "call_id": "c1",
                         "output": json.dumps({"exit_code": 1, "output": "build failed"})}},
        ])
        out = at.render_codex(path)
        self.assertIn("OUTPUT ERROR", out)
        self.assertIn("build failed", out)

    def test_agents_md_injection_filtered(self):
        # AGENTS.md injection should be marked as [CONTEXT injected: ...], not treated as a human turn
        path = _write_jsonl([
            {"type": "session_meta", "timestamp": "2026-06-11T10:00:00Z",
             "payload": {"cwd": "/tmp", "model": "gpt-5"}},
            {"type": "response_item", "timestamp": "2026-06-11T10:00:01Z",
             "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": "<user_instructions>do X</user_instructions>"}]}},
            {"type": "response_item", "timestamp": "2026-06-11T10:00:02Z",
             "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": "the real question"}]}},
        ])
        out = at.render_codex(path)
        self.assertIn("[CONTEXT injected:", out)
        # The injection is not a human turn; the real question is [U1]
        self.assertIn("[U1]", out)
        self.assertIn("the real question", out)
        self.assertNotIn("[U2]", out)

    def test_injection_and_real_prompt_in_same_message_are_split(self):
        path = _write_jsonl([
            {"type": "session_meta", "timestamp": "2026-06-11T10:00:00Z",
             "payload": {"cwd": "/tmp", "model": "gpt-5"}},
            {"type": "response_item", "timestamp": "2026-06-11T10:00:01Z",
             "payload": {"type": "message", "role": "user", "content": [
                 {"type": "input_text", "text": "<environment_context><cwd>/tmp</cwd></environment_context>"},
                 {"type": "input_text", "text": "keep this real prompt"},
             ]}},
        ])
        out = at.render_codex(path)
        self.assertIn("[CONTEXT injected:", out)
        self.assertIn("[U1] [L2] USER", out)
        self.assertIn("keep this real prompt", out)


class KimiRenderTests(unittest.TestCase):
    def setUp(self):
        self.out = at.render_kimi(KIMI_MAIN)
        self.lines = self.out.splitlines()

    def test_user_turns_anchor_to_turn_prompt_lines(self):
        self.assertIn("━━━━━━━━━━ [U1] [L4] USER", self.out)
        self.assertIn("List the files in the project and tell me what it does", self.out)
        self.assertIn("[U2] [L30] USER", self.out)
        self.assertNotIn("[U3]", self.out)

    def test_injection_and_task_messages_are_context_not_user_turns(self):
        self.assertIn("[L5]   [CONTEXT injected (injection): <env>injected environment notes</env>]", self.out)
        self.assertIn("[L29]   [CONTEXT injected (task): Background task finished: index rebuilt]", self.out)
        self.assertEqual(sum(1 for l in self.lines if "Background task finished" in l), 1)

    def test_think_tool_and_result_lines(self):
        self.assertIn("[L9]   💭 THINK: I should list the files first.", self.out)
        self.assertIn("[L10]   🔧 Bash: ls -la", self.out)
        self.assertIn("[L11]   ⮑ RESULT OK: [2 lines, 19 chars hidden; use evidence]", self.out)
        self.assertNotIn("README.md", self.out)
        self.assertIn("[L23]   🔧 Grep: pattern='def main' path=/Users/alice/my-app", self.out)
        self.assertIn("[L24]   ⮑ RESULT ERROR: grep: permission denied", self.out)

    def test_assistant_text_per_part_and_qa(self):
        self.assertIn("[L14] ASSISTANT: It is a small web dashboard.", self.out)
        self.assertIn("[L15]   🔧 AskUserQuestion: Which area should I explain first?", self.out)
        self.assertIn("[L17]   ⮑ USER ANSWERED: Which area should I explain first? → Backend", self.out)
        self.assertEqual(sum(1 for l in self.lines if "Which area should I explain first?" in l), 2)

    def test_session_meta_and_turn_end(self):
        self.assertIn("[L1] [SESSION_META] protocol=1.5", self.out)
        self.assertIn("[L2] [SESSION_META] cwd=/Users/alice/my-app model=kimi-k2-turbo", self.out)
        self.assertIn("[L35] [TURN ENDED: completed]", self.out)

    def test_every_rendered_line_carries_an_anchor(self):
        for line in self.lines:
            if not line or line.startswith("━━━"):
                continue
            if line.startswith("[L"):
                continue
            # user body text follows its own [U#] [L#] banner
            self.assertIn(line, ("List the files in the project and tell me what it does",
                                 "Thanks, summarize in one line"))

    def test_render_core_stays_clean(self):
        self.assertNotIn("COMPACT SESSION DIGEST", self.out)


class DigestHeaderTests(unittest.TestCase):
    """Self-describing header: lets a cold recipient read the anchors and go back to the original from the .txt alone."""

    def test_header_carries_source_path(self):
        # The header must carry the original file path, otherwise "go back and expand" is broken for a cold agent
        h = at.digest_header("/abs/path/to/sess.jsonl", "claude")
        self.assertIn("/abs/path/to/sess.jsonl", h)
        self.assertIn("SOURCE", h)

    def test_header_explains_anchors(self):
        h = at.digest_header("/x.jsonl", "claude")
        self.assertIn("[L<n>]", h)     # line-number anchor legend
        self.assertIn("[U<n>]", h)     # human turn legend
        self.assertIn("truncated", h)  # reduced tool-process detail still carries a marker

    def test_header_promises_complete_conversation_messages(self):
        h = at.digest_header("/x.jsonl", "codex")
        self.assertIn("User and Assistant messages", h)
        self.assertIn("are complete", h)

    def test_header_source_label(self):
        self.assertIn("Codex", at.digest_header("/x.jsonl", "codex"))
        self.assertIn("Claude Code", at.digest_header("/x.jsonl", "claude"))
        self.assertIn("Kimi Code", at.digest_header("/x.jsonl", "kimi"))

    def test_render_core_stays_clean(self):
        # Key: the render body must never contain the header -- the offline pipeline's byte-level consistency contract depends on this
        path = _write_jsonl([
            {"type": "user", "timestamp": "2026-06-11T10:00:00Z",
             "message": {"content": "hi"}},
        ])
        out = at.render_claude(path)
        self.assertNotIn("COMPACT SESSION DIGEST", out)
        self.assertNotIn("SOURCE", out)


if __name__ == "__main__":
    unittest.main()
