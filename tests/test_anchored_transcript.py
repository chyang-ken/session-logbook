"""sources/anchored_transcript tests: dual-source anchored transcript rendering.

The anchored transcript is the standard reduced artifact meant for agents to read
(see docs/philosophy.md "context reduction for whom"). Core contract: every line carries
a [L#] origin line-number anchor, human turns carry [U#], and truncation leaves a marker --
the agent uses the anchors to go back to the original and expand.
"""
import json
import tempfile
import unittest
from pathlib import Path

from sources import anchored_transcript as at

FIXTURES = Path(__file__).parent / "fixtures" / "codex"


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
        self.assertIn("[L3]   ⮑ TOOL_RESULT", self.out)
        self.assertIn("file1", self.out)

    def test_L_anchor_points_to_real_line(self):
        # [L#] must be back-referenceable: line 1 really is that user message
        with open(self.path, encoding="utf-8") as f:
            line1 = json.loads(f.readline())
        self.assertEqual(line1["message"]["content"], "hello world")


class ClaudeTruncationTests(unittest.TestCase):
    def test_long_text_leaves_truncation_marker(self):
        long_text = "x" * 9000  # exceeds the user 6000 truncation threshold
        path = _write_jsonl([
            {"type": "user", "timestamp": "2026-06-11T10:00:00Z",
             "message": {"content": long_text}},
        ])
        out = at.render_claude(path)
        self.assertIn("chars truncated]", out)  # leaves a back-reference marker
        self.assertLess(len(out), len(long_text))  # actually shortened

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
        self.assertIn("truncated", h)  # truncation marker legend

    def test_header_source_label(self):
        self.assertIn("Codex", at.digest_header("/x.jsonl", "codex"))
        self.assertIn("Claude Code", at.digest_header("/x.jsonl", "claude"))

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
