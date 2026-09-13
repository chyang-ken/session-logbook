"""Hermes source tests: the read-only state.db adapter.

Fixtures are synthetic SQLite stores with the tables/columns the adapter reads —
no real session data (see AGENTS.md, "never commit real session data").
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import server
from sources import anchored_transcript
from sources import hermes

SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    title TEXT,
    cwd TEXT,
    git_repo_root TEXT,
    model TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    archived INTEGER NOT NULL DEFAULT 0,
    hidden INTEGER NOT NULL DEFAULT 0,
    last_activity_at REAL
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    finish_reason TEXT,
    reasoning TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    compacted INTEGER NOT NULL DEFAULT 0,
    _compressed_summary INTEGER NOT NULL DEFAULT 0
);
"""

SID = "20260101_101010_aaaaaa"
SID_HIDDEN = "20260101_111111_bbbbbb"
SID_ARCHIVED = "20260101_121212_cccccc"
SID_PROFILE = "20260102_090000_dddddd"

T0 = 1767225600.0  # 2026-01-01T00:00:00Z, synthetic


def build_store(db_path: Path, sessions, messages) -> Path:
    """Create one synthetic state.db with the given session/message rows."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    for s in sessions:
        conn.execute(
            "INSERT INTO sessions (id, source, title, cwd, git_repo_root, model, "
            "started_at, ended_at, archived, hidden, last_activity_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                s["id"], s.get("source", "desktop"), s.get("title"), s.get("cwd"),
                s.get("git_repo_root"), s.get("model", "test-model"),
                s.get("started_at", T0), s.get("ended_at"), s.get("archived", 0),
                s.get("hidden", 0), s.get("last_activity_at"),
            ),
        )
    for m in messages:
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, tool_call_id, "
            "tool_calls, tool_name, timestamp, finish_reason, reasoning, active, compacted) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                m.get("id"), m["session_id"], m["role"], m.get("content"),
                m.get("tool_call_id"), m.get("tool_calls"), m.get("tool_name"),
                m.get("timestamp", T0), m.get("finish_reason"), m.get("reasoning"),
                m.get("active", 1), m.get("compacted", 0),
            ),
        )
    conn.commit()
    conn.close()
    return db_path


def main_messages():
    """One small session covering user / assistant text / tool call / tool result."""
    return [
        {"id": 1, "session_id": SID, "role": "user", "content": "sketch the dashboard layout"},
        {"id": 2, "session_id": SID, "role": "assistant", "content": "layout sketched",
         "timestamp": T0 + 1, "finish_reason": "stop", "reasoning": "thought about it"},
        {"id": 3, "session_id": SID, "role": "assistant", "content": "",
         "timestamp": T0 + 2, "finish_reason": "tool_calls",
         "tool_calls": json.dumps([{
             "id": "call_1", "type": "function",
             "function": {"name": "terminal", "arguments": json.dumps({"command": "ls -la"})},
         }])},
        {"id": 4, "session_id": SID, "role": "tool", "tool_name": "terminal",
         "tool_call_id": "call_1", "timestamp": T0 + 3,
         "content": json.dumps({"output": "file1\nfile2", "exit_code": 0})},
        {"id": 5, "session_id": SID, "role": "user", "content": "verify the layout",
         "timestamp": T0 + 4},
        {"id": 6, "session_id": SID, "role": "assistant", "content": "all green",
         "timestamp": T0 + 5, "finish_reason": "stop"},
    ]


class HermesTestCase(unittest.TestCase):
    """Temp HERMES_HOME with one main store; listing cache reset around each test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "hermes-home"
        self.db = build_store(
            self.home / "state.db",
            [
                {"id": SID, "title": "Layout sketch", "cwd": "/Users/alice/my-app"},
                {"id": SID_HIDDEN, "title": "Hidden helper", "cwd": "/Users/alice/my-app",
                 "hidden": 1},
                {"id": SID_ARCHIVED, "title": "Old work", "cwd": "/Users/alice/other-app",
                 "archived": 1, "started_at": T0 - 100},
            ],
            main_messages() + [
                {"id": 20, "session_id": SID_HIDDEN, "role": "user", "content": "hidden text"},
                {"id": 21, "session_id": SID_ARCHIVED, "role": "user", "content": "old text"},
            ],
        )
        # One extra profile store, discovered via profiles/*/state.db
        build_store(
            self.home / "profiles" / "other" / "state.db",
            [{"id": SID_PROFILE, "title": "Profile work", "cwd": "/Users/alice/prof"}],
            [{"id": 30, "session_id": SID_PROFILE, "role": "user", "content": "profile text"}],
        )
        self._patchers = [
            mock.patch.object(hermes, "HERMES_HOME", self.home),
            mock.patch.object(hermes, "_LISTING_CACHE", {"stamp": None, "data": []}),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in reversed(self._patchers):
            p.stop()
        self.tmp.cleanup()

    def pseudo(self, sid=SID, db=None):
        return hermes.pseudo_path(db or self.db, sid)


class PseudoPathTests(unittest.TestCase):
    def test_round_trip(self):
        p = hermes.pseudo_path("/tmp/h/state.db", "20260101_000000_ffffff")
        self.assertTrue(hermes.is_hermes_path(p))
        db, sid = hermes.split_pseudo(p)
        self.assertEqual(str(db), "/tmp/h/state.db")
        self.assertEqual(sid, "20260101_000000_ffffff")

    def test_real_paths_are_not_hermes_paths(self):
        self.assertFalse(hermes.is_hermes_path("/tmp/x.jsonl"))
        self.assertFalse(hermes.is_hermes_path("/tmp/store.db#abc"))
        self.assertIsNone(hermes.split_pseudo("/tmp/store.db"))


class ListSessionsTests(HermesTestCase):
    def test_lists_visible_sessions_from_all_stores(self):
        listings = hermes.list_sessions()
        ids = {entry["id"] for entry in listings}
        self.assertEqual(ids, {SID, SID_ARCHIVED, SID_PROFILE})

    def test_hidden_sessions_are_excluded(self):
        listings = hermes.list_sessions()
        self.assertNotIn(SID_HIDDEN, {entry["id"] for entry in listings})

    def test_mtime_is_newest_message_and_size_sums_content(self):
        entry = next(e for e in hermes.list_sessions() if e["id"] == SID)
        self.assertEqual(entry["mtime"], T0 + 5)
        expected = sum(
            len(m.get("content") or "") + len(m.get("tool_calls") or "")
            for m in main_messages()
        )
        self.assertEqual(entry["size"], expected)
        self.assertEqual(entry["n_user"], 2)

    def test_missing_home_yields_no_sessions(self):
        with mock.patch.object(hermes, "HERMES_HOME", self.root / "missing"):
            self.assertEqual(hermes.list_sessions(), [])


class ExtractMetadataTests(HermesTestCase):
    def test_meta_aligned_with_other_sources(self):
        meta = hermes.extract_metadata(self.pseudo())
        self.assertEqual(meta["id"], SID)
        self.assertEqual(meta["source"], "hermes")
        self.assertEqual(meta["project_path"], "/Users/alice/my-app")
        self.assertEqual(meta["custom_title"], "Layout sketch")
        self.assertEqual(meta["model"], "test-model")
        self.assertEqual(meta["user_turn_count"], 2)
        self.assertEqual(meta["last_stop_reason"], "stop")
        self.assertEqual(meta["first_user_msg"], "sketch the dashboard layout")
        self.assertFalse(meta["hermes_archived"])
        self.assertAlmostEqual(meta["mtime"], T0 + 5)

    def test_recent_msgs_merge_user_and_assistant_tails(self):
        meta = hermes.extract_metadata(self.pseudo())
        pairs = [(m["role"], m["text"]) for m in meta["recent_msgs"]]
        self.assertEqual(pairs[-1], ("assistant", "all green"))
        self.assertIn(("user", "verify the layout"), pairs)
        self.assertIn(("user", "sketch the dashboard layout"), pairs)

    def test_archived_flag_is_derived(self):
        meta = hermes.extract_metadata(self.pseudo(SID_ARCHIVED))
        self.assertTrue(meta["hermes_archived"])

    def test_cwd_falls_back_to_git_repo_root(self):
        meta = hermes.extract_metadata(self.pseudo(SID_ARCHIVED))
        self.assertEqual(meta["project_path"], "/Users/alice/other-app")

    def test_unknown_session_returns_none(self):
        self.assertIsNone(hermes.extract_metadata(self.pseudo("nope")))
        self.assertIsNone(hermes.extract_metadata("/tmp/gone.db#nope"))


class ConversationTests(HermesTestCase):
    def test_turns_pair_tool_calls_with_results(self):
        conv = hermes.extract_conversation(self.pseudo())
        self.assertEqual(conv["source"], "hermes")
        self.assertEqual(conv["id"], SID)
        types = [t["type"] for t in conv["turns"]]
        self.assertEqual(types, ["user", "assistant", "tool", "user", "assistant"])
        tool = conv["turns"][2]
        self.assertEqual(tool["name"], "terminal")
        self.assertIn("ls -la", tool["summary"])
        self.assertIn("file1", tool["result"])
        self.assertFalse(tool["is_error"])
        self.assertEqual(conv["total_lines"], 6)

    def test_structured_failure_marks_tool_error(self):
        db = build_store(
            self.root / "err-home" / "state.db",
            [{"id": "err-1", "title": "Err", "cwd": "/tmp"}],
            [
                {"id": 1, "session_id": "err-1", "role": "assistant", "content": "",
                 "tool_calls": json.dumps([{
                     "id": "c1", "function": {"name": "terminal", "arguments": "{}"}}])},
                {"id": 2, "session_id": "err-1", "role": "tool", "tool_name": "terminal",
                 "tool_call_id": "c1",
                 "content": json.dumps({"success": False, "error": "boom"})},
            ],
        )
        conv = hermes.extract_conversation(hermes.pseudo_path(db, "err-1"))
        self.assertTrue(conv["turns"][-1]["is_error"])

    def test_ts_is_iso_and_empty_content_rows_are_skipped(self):
        conv = hermes.extract_conversation(self.pseudo())
        self.assertTrue(conv["turns"][0]["ts"].startswith("2026-01-01T"))
        # The tool-call-only assistant row contributes no assistant text turn
        self.assertNotIn("", [t.get("text") for t in conv["turns"] if t["type"] == "assistant"])


class TranscriptTests(HermesTestCase):
    def test_transcript_blocks_and_tool_line(self):
        text = hermes.extract_transcript(self.pseudo())
        self.assertIn(f"# Session {SID}", text)
        self.assertIn("Project: /Users/alice/my-app", text)
        self.assertIn("## USER\nsketch the dashboard layout", text)
        self.assertIn("## ASSISTANT\nlayout sketched", text)
        self.assertIn("[tool: terminal(ls -la)]", text)
        self.assertIn("file1", text)

    def test_long_tool_result_is_truncated_with_shared_suffix(self):
        db = build_store(
            self.root / "big-home" / "state.db",
            [{"id": "big-1", "title": "Big", "cwd": "/tmp"}],
            [
                {"id": 1, "session_id": "big-1", "role": "assistant", "content": "",
                 "tool_calls": json.dumps([{
                     "id": "c1", "function": {"name": "terminal", "arguments": "{}"}}])},
                {"id": 2, "session_id": "big-1", "role": "tool", "tool_name": "terminal",
                 "tool_call_id": "c1", "content": "x" * 500},
            ],
        )
        text = hermes.extract_transcript(hermes.pseudo_path(db, "big-1"))
        self.assertIn("chars, ~1 lines]", text)


class AnchoredRenderTests(HermesTestCase):
    def test_render_uses_message_id_anchors(self):
        body = anchored_transcript.render_hermes(self.pseudo())
        self.assertIn("[U1] [L1] USER", body)
        self.assertIn("sketch the dashboard layout", body)
        self.assertIn("[L2] ASSISTANT: layout sketched", body)
        self.assertIn("[L2]   💭 THINK: thought about it", body)
        self.assertIn("[L3]   🔧 terminal: ls -la", body)
        self.assertIn("[L4]   ⮑ TOOL_RESULT OK", body)
        self.assertIn("use evidence", body)
        self.assertNotIn("file1", body)

    def test_digest_header_describes_store_anchors(self):
        header = anchored_transcript.digest_header(self.pseudo(), "hermes")
        self.assertIn("Hermes Agent session", header)
        self.assertIn("state.db — SQLite store", header)
        self.assertIn("messages row id", header)

    def test_digest_header_claude_output_is_unchanged(self):
        header = anchored_transcript.digest_header("/tmp/session.jsonl", "claude")
        self.assertIn("Claude Code session", header)
        self.assertIn("line <n> in the SOURCE jsonl above", header)


class LookupTests(HermesTestCase):
    def test_pseudo_exists_and_find_by_id(self):
        self.assertTrue(hermes.pseudo_exists(self.pseudo()))
        self.assertFalse(hermes.pseudo_exists(self.pseudo("nope")))
        self.assertEqual(hermes.find_pseudo_by_id(SID), self.pseudo())
        self.assertEqual(hermes.find_pseudo_by_id(SID_PROFILE),
                         hermes.pseudo_path(self.home / "profiles" / "other" / "state.db", SID_PROFILE))
        self.assertIsNone(hermes.find_pseudo_by_id("missing"))

    def test_max_anchor_and_evidence(self):
        self.assertEqual(hermes.max_anchor(self.pseudo()), 6)
        lines = hermes.evidence_lines(self.pseudo(), 4, 0)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("[L4]"))
        self.assertIn("file1", lines[0])
        self.assertEqual(hermes.evidence_lines(self.pseudo(), 999, 0), [])


