"""Tests for the read-only Agent-facing Session Logbook command."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import server
import session_logbook_cli as cli
from sources import codex as codex_source


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


class SessionLogbookCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.claude_root = root / "claude-projects"
        self.codex_root = root / "codex-sessions"
        self.codex_archived = root / "codex-archived"

        project = self.claude_root / "-Users-alice-my-app"
        self.claude = _write_jsonl(project / "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.jsonl", [
            {"type": "user", "timestamp": "2026-08-20T10:00:00Z", "cwd": "/Users/alice/my-app",
             "slug": "calm-session", "message": {"content": "design payment retry"}},
            {"type": "assistant", "timestamp": "2026-08-20T10:00:01Z", "cwd": "/Users/alice/my-app",
             "message": {"content": [{"type": "text", "text": "implemented retry policy"}],
                         "stop_reason": "end_turn"}},
            {"type": "user", "timestamp": "2026-08-20T10:00:02Z", "cwd": "/Users/alice/my-app",
             "message": {"content": "verify the payment flow"}},
        ])
        self.subagent = _write_jsonl(
            project / "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" / "subagents" / "agent-worker.jsonl",
            [{"type": "user", "timestamp": "2026-08-20T10:00:03Z", "cwd": "/Users/alice/my-app",
              "message": {"content": "inspect hidden retry edge"}}],
        )
        self.codex = _write_jsonl(
            self.codex_root / "2026" / "08" / "20" /
            "rollout-2026-08-20T10-00-00-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb.jsonl",
            [
                {"timestamp": "2026-08-20T10:00:00Z", "type": "session_meta", "payload": {
                    "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                    "cwd": "/Users/alice/other-app", "thread_source": "user"}},
                {"timestamp": "2026-08-20T10:00:01Z", "type": "response_item", "payload": {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "diagnose cache miss"}]}},
                {"timestamp": "2026-08-20T10:00:02Z", "type": "response_item", "payload": {
                    "type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "cache miss fixed"}]}},
                {"timestamp": "2026-08-20T10:00:03Z", "type": "event_msg", "payload": {
                    "type": "task_complete"}},
            ],
        )

        self.patches = [
            mock.patch.object(server, "PROJECTS_DIR", self.claude_root),
            mock.patch.object(codex_source, "CODEX_ROOT", self.codex_root),
            mock.patch.object(codex_source, "CODEX_ARCHIVED_ROOT", self.codex_archived),
            mock.patch.object(server, "_cache", {}),
            mock.patch.object(server, "_CWD_TRUTH_MAP", {}),
            mock.patch.object(server, "_CWD_INDEX_SEEN", set()),
            mock.patch.object(server, "_CWD_SEQ", {}),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.tmp.cleanup()

    def test_known_id_resolves_without_dashboard_server(self):
        path = cli.resolve_target("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        self.assertEqual(path, self.claude.resolve())

    def test_context_is_anchored_and_self_describing(self):
        rendered = cli.render_context(self.claude)
        self.assertIn("[U1] [L1] USER", rendered)
        self.assertIn("SOURCE", rendered)
        self.assertIn("NEXT_CURSOR: L3", rendered)
        self.assertIn("EXPLICIT_TERMINAL: unknown", rendered)

    def test_follow_repeats_cursor_line_then_returns_newer_content(self):
        rendered = cli.render_context(self.claude, after_line=2)
        self.assertIn("implemented retry policy", rendered)
        self.assertIn("verify the payment flow", rendered)
        self.assertNotIn("design payment retry", rendered)
        self.assertIn("INPUT_CURSOR: L2", rendered)
        self.assertIn("REPEATED_CURSOR_LINE: L2", rendered)

    def test_quiet_follow_returns_only_the_overlap_line(self):
        rendered = cli.render_context(self.claude, after_line=3)
        self.assertIn("NEXT_CURSOR: L3", rendered)
        self.assertIn("REPEATED_CURSOR_LINE: L3", rendered)
        self.assertIn("verify the payment flow", rendered)
        self.assertNotIn("implemented retry policy", rendered)

    def test_completed_partial_cursor_line_is_returned_on_next_follow(self):
        path = Path(self.tmp.name) / "partial.jsonl"
        first = {"type": "user", "timestamp": "2026-08-20T10:00:00Z",
                 "cwd": "/Users/alice/my-app", "message": {"content": "first"}}
        second = {"type": "assistant", "timestamp": "2026-08-20T10:00:01Z",
                  "cwd": "/Users/alice/my-app",
                  "message": {"content": [{"type": "text", "text": "completed later"}]}}
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(first) + "\n")
            handle.write('{"type":"assistant"')

        with mock.patch.object(server, "PROJECTS_DIR", Path(self.tmp.name)):
            before = cli.render_context(path)
            self.assertIn("NEXT_CURSOR: L2", before)
            self.assertNotIn("completed later", before)

            with open(path, "r+", encoding="utf-8") as handle:
                handle.seek(0)
                handle.write(json.dumps(first) + "\n")
                handle.write(json.dumps(second) + "\n")
                handle.truncate()

            after = cli.render_context(path, after_line=2)
            self.assertIn("REPEATED_CURSOR_LINE: L2", after)
            self.assertIn("completed later", after)

    def test_search_role_filter_and_and_semantics(self):
        user_hits = cli.search_sessions("payment retry", role="user")
        assistant_hits = cli.search_sessions("payment retry", role="assistant")
        self.assertEqual([hit["id"] for hit in user_hits], ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"])
        self.assertEqual(assistant_hits, [])
        self.assertEqual(user_hits[0]["snippets"][0]["role"], "user")

    def test_subagents_are_opt_in_and_linked_to_parent(self):
        self.assertEqual(cli.search_sessions("hidden retry"), [])
        hits = cli.search_sessions("hidden retry", include_subagents=True)
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0]["is_subagent"])
        self.assertEqual(hits[0]["parent_session_id"], "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    def test_codex_search_and_status(self):
        hits = cli.search_sessions("cache miss", source="codex")
        self.assertEqual([hit["id"] for hit in hits], ["bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"])
        status = cli.status_for(self.codex)
        self.assertEqual(status["source"], "codex")
        self.assertEqual(status["next_cursor"], "L4")
        self.assertEqual(status["explicit_terminal"], "complete")
        self.assertEqual(status["liveness"], "unknown")

    def test_evidence_reads_only_bounded_source_lines(self):
        evidence = cli.read_evidence(self.claude, line=2, context=0)
        self.assertTrue(evidence.startswith("[L2]"))
        self.assertIn("implemented retry policy", evidence)
        self.assertNotIn("design payment retry", evidence)

    def test_ambiguous_query_does_not_silently_choose(self):
        with self.assertRaises(cli.SessionAmbiguous):
            cli.resolve_target("retry", include_subagents=True)


if __name__ == "__main__":
    unittest.main()
