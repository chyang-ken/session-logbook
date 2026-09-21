"""Synthetic regressions for reminder-prefixed desktop handoff prompts."""
import json
import tempfile
import unittest
from pathlib import Path

import server
import session_logbook_cli as cli
from sources import anchored_transcript
from sources.claude_text import anchored_user_text, strip_leading_reminders


REMINDER = "<system-reminder>Desktop environment\nsettings</system-reminder>"
BODY = "Please review the orchard plan."


def human_text(content, **flags):
    """What the shared rule says a person said in a user record with this content."""
    return anchored_user_text(dict({"type": "user", "message": {"content": content}}, **flags))


class ReminderTests(unittest.TestCase):
    def test_cleanup_is_leading_and_complete_only(self):
        self.assertEqual(strip_leading_reminders(" \n" + REMINDER + "\n" + REMINDER + BODY), BODY)
        self.assertEqual(strip_leading_reminders(REMINDER), "")
        for text in ("ordinary text", "quote " + REMINDER, "<system-reminder>unfinished"):
            self.assertEqual(strip_leading_reminders(text), text)

    def test_existing_system_and_tool_filters_remain(self):
        for text in (REMINDER, REMINDER + "<command-name>test</command-name>",
                     REMINDER + "<task-notification>done</task-notification>"):
            self.assertEqual(human_text(text), "")
        self.assertEqual(human_text([{"type": "tool_result", "content": REMINDER + BODY}]), "")
        # A skill body is told apart by the flag the client sets, not by its shape: every
        # real one is isMeta, and the same blocks without the flag are a person's message.
        skill_body = [{"type": "text", "text": "ordinary skill body"}]
        self.assertEqual(human_text(skill_body, isMeta=True), "")
        self.assertEqual(human_text(skill_body), "ordinary skill body")

    def test_all_reading_paths_preserve_human_body(self):
        variants = [
            REMINDER + "\n\n" + BODY,
            [{"type": "text", "text": REMINDER + "\n" + BODY}],
            [{"type": "text", "text": REMINDER}, {"type": "text", "text": BODY}],
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.jsonl"
            for content in variants:
                with self.subTest(content=content):
                    row = {"type": "user", "cwd": "/Users/alice/my-app",
                           "timestamp": "2026-09-14T10:00:00Z", "message": {"content": content}}
                    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
                    self.assertEqual(human_text(content), BODY)
                    self.assertIn(BODY, [m["text"] for m in server.extract_metadata(path)["recent_msgs"]])
                    turns = server.extract_conversation(path)["turns"]
                    self.assertEqual([t["text"] for t in turns if t["type"] == "user"], [BODY])
                    self.assertIn("## USER\n" + BODY, server.extract_transcript(path))
                    self.assertEqual(list(cli.iter_messages(path, "claude")), [(1, "user", BODY)])
                    self.assertTrue(server._search_session(path, ["orchard"]))
                    self.assertFalse(server._search_session(path, ["settings"]))
                    anchored = anchored_transcript.render_claude(path)
                    self.assertIn("[L1] USER", anchored)
                    self.assertIn(BODY, anchored)
                    self.assertNotIn("Desktop environment", anchored)

    def test_pure_reminder_does_not_create_a_user_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.jsonl"
            path.write_text(json.dumps({"type": "user", "message": {"content": REMINDER}}) + "\n")
            self.assertFalse(server.extract_conversation(path)["turns"])
            self.assertNotIn("## USER", server.extract_transcript(path))
            self.assertNotIn("[U1]", anchored_transcript.render_claude(path))
