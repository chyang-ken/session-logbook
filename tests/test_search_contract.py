"""Search contract across every source, through the same path the web UI uses.

Search is the product's primary capability. This suite builds one synthetic corpus with
every supported source (Claude, Codex including a continued page, Kimi, Antigravity, Pi,
Devin), runs `scan_sessions()` + `search_sessions()` for a table of queries, and checks:

- the expected sessions, in activity order;
- the fast path (ripgrep streaming matching lines) and the whole-file path agree;
- the fast path really ran for queries it supports.

When search changes, add the new behaviour as a row here. For a change meant to keep
results identical on real data, also run `scripts/search_compare.py`.
"""
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest import mock

import server
from sources import antigravity, codex, codex_history, devin, kimi, pi

FIXTURES = Path(__file__).parent / "fixtures"

CLAUDE_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
CLAUDE_B = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
CODEX_PLAIN = "cccccccc-3333-4333-8333-cccccccccccc"
CODEX_PAGED = "dddddddd-4444-4444-8444-dddddddddddd"
KIMI_A = "session_aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
KIMI_B = "session_bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
DEVIN = "devin:devin-alpha"
AG_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
AG_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
PI = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def claude_user(text, ts):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}}


def claude_assistant(blocks, ts):
    return {"type": "assistant", "timestamp": ts, "message": {"role": "assistant", "content": blocks}}


def codex_message(role, text):
    kind = "input_text" if role == "user" else "output_text"
    return {"type": "response_item", "payload": {"type": "message", "role": role,
            "content": [{"type": kind, "text": text}]}}


class SearchContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.root = root
        stack = ExitStack()
        self.addCleanup(stack.close)
        for fixture in ("kimi", "antigravity", "pi"):
            shutil.copytree(FIXTURES / fixture, root / fixture)
        patches = [
            (server, "PROJECTS_DIR", root / "claude"),
            (server, "STATE_FILE", root / "state.json"),
            (server, "SCAN_CACHE_FILE", root / "scan-cache.json"),
            (server, "SCAN_CACHE_BACKUP_DIR", root / "scan-backups"),
            (server, "BACKUP_DIR", root / "backups"),
            (server, "_cache", {}),
            (server, "_state", {}),
            (server, "_state_loaded", True),
            (codex, "CODEX_ROOT", root / "codex"),
            (codex, "CODEX_ARCHIVED_ROOT", root / "codex-archive"),
            (codex, "SESSION_INDEX_PATH", root / "codex-index.jsonl"),
            (kimi, "KIMI_HOME", root / "kimi"),
            (kimi, "KIMI_SESSIONS_ROOT", root / "kimi" / "sessions"),
            (antigravity, "AG_ROOT", root / "antigravity"),
            (antigravity, "AG_BRAIN", root / "antigravity" / "brain"),
            (pi, "PI_SESSIONS_ROOT", root / "pi"),
            (devin, "DEVIN_ROOT", root / "devin"),
        ]
        for target, name, value in patches:
            stack.enter_context(mock.patch.object(target, name, value))
        # antigravity.scan_sessions binds its default root at import time; pin it here so
        # the suite can never read a developer's real Antigravity history.
        ag_scan = antigravity.scan_sessions
        stack.enter_context(mock.patch.object(
            antigravity, "scan_sessions", lambda root=None: ag_scan(root or antigravity.AG_BRAIN)))
        stack.enter_context(mock.patch.dict(
            "os.environ", {"SESSION_LOGBOOK_HISTORY_INDEX": str(root / "history-index.sqlite3")}))
        self.write_claude()
        self.write_codex()
        self.write_devin()
        server._state[CLAUDE_B] = {"title_override": "Billing recovery"}
        server.scan_sessions()

    # ---------- corpus ----------
    def write_claude(self):
        project = self.root / "claude" / "-Users-alice-my-app"
        project.mkdir(parents=True)
        rows_a = [
            claude_user("Please Deploy the Widget to staging", "2026-01-01T00:00:01Z"),
            claude_assistant([{"type": "tool_use", "id": "t1", "name": "Bash",
                               "input": {"command": "cat launch.json"}}], "2026-01-01T00:00:02Z"),
            {"type": "user", "timestamp": "2026-01-01T00:00:03Z", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "toolonlyneedle output"}]}},
            claude_assistant([{"type": "text", "text": "Widget deployed. The log shows {\"sessionId\": 1}."}],
                             "2026-01-01T00:00:04Z"),
            claude_user('Say "quoted" words and a C:\\path please', "2026-01-01T00:00:05Z"),
        ] + [claude_user(f"repeat marker {i}", f"2026-01-01T00:01:0{i}Z") for i in range(5)]
        (project / f"{CLAUDE_A}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows_a))
        rows_b = [
            claude_user("payment retry details for the widget", "2025-06-01T00:00:01Z"),
            claude_assistant([{"type": "text", "text": "Retry handled"}], "2025-06-01T00:00:02Z"),
        ]
        (project / f"{CLAUDE_B}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows_b))

    def write_codex(self):
        day = self.root / "codex" / "2026" / "01" / "02"
        day.mkdir(parents=True)

        def write(name, sid, rows, ts, base=None):
            meta = {"id": sid, "cwd": "/Users/alice/my-app", "timestamp": ts}
            if base:
                meta.update(history_base=base, history_mode="paginated", session_id=sid)
            path = day / name
            records = [{"type": "session_meta", "payload": meta}, *rows]
            path.write_text("".join(json.dumps(dict(r, ordinal=i + (base or {}).get("end_ordinal_exclusive", 0),
                                                    timestamp=ts)) + "\n" for i, r in enumerate(records)))
            return path

        write(f"rollout-2026-01-02T00-00-00-{CODEX_PLAIN}.jsonl", CODEX_PLAIN,
              [codex_message("user", "Codex plain request about the widget"),
               {"type": "response_item", "payload": {"type": "function_call_output",
                                                     "output": "codexToolOnly result"}}],
              "2026-01-02T00:00:00Z")
        first = write(f"rollout-2026-01-02T01-00-00-{CODEX_PAGED}.jsonl", CODEX_PAGED,
                      [codex_message("user", "Inherited goal: build the orchard index"),
                       codex_message("assistant", "Started"),
                       codex_message("user", "Replaced branch tail")],
                      "2026-01-02T01:00:00Z")
        prefix = codex_history.read_segment(first)[0][:3]
        write(f"rollout-2026-01-02T02-00-00-{CODEX_PAGED}.jsonl", CODEX_PAGED,
              [codex_message("user", "Continued page request")],
              "2026-01-02T02:00:00Z",
              base={"thread_id": CODEX_PAGED, "end_ordinal_exclusive": 3,
                    "end_byte_offset": prefix[-1]["end"]})

    def write_devin(self):
        db = devin.database_path()
        db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(db)) as conn:
            conn.executescript('''
                CREATE TABLE sessions (id TEXT PRIMARY KEY, working_directory TEXT,
                  model TEXT, title TEXT, created_at INTEGER, last_activity_at INTEGER,
                  main_chain_id INTEGER, hidden INTEGER);
                CREATE TABLE message_nodes (row_id INTEGER PRIMARY KEY, session_id TEXT,
                  node_id INTEGER, parent_node_id INTEGER, chat_message TEXT, created_at INTEGER);
                INSERT INTO sessions VALUES ('devin-alpha', '/Users/alice/my-app', 'example-model',
                  'Synthetic review', 1700000000, 1700000010, 20, 0);
            ''')
            for row, node, parent, message in [
                    (1, 10, None, {"role": "user", "content": "devin widget audit",
                                   "metadata": {"is_user_input": True}}),
                    (2, 20, 10, {"role": "assistant", "content": "Audit complete"})]:
                conn.execute("INSERT INTO message_nodes VALUES (?,?,?,?,?,?)",
                             (row, "devin-alpha", node, parent, json.dumps(message), 1700000000 + row))
            conn.commit()

    # ---------- helpers ----------
    def search(self, query, fast_expected=True):
        """Return result ids; assert fast and whole-file search agree."""
        if fast_expected:
            with mock.patch.object(server, "_warn_search_fallback", side_effect=AssertionError(query)):
                fast = server.search_sessions(query)
        else:
            fast = server.search_sessions(query)
        with mock.patch.object(server, "_rg_matching_lines", side_effect=RuntimeError("forced")):
            full = server.search_sessions(query)
        self.assertEqual(fast, full, query)
        return fast

    def ids(self, query, **kwargs):
        return [r["id"] for r in self.search(query, **kwargs)]

    # ---------- contract ----------
    def test_every_source_is_scanned(self):
        sources = {m.get("source", "claude") for m in server._cache.values()}
        self.assertTrue({"codex", "kimi", "antigravity", "pi", "devin"} <= sources, sources)
        paths = [Path(m["jsonl_path"]) for m in server._cache.values()]
        self.assertTrue(any(p.parent.parent == self.root / "claude" for p in paths))

    def test_query_table(self):
        cases = [
            # (query, expected ids in activity order, why)
            ("deploy the widget", [CLAUDE_A], "multi-term AND inside one message, case-insensitive"),
            ("WIDGET", [CODEX_PLAIN, CLAUDE_A, CLAUDE_B, DEVIN], "every source with the word, newest first"),
            ("launch.json", [], "tool input is not message text"),
            ("toolonlyneedle", [], "tool output is not message text"),
            ("codextoolonly", [], "Codex tool output is not message text"),
            ("sessionid", [CLAUDE_A], "key-like text inside a message still matches"),
            ("parentuuid", [], "JSON keys never match"),
            ("billing retry", [CLAUDE_B], "one term by personal title, one in content"),
            ("bbbbbbbb-2222", [CLAUDE_B], "session id substring"),
            ("inherited goal", [CODEX_PAGED], "Codex continuation includes the inherited page"),
            ("continued page", [CODEX_PAGED], "Codex continuation's own page"),
            ("replaced branch tail", [], "text after the inherited boundary is excluded"),
            ("small web dashboard", [KIMI_A], "Kimi"),
            ("run the tests", [KIMI_B], "Kimi second session"),
            ("hello world", [AG_B], "Antigravity"),
            ("orchard", [PI, CODEX_PAGED], "Pi and Codex both mention it"),
            ("devin widget audit", [DEVIN], "Devin"),
            ("widget payment", [CLAUDE_B], "AND across messages of one session only"),
            ("zzqxv-nothing", [], "no match"),
        ]
        for query, expected, why in cases:
            with self.subTest(query=query, why=why):
                got = self.ids(query)
                self.assertEqual([i for i in got if i in expected], expected)
                extra = [i for i in got if i not in expected]
                self.assertEqual(extra, [], f"unexpected sessions for {query!r}")

    def test_queries_with_json_escaped_characters(self):
        # Quotes and backslashes are stored escaped in JSON; they must still match, fast.
        self.assertEqual(self.ids('"quoted"'), [CLAUDE_A])
        self.assertEqual(self.ids("c:\\path"), [CLAUDE_A])
        self.assertEqual(self.ids('"quoted" widget'), [CLAUDE_A])

    def test_snippets(self):
        result = self.search("repeat marker")[0]
        self.assertEqual(len(result["snippets"]), server.SEARCH_MAX_SNIPPETS)
        self.assertTrue(all(s["role"] == "you" for s in result["snippets"]))
        title = self.search("billing retry")[0]["snippets"][0]
        self.assertEqual(title["text"], "Session title: Billing recovery")
        paged = self.search("inherited goal")[0]["snippets"][0]
        self.assertTrue(paged["source_path"].endswith(f"01-00-00-{CODEX_PAGED}.jsonl"))
        self.assertEqual(paged["line"], 2)

    def test_ripgrep_without_pcre2_gives_the_same_results(self):
        rg = server._find_ripgrep()
        with mock.patch.dict(server._RG_PCRE2, {rg: False}):
            self.assertEqual(self.ids("sessionid"), [CLAUDE_A])
            self.assertEqual(self.ids("parentuuid"), [])
            self.assertEqual(self.ids("inherited goal"), [CODEX_PAGED])

    def test_without_ripgrep_python_fallback_matches(self):
        expected = {q: self.ids(q) for q in ("widget", "inherited goal", "billing retry")}
        with mock.patch.object(server, "_find_ripgrep", return_value=None):
            for query, ids in expected.items():
                self.assertEqual([r["id"] for r in server.search_sessions(query)], ids, query)


if __name__ == "__main__":
    unittest.main()
