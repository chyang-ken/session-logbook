"""Search contract across every source, through the same path the web UI uses.

Search is the product's primary capability. This suite builds one synthetic corpus with
every supported source (Claude, Codex including a continued page, Kimi, Antigravity, Pi,
Devin), runs `scan_sessions()` + `search_sessions()` for a table of queries, and checks:

- the expected sessions, in activity order;
- the fast path (ripgrep streaming matching lines) and the whole-file path agree;
- the fast path really ran for queries it supports;
- the search-text sidecar prefilter ran, and gives the same results as the raw-file
  prefilter it replaces.

When search changes, add the new behaviour as a row here. For a change meant to keep
results identical on real data, also run `scripts/search_compare.py`.
"""
import json
import shutil
import sqlite3
import tempfile
import threading
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest import mock

import server
from sources import antigravity, codex, codex_history, devin, kimi, pi, search_text

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
AG_REWOUND = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
PI = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PROJECT_THREAD = "cmsg_0aaaaaaaaaaaaaaaaaaaaaaaaaaa"


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
            "os.environ", {"SESSION_LOGBOOK_HISTORY_INDEX": str(root / "history-index.sqlite3"),
                           "SESSION_LOGBOOK_SEARCH_TEXT": str(root / "search-text")}))
        stack.enter_context(mock.patch.object(search_text, "BACKGROUND", False))
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
            {"type": "queue-operation", "operation": "enqueue", "content": "unsentqueueonly"},
            {"type": "attachment", "timestamp": "2026-01-01T00:00:06Z",
             "attachment": {"type": "queued_command", "commandMode": "prompt",
                            "origin": {"kind": "human"}, "prompt": "interruptneedle clarify criteria"}},
            {"type": "attachment", "timestamp": "2026-01-01T00:00:07Z",
             "attachment": {"type": "queued_command", "commandMode": "prompt",
                            "origin": {"kind": "task-notification", "subkind": "projects-relay"},
                            "prompt": '<relay from="coordinator"><note>peerattachmentneedle '
                                      'review &amp; report &lt;gate&gt; R&amp;D</note></relay>'}},
            claude_user('<project_claude_message thread_id="cmsg_peer"><relay from="coordinator">'
                        '<note>peeruserneedle report</note></relay></project_claude_message>',
                        "2026-01-01T00:00:08Z"),
            {"type": "queue-operation", "operation": "enqueue",
             "content": '<relay from="coordinator"><note>undeliveredpeerneedle</note></relay>'},
            claude_assistant([{"type": "tool_use", "id": "projectreply", "name": "mcp__hearthbot__reply",
                               "input": {"text": "Project answer " + "detail " * 30 + "projectanswerterminus"}}],
                             "2026-01-01T00:00:09Z"),
        ] + [claude_user(f"repeat marker {i}", f"2026-01-01T00:01:0{i}Z") for i in range(5)]
        (project / f"{CLAUDE_A}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows_a))
        rows_b = [
            claude_user("payment retry details for the widget", "2025-06-01T00:00:01Z"),
            claude_assistant([{"type": "text", "text": "Retry handled"}], "2025-06-01T00:00:02Z"),
            claude_user("\u90e8\u7f72 done", "2025-06-01T00:00:03Z"),
            claude_user(f'<wake reason="mention"><thread ts="{PROJECT_THREAD}">'
                        '<message id="cmsg_1">wake body</message></thread></wake>',
                        "2025-06-01T00:00:04Z"),
            # The last record has no newline yet: the matcher still reads it.
            claude_user("unfinishedneedle here", "2025-06-01T00:00:05Z"),
        ]
        (project / f"{CLAUDE_B}.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows_b).rstrip("\n"),
            encoding="utf-8")

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
        """Return result ids; assert the fast, whole-file and raw-prefilter paths agree."""
        before = dict(search_text.stats)
        if fast_expected:
            with mock.patch.object(server, "_warn_search_fallback", side_effect=AssertionError(query)):
                fast = server.search_sessions(query)
        else:
            fast = server.search_sessions(query)
        if fast_expected:
            # The sidecars were the prefilter, for every file that has one.
            self.assertGreater(search_text.stats["indexed"], before["indexed"], query)
            self.assertEqual(search_text.stats["unindexed"], before["unindexed"], query)
        with mock.patch.object(server, "_rg_matching_lines", side_effect=RuntimeError("forced")):
            full = server.search_sessions(query)
        self.assertEqual(fast, full, query)
        with mock.patch.dict("os.environ", {"SESSION_LOGBOOK_SEARCH_TEXT": "off"}):
            raw = server.search_sessions(query)
        self.assertEqual(fast, raw, query)
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
            ("interruptneedle criteria", [CLAUDE_A], "delivered human interruption is searchable"),
            ("peerattachmentneedle report", [CLAUDE_A], "delivered mid-turn Project peer message"),
            ("<gate>", [CLAUDE_A], "Project relay HTML entities are decoded before matching"),
            ("R&D", [CLAUDE_A], "a Project peer phrase split by an HTML entity is searchable"),
            ("peeruserneedle report", [CLAUDE_A], "Project peer message filed under the user role"),
            ("undeliveredpeerneedle", [], "peer queue bookkeeping is not a delivered message"),
            ("projectanswerterminus", [CLAUDE_A], "the end of a Project reply is searchable"),
            ("unsentqueueonly", [CLAUDE_A], "a queue entry nothing confirmed delivered is still findable"),
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
            ("liveneedle", [AG_REWOUND], "Antigravity live branch after an in-file rewind"),
            ("abandonedneedle", [], "rows an Antigravity rewind abandoned are not searchable"),
            ("orchard", [PI, CODEX_PAGED], "Pi and Codex both mention it"),
            ("devin widget audit", [DEVIN], "Devin"),
            ("widget payment", [CLAUDE_B], "AND across messages of one session only"),
            ("zzqxv-nothing", [], "no match"),
            ("logbooksearchtext", [], "a sidecar's own header never matches"),
            ("\u90e8", [CLAUDE_B], "a one-character CJK query"),
            ("\u90e8\u7f72", [CLAUDE_B], "a two-character CJK query"),
            ("unfinishedneedle", [CLAUDE_B], "a final record still being written is searchable"),
            (PROJECT_THREAD, [CLAUDE_B], "a Project thread id from a wake envelope"),
        ]
        for query, expected, why in cases:
            with self.subTest(query=query, why=why):
                got = self.ids(query)
                self.assertEqual([i for i in got if i in expected], expected)
                extra = [i for i in got if i not in expected]
                self.assertEqual(extra, [], f"unexpected sessions for {query!r}")

    def test_project_peer_hits_keep_a_distinct_role_in_every_search_path(self):
        for query in ('peerattachmentneedle', 'peeruserneedle'):
            hits = self.search(query)
            self.assertEqual(hits[0]['snippets'][0]['role'], 'peer')

    PAGING_QUERIES = ["widget", "orchard", "inherited goal", "billing retry",
                      "bbbbbbbb-2222", "liveneedle", "zzqxv-nothing", "devin widget audit"]

    def test_pages_add_up_to_the_full_search(self):
        # Newest-first paging must return the same hits, in the same order, as one full
        # search: slices of one record exercise every page boundary, including a Codex
        # continuation whose inherited page lies in an older slice. The time limits are
        # lifted so a page always ends on its match count here.
        for limit in (1, 2, 50):
            for query in self.PAGING_QUERIES:
                with self.subTest(query=query, limit=limit), \
                        mock.patch.object(server, "SEARCH_PAGE_FIRST_SLICE", 1), \
                        mock.patch.object(server, "SEARCH_PAGE_SOON_S", 1e9), \
                        mock.patch.object(server, "SEARCH_PAGE_MAX_S", 1e9), \
                        mock.patch.object(server, "_warn_search_fallback",
                                          side_effect=AssertionError(query)):
                    full = server.search_sessions(query)
                    pages, cursor, seen = [], None, 0
                    while True:
                        page = server.search_sessions_page(query, limit, cursor)
                        if page["next"] is not None:
                            self.assertGreaterEqual(len(page["hits"]), limit)
                        pages.extend(page["hits"])
                        cursor = page["next"]
                        seen += 1
                        if cursor is None:
                            break
                        self.assertLess(seen, 100)
                    self.assertEqual(pages, full)

    def test_pages_that_end_on_time_still_add_up_to_the_full_search(self):
        # With no time to spare every page ends after one slice: a page with a match comes
        # back before it holds `limit`, an empty page comes back empty with a cursor, and
        # the pages still concatenate to the full search.
        for query in self.PAGING_QUERIES:
            with self.subTest(query=query), \
                    mock.patch.object(server, "SEARCH_PAGE_FIRST_SLICE", 1), \
                    mock.patch.object(server, "SEARCH_PAGE_SOON_S", 0), \
                    mock.patch.object(server, "SEARCH_PAGE_MAX_S", 0):
                full = server.search_sessions(query)
                pages, cursor, sizes = [], None, []
                while True:
                    page = server.search_sessions_page(query, 50, cursor)
                    pages.extend(page["hits"])
                    sizes.append(len(page["hits"]))
                    cursor = page["next"]
                    if cursor is None:
                        break
                    self.assertLess(len(sizes), 100)
                self.assertEqual(pages, full)
                self.assertTrue(all(n <= 1 for n in sizes), sizes)

    def test_first_page_stops_at_the_newest_matches(self):
        # A common word is found in several sessions; the first page holds only the newest.
        full = self.ids("widget")
        with mock.patch.object(server, "SEARCH_PAGE_FIRST_SLICE", 1):
            page = server.search_sessions_page("widget", 1)
        self.assertEqual([h["id"] for h in page["hits"]], full[:1])
        self.assertIsNotNone(page["next"])

    def test_slices_stop_growing_at_the_cap(self):
        # The page clock is read only between slices, so slices must stop doubling.
        sizes = []
        real = server._search_slice

        def spy(terms, chunk, context=None):
            sizes.append(len(chunk))
            return real(terms, chunk, context)

        with mock.patch.object(server, "SEARCH_PAGE_FIRST_SLICE", 1), \
                mock.patch.object(server, "SEARCH_PAGE_MAX_SLICE", 2), \
                mock.patch.object(server, "SEARCH_PAGE_SOON_S", 1e9), \
                mock.patch.object(server, "SEARCH_PAGE_MAX_S", 1e9), \
                mock.patch.object(server, "_search_slice", side_effect=spy):
            server.search_sessions_page("zzqxv-nothing", 50)
        self.assertGreater(len(sizes), 3)
        self.assertEqual(sizes[0], 1)
        self.assertTrue(all(n <= 2 for n in sizes), sizes)

    def test_search_does_not_wait_behind_a_running_scan(self):
        # Another request's scan holds the scan lock; search answers from the snapshot the
        # last scan left instead of waiting for it, and still finds every match.
        expected = self.ids("widget")
        server.scan_sessions()
        held, release = threading.Event(), threading.Event()

        def scan_in_progress():
            with server._SCAN_LOCK:
                held.set()
                release.wait(30)

        holder = threading.Thread(target=scan_in_progress)
        holder.start()
        try:
            self.assertTrue(held.wait(10))
            result = {}
            worker = threading.Thread(
                target=lambda: result.update(page=server.search_sessions_page("widget", 50)))
            worker.start()
            worker.join(20)
            self.assertFalse(worker.is_alive(), "search waited for the scan lock")
            self.assertEqual([h["id"] for h in result["page"]["hits"]], expected)
        finally:
            release.set()
            holder.join()

    def test_long_codex_lines_are_read_only_when_they_can_be_messages(self):
        # Real rollouts are compact JSON with the record type first. Long archive and event
        # lines that mention a term are ruled out from their first bytes; a long message is
        # read from disk and still found; an unfinished long last line is still skipped.
        sid = "dddddddd-4444-4444-4444-dddddddddddd"
        pad = "x" * (server.SEARCH_LONG_LINE_BYTES * 2)

        def line(i, kind, payload):
            return json.dumps({"timestamp": "2026-01-03T00:00:00Z", "ordinal": i,
                               "type": kind, "payload": payload}, separators=(",", ":"))

        def message(role, text):
            return codex_message(role, text)["payload"]

        rows = [
            line(0, "session_meta", {"id": sid, "cwd": "/Users/alice/my-app",
                                     "timestamp": "2026-01-03T00:00:00Z"}),
            line(1, "compacted", {"message": pad + " archiveneedle longneedle"}),
            line(2, "event_msg", {"type": "item_completed", "text": pad + " eventneedle"}),
            line(3, "response_item", {"type": "function_call_output",
                                      "output": pad + " outputneedle"}),
            line(4, "response_item", message("user", pad + " longneedle in a real message")),
        ]
        day = self.root / "codex" / "2026" / "01" / "03"
        day.mkdir(parents=True)
        path = day / f"rollout-2026-01-03T00-00-00-{sid}.jsonl"
        path.write_text("\n".join(rows) + "\n"
                        + line(5, "response_item", message("user", pad + " tailneedle")))
        server.scan_sessions()

        seen = []
        real = server._codex_long_line

        def spy(p, offset):
            got = real(p, offset)
            seen.append(got is not None)
            return got

        with mock.patch.object(server, "_codex_long_line", side_effect=spy):
            self.assertEqual(self.ids("longneedle"), [sid])
            for needle in ("archiveneedle", "eventneedle", "outputneedle", "tailneedle"):
                self.assertEqual(self.ids(needle), [], needle)
        self.assertIn(True, seen)    # the long message was read in full
        self.assertIn(False, seen)   # the archive, event and tool output lines were not

    def test_later_terms_only_search_files_with_the_earlier_ones(self):
        # "unsentqueueonly" is in one file, so "widget" is looked for in that file only.
        handed = []
        real = server.subprocess.run

        def spy(cmd, *args, **kwargs):
            if "-l" in cmd:
                handed.append(cmd[cmd.index("--") + 1:])
            return real(cmd, *args, **kwargs)

        paths = [Path(m["jsonl_path"]) for m in server._cache.values()]
        with mock.patch.object(server.subprocess, "run", side_effect=spy):
            found = server._rg_prefilter(["unsentqueueonly", "widget"], paths)
        self.assertEqual(len(found), 1)
        self.assertEqual(len(handed), 2)
        self.assertGreater(len(handed[0]), 1)
        self.assertEqual(handed[1], sorted(found) if len(found) > 1 else list(found))
        self.assertEqual(self.ids("unsentqueueonly widget"), [CLAUDE_A])

    # ---------- search-text sidecars ----------
    def claude_a_path(self):
        return self.root / "claude" / "-Users-alice-my-app" / f"{CLAUDE_A}.jsonl"

    def test_a_growing_session_is_appended_not_rebuilt(self):
        self.assertEqual(self.ids("widget deployed"), [CLAUDE_A])
        path = self.claude_a_path()
        sidecar = search_text.sidecar_path(self.root / "search-text", str(path))
        before = sidecar.read_bytes()
        with path.open("a") as f:
            f.write(json.dumps(claude_user("grownneedle arrives", "2026-01-01T00:02:00Z")) + "\n")
        self.assertEqual(self.ids("grownneedle"), [CLAUDE_A])
        after = sidecar.read_bytes()
        header = search_text.HEADER_BYTES
        self.assertTrue(after[header:].startswith(before[header:]), "body was rewritten, not appended")
        self.assertIn(b"grownneedle arrives", after)

    def test_a_rewritten_session_is_rebuilt(self):
        self.assertEqual(self.ids("widget deployed"), [CLAUDE_A])
        path = self.claude_a_path()
        rows = [claude_user("rewrittenneedle " + "x" * 4000, "2026-01-01T00:00:01Z"),
                claude_assistant([{"type": "text", "text": "Widget deployed."}], "2026-01-01T00:00:04Z")]
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.assertEqual(self.ids("rewrittenneedle"), [CLAUDE_A])
        self.assertNotIn(b"unsentqueueonly",
                         search_text.sidecar_path(self.root / "search-text", str(path)).read_bytes())

    def test_a_sidecar_too_large_to_refresh_falls_back_to_the_raw_file(self):
        expected = {q: self.ids(q) for q in ("widget", "inherited goal", "unsentqueueonly")}
        shutil.rmtree(self.root / "search-text")
        search_text._fresh.clear()  # as in a new process
        before = dict(search_text.stats)
        with mock.patch.object(server, "SEARCH_TEXT_INLINE_BYTES", 0):
            for query, ids in expected.items():
                self.assertEqual([r["id"] for r in server.search_sessions(query)], ids, query)
        self.assertGreater(search_text.stats["unindexed"], before["unindexed"])
        self.assertEqual(search_text.stats["indexed"], before["indexed"])

    def test_the_header_never_admits_a_file(self):
        # Every sidecar starts with a capitalised header; a lowercased term must not match it,
        # or a query naming it would send every file to the matchers.
        paths = [Path(m["jsonl_path"]) for m in server._cache.values()]
        self.assertEqual(self.ids("widget"), [CODEX_PLAIN, CLAUDE_A, CLAUDE_B, DEVIN])
        found = server._text_prefilter(["logbooksearchtext"], paths)
        self.assertEqual(found, set())

    def test_a_sidecar_removed_behind_the_memo_costs_time_not_results(self):
        expected = self.ids("widget")
        shutil.rmtree(self.root / "search-text")
        self.assertEqual([r["id"] for r in server.search_sessions("widget")], expected)
        # and the next search rebuilds what went missing
        self.assertEqual(self.ids("widget"), expected)

    def test_the_background_builder_fills_missing_sidecars(self):
        store = self.root / "search-text"
        items = server._search_text_items([Path(m["jsonl_path"]) for m in server._cache.values()])
        with mock.patch.object(search_text, "BACKGROUND", True):
            search_text.kick(items, store)
            search_text._worker.join(10)
        self.assertTrue(all(search_text.sidecar_path(store, source).exists() for source, _ in items))

    def test_sidecars_stay_out_of_the_users_state_without_the_variable(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("SESSION_LOGBOOK_SEARCH_TEXT", None)
            self.assertIsNone(server._search_text_dir())

    def test_bad_cursor_is_rejected(self):
        with self.assertRaises(ValueError):
            server.search_sessions_page("widget", 5, "not-a-cursor")

    def test_queries_with_json_escaped_characters(self):
        # Quotes and backslashes are stored escaped in JSON; they must still match, fast.
        self.assertEqual(self.ids('"quoted"'), [CLAUDE_A])
        self.assertEqual(self.ids("c:\\path"), [CLAUDE_A])
        self.assertEqual(self.ids('"quoted" widget'), [CLAUDE_A])

    def test_snippets(self):
        result = self.search("repeat marker")[0]
        self.assertEqual(len(result["snippets"]), server.SEARCH_MAX_SNIPPETS)
        self.assertTrue(all(s["role"] == "you" for s in result["snippets"]))
        # An unconfirmed queue entry is findable, but its snippet must not claim the role
        # a delivered message has: "you said this" and "you typed this" are not the same.
        queued = self.search("unsentqueueonly")[0]["snippets"]
        self.assertEqual([s["role"] for s in queued], ["queued"])
        self.assertIn("unsentqueueonly", queued[0]["text"])
        # The delivered interruption keeps the ordinary role; it really did arrive.
        self.assertEqual(self.search("interruptneedle")[0]["snippets"][0]["role"], "you")
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