class SearchTests(HermesTestCase):
    def test_and_semantics(self):
        hit = hermes.search_session(self.pseudo(), ["layout", "sketch"])
        self.assertEqual(hit["found_terms"], ["layout", "sketch"])
        self.assertTrue(hit["snippets"])
        miss = hermes.search_session(self.pseudo(), ["layout", "zzz"])
        self.assertEqual(miss["snippets"], [])
        self.assertEqual(miss["found_terms"], ["layout"])

    def test_role_filter(self):
        hits = hermes.search_session(self.pseudo(), ["all green"], role="assistant")
        self.assertEqual([s["role"] for s in hits["snippets"]], ["assistant"])
        none = hermes.search_session(self.pseudo(), ["all green"], role="user")
        self.assertEqual(none["snippets"], [])
        self.assertEqual(none["found_terms"], [])

    def test_session_id_match_returns_id_snippet(self):
        hits = hermes.search_session(self.pseudo(), ["aaaaaa"])
        self.assertTrue(hits["id_hit"])
        self.assertEqual(hits["snippets"][0]["text"], f"Session ID: {SID}")

    def test_snippet_carries_message_id_as_line(self):
        hits = hermes.search_session(self.pseudo(), ["file1"])
        self.assertEqual(hits["snippets"], [])  # tool rows are not searched
        hits = hermes.search_session(self.pseudo(), ["verify"])
        self.assertEqual(hits["snippets"][0]["line"], 5)


