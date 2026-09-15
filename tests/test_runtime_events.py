"""Synthetic runtime journal and incomplete-record regression tests."""

import json
import tempfile
import unittest
import subprocess
import sys
from pathlib import Path

from sources import runtime_events as events
from scripts.setup_runtime_hooks import configured, EVENTS


class RuntimeEventsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "events.sqlite3"

    def test_journal_filters_sessions_and_pages_without_skipping(self):
        for session in ("a", "b", "a", "a"):
            events.record("claude", {"session_id": session, "hook_event_name": "Stop",
                                    "prompt": "must not store", "stop_hook_active": True}, self.path)
        page = events.read("claude", "a", limit=2, path=self.path)
        self.assertTrue(page["has_more"])
        self.assertEqual([e["cursor"] for e in page["events"]], [1, 3])
        self.assertNotIn("prompt", page["events"][0]["facts"])
        self.assertTrue(page["events"][0]["facts"]["stop_hook_active"])
        rest = events.read("claude", "a", after=page["next_event_cursor"], path=self.path)
        self.assertEqual([e["cursor"] for e in rest["events"]], [4])
        self.assertEqual(rest["liveness"], "unknown")

    def test_no_collector_does_not_create_database(self):
        self.assertEqual(events.read("codex", "a", path=self.path)["collection"], "no_events_observed")
        self.assertFalse(self.path.exists())

    def test_broken_journal_is_unavailable_not_empty_or_completed(self):
        self.path.write_bytes(b"not a database")
        result = events.read("claude", "a", after=10, path=self.path)
        self.assertEqual(result["collection"], "unavailable")
        self.assertEqual(result["liveness"], "unknown")
        self.assertEqual(result["next_event_cursor"], 10)

    def test_native_preserves_old_and_new_turn_and_retries_partial_line(self):
        path = Path(self.temp.name) / "source.jsonl"
        rows = [{"type": "event_msg", "payload": {"type": name, "turn_id": turn}}
                for name, turn in [("task_started", "a"), ("task_complete", "a"), ("task_started", "b")]]
        path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
        page = events.native_events(path, "codex")
        self.assertEqual(page["next_line_cursor"], 2)
        with path.open("a") as stream:
            stream.write("\n")
        rest = events.native_events(path, "codex", after=2)
        self.assertEqual(rest["events"][0]["facts"]["turn_id"], "b")

    def test_kimi_blocked_is_not_completed(self):
        path = Path(self.temp.name) / "wire.jsonl"
        path.write_text(json.dumps({"type": "turn.ended", "turnId": 3, "reason": "blocked"}) + "\n")
        self.assertEqual(events.native_events(path, "kimi")["events"][0]["facts"]["reason"], "blocked")

    def test_invalid_input_is_rejected(self):
        with self.assertRaises(ValueError):
            events.record("devin", {}, self.path)
        with self.assertRaises(ValueError):
            events.record("claude", {"session_id": "a"}, self.path)
        with self.assertRaises(ValueError):
            events.read("claude", "a", after=-1, path=self.path)

    def test_installer_preserves_other_hooks_and_is_idempotent(self):
        original = {"permissions": {"allow": []}, "hooks": {"Stop": [
            {"matcher": "", "hooks": [{"type": "command", "command": "other-observer"}]}]}}
        for source in EVENTS:
            installed = configured(original, source)
            self.assertEqual(configured(installed, source), installed)
            self.assertEqual(configured(installed, source, False), original)
            self.assertEqual(original["hooks"]["Stop"][0]["hooks"][0]["command"], "other-observer")

    def test_native_ignores_non_object_and_wrong_envelope(self):
        path = Path(self.temp.name) / "source.jsonl"
        path.write_text('[]\nnull\n{"type":"task_complete"}\n'
                        '{"type":"event_msg","payload":null}\n')
        page = events.native_events(path, "codex")
        self.assertEqual(page["events"], [])
        self.assertEqual(page["next_line_cursor"], 4)

    def test_collector_failure_never_emits_permission_output(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "record_runtime_event.py"
        result = subprocess.run([sys.executable, str(script), "--source", "claude"],
                                input="not-json", text=True, capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("event not recorded", result.stderr)