class ServerIntegrationTests(HermesTestCase):
    """server.py scan / lookup / archive behavior with a Hermes store present."""

    def setUp(self):
        super().setUp()
        self._old_cache = dict(server._cache)
        self._old_dirty = server._scan_cache_dirty
        server._cache.clear()
        server._scan_cache_dirty = False

    def tearDown(self):
        server._cache.clear()
        server._cache.update(self._old_cache)
        server._scan_cache_dirty = self._old_dirty
        super().tearDown()

    def _scan(self, **extra):
        empty = self.root / "empty"
        empty.mkdir(exist_ok=True)
        patches = [
            mock.patch.object(server, "PROJECTS_DIR", empty),
            mock.patch.object(server.codex_source, "scan_sessions", return_value=[]),
            mock.patch.object(server.ag_source, "scan_sessions", return_value=[]),
            mock.patch.object(server, "SCAN_CACHE_FILE", self.root / "scan-cache.json"),
            mock.patch.object(server, "SCAN_CACHE_BACKUP_DIR", self.root / "backups"),
        ]
        for p in patches:
            p.start()
        try:
            return server.scan_sessions(**extra)
        finally:
            for p in reversed(patches):
                p.stop()

    def test_scan_includes_hermes_sessions(self):
        items = self._scan(force=True)
        by_id = {m["id"]: m for m in items}
        self.assertIn(SID, by_id)
        self.assertEqual(by_id[SID]["source"], "hermes")
        self.assertNotIn(SID_HIDDEN, by_id)
        # Archive scope comes from the Hermes archive flag via _effective_archived
        self.assertEqual(server.compute_scope(by_id[SID_ARCHIVED], {}), "archived")

    def test_find_jsonl_resolves_hermes_pseudopath(self):
        self._scan(force=True)
        path = server._find_jsonl(SID)
        self.assertTrue(hermes.is_hermes_path(path))
        self.assertEqual(str(path), self.pseudo())

    def test_find_jsonl_cold_start_uses_store_lookup(self):
        # Empty cache: the store fallback must still resolve the id
        path = server._find_jsonl(SID)
        self.assertEqual(str(path), self.pseudo())

    def test_stale_hermes_keys_are_cleaned_when_store_disappears(self):
        self._scan(force=True)
        hermes_keys = [k for k in server._cache if hermes.is_hermes_path(k)]
        self.assertTrue(hermes_keys)
        with mock.patch.object(hermes, "HERMES_HOME", self.root / "missing"):
            self._scan(force=True)
        self.assertEqual([k for k in server._cache if hermes.is_hermes_path(k)], [])

    def test_search_sessions_finds_hermes_text(self):
        self._scan(force=True)
        results = server.search_sessions("layout sketch")
        self.assertEqual([r["id"] for r in results], [SID])
        self.assertTrue(results[0]["snippets"])

    def test_effective_archived_falls_back_to_hermes_flag(self):
        self.assertTrue(server._effective_archived({"hermes_archived": True}, {}))
        self.assertFalse(server._effective_archived({"hermes_archived": True},
                                                    {"archived": False}))


if __name__ == "__main__":
    unittest.main()
