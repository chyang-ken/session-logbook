"""Conversation identity where it meets the API, the CLI and the state file.

All fixtures are synthetic: invented uuids, an invented project path, invented text.
"""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import server
import session_logbook_cli as cli
from sources import claude_desktop

R1 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
R2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
R3 = "cccccccc-cccc-cccc-cccc-cccccccccccc"
LONE = "dddddddd-dddd-dddd-dddd-dddddddddddd"
CONVERSATION = "local_aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CWD = "/Users/alice/my-app"
SLUG = "-Users-alice-my-app"


def user(text, sec, session_id, uuid=None):
    row = {"type": "user", "timestamp": f"2026-08-20T10:00:{sec:02d}Z", "cwd": CWD,
           "sessionId": session_id, "message": {"content": text}}
    if uuid:
        row["uuid"] = uuid
    return row


def assistant(text, sec, session_id):
    return {"type": "assistant", "timestamp": f"2026-08-20T10:00:{sec:02d}Z", "cwd": CWD,
            "sessionId": session_id,
            "message": {"content": [{"type": "text", "text": text}],
                        "stop_reason": "end_turn"}}


class ScanEvidenceTests(unittest.TestCase):
    """What extract_metadata has to notice for identity to be computable at all."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, name, rows):
        path = self.dir / name
        # Real transcripts are compact JSON; the scanners' byte needles assume it.
        path.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n"
                                for row in rows))
        return path

    def test_head_session_id_is_recorded(self):
        path = self.write(f"{R2}.jsonl", [user("hello", 0, R1), assistant("hi", 1, R2)])
        self.assertEqual(server.extract_metadata(path)["head_session_id"], R1)

    def test_a_normal_file_heads_with_its_own_id(self):
        path = self.write(f"{R1}.jsonl", [user("hello", 0, R1)])
        self.assertEqual(server.extract_metadata(path)["head_session_id"], R1)

    def test_an_in_file_compaction_parent_is_not_external(self):
        path = self.write(f"{R1}.jsonl", [
            user("hello", 0, R1, uuid="first"),
            {"type": "system", "subtype": "compact_boundary", "sessionId": R1,
             "uuid": "boundary", "logicalParentUuid": "first",
             "compactMetadata": {"trigger": "manual"}},
        ])
        self.assertEqual(server.extract_metadata(path)["compaction_parent_uuids"], [])

    def test_a_compaction_parent_from_another_file_is_external(self):
        path = self.write(f"{R2}.jsonl", [
            {"type": "system", "subtype": "compact_boundary", "sessionId": R2,
             "uuid": "boundary", "logicalParentUuid": "elsewhere",
             "compactMetadata": {"trigger": "auto"}},
            user("carry on", 1, R2, uuid="second"),
        ])
        self.assertEqual(server.extract_metadata(path)["compaction_parent_uuids"], ["elsewhere"])

    def test_a_parent_written_after_the_boundary_is_still_external(self):
        # A record whose uuid only turns up below the boundary was not in the file when the
        # compaction happened, so it is not what the boundary points at. Searching the whole
        # file would call this conversation self-contained and silently lose the link.
        path = self.write(f"{R2}.jsonl", [
            {"type": "system", "subtype": "compact_boundary", "sessionId": R2,
             "uuid": "boundary", "logicalParentUuid": "anchor"},
            user("carry on", 1, R2, uuid="anchor"),
        ])
        self.assertEqual(server.extract_metadata(path)["compaction_parent_uuids"], ["anchor"])

    def test_the_custom_title_still_comes_out_of_the_same_pass(self):
        path = self.write(f"{R1}.jsonl", [
            user("hello", 0, R1),
            {"type": "custom-title", "customTitle": "A chosen title"},
            {"type": "system", "subtype": "compact_boundary", "sessionId": R1,
             "uuid": "boundary", "logicalParentUuid": "absent"},
        ])
        meta = server.extract_metadata(path)
        self.assertEqual(meta["custom_title"], "A chosen title")
        self.assertEqual(meta["compaction_parent_uuids"], ["absent"])
        self.assertEqual(server._extract_custom_title(path), "A chosen title")

    def test_compaction_links_resolve_to_the_owning_sibling(self):
        self.write(f"{R1}.jsonl", [user("first half", 0, R1, uuid="anchor")])
        self.write(f"{R2}.jsonl", [
            {"type": "system", "subtype": "compact_boundary", "sessionId": R2,
             "uuid": "boundary", "logicalParentUuid": "anchor"}])
        metas = [server.extract_metadata(self.dir / f"{R1}.jsonl"),
                 server.extract_metadata(self.dir / f"{R2}.jsonl")]
        for meta in metas:
            meta["source"] = "claude"
        server._COMPACTION_UUID_OWNERS.clear()
        self.assertEqual(server.compaction_links(metas), [(R2, R1)])

    def test_an_unresolvable_boundary_produces_no_link(self):
        self.write(f"{R2}.jsonl", [
            {"type": "system", "subtype": "compact_boundary", "sessionId": R2,
             "uuid": "boundary", "logicalParentUuid": "nobody-has-this"}])
        meta = server.extract_metadata(self.dir / f"{R2}.jsonl")
        meta["source"] = "claude"
        server._COMPACTION_UUID_OWNERS.clear()
        self.assertEqual(server.compaction_links([meta]), [])

    def test_an_ambiguous_boundary_produces_no_link(self):
        for name in (R1, R3):
            self.write(f"{name}.jsonl", [user("copied", 0, name, uuid="anchor")])
        self.write(f"{R2}.jsonl", [
            {"type": "system", "subtype": "compact_boundary", "sessionId": R2,
             "uuid": "boundary", "logicalParentUuid": "anchor"}])
        meta = server.extract_metadata(self.dir / f"{R2}.jsonl")
        meta["source"] = "claude"
        server._COMPACTION_UUID_OWNERS.clear()
        self.assertEqual(server.compaction_links([meta]), [])


class SaveStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(server, "STATE_FILE", self.root / "state.json"),
            mock.patch.object(server, "_state", {R1: {"note": "kept"}}),
            mock.patch.object(server, "_state_loaded", True),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_rebound_state_file_still_gets_a_backup(self):
        server.save_state()
        server._state[R2] = {"note": "second"}
        server.save_state()
        backups = sorted((self.root / "backups").glob("state-*.json"))
        self.assertTrue(backups, "a rebound STATE_FILE must not silently skip its backup")
        self.assertNotEqual(server._state_backup_dir(), server.BACKUP_DIR)

    def test_the_write_is_read_back_before_it_counts_as_saved(self):
        self.assertTrue(server.save_state())
        self.assertEqual(json.loads((self.root / "state.json").read_text()), server._state)

    def test_a_corrupted_write_is_reported_not_swallowed(self):
        original = Path.read_text

        def broken(self, *args, **kwargs):
            if self.name == "state.json":
                return "{}"
            return original(self, *args, **kwargs)

        with mock.patch.object(Path, "read_text", broken):
            with mock.patch("sys.stderr") as err:
                self.assertFalse(server.save_state())
        self.assertTrue(err.write.called)


class ApiFixture(unittest.TestCase):
    """A three-record rewind chain plus one unrelated record, served over HTTP."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.projects = self.root / "projects"
        (self.projects / SLUG).mkdir(parents=True)
        for index, sid in enumerate((R1, R2, R3, LONE)):
            (self.projects / SLUG / f"{sid}.jsonl").write_text(
                json.dumps(user(f"question {index}", index, sid)) + "\n"
                + json.dumps(assistant(f"answer {index}", index, sid)) + "\n")
        self.descriptors = self.root / "descriptors"
        (self.descriptors / "account" / "organization").mkdir(parents=True)
        (self.descriptors / "account" / "organization" / "local_one.json").write_text(
            json.dumps({"sessionId": CONVERSATION, "cliSessionId": R3, "cwd": CWD,
                        "priorCliSessionIds": [R1, R2],
                        "rewindEdges": [self.edge(R1, R2), self.edge(R2, R3)]}))
        self.state = {}
        self.patches = [
            mock.patch.object(server, "PROJECTS_DIR", self.projects),
            mock.patch.object(server, "STATE_FILE", self.root / "state.json"),
            mock.patch.object(server, "SCAN_CACHE_FILE", self.root / "scan-cache.json"),
            mock.patch.object(server, "_state", self.state),
            mock.patch.object(server, "_state_loaded", True),
            mock.patch.object(claude_desktop, "DESKTOP_ROOT", self.descriptors),
            mock.patch.object(server.codex_source, "scan_sessions", return_value=[]),
            mock.patch.object(server.ag_source, "scan_sessions", return_value=[]),
            mock.patch.object(server.pi_source, "scan_sessions", return_value=[]),
            mock.patch.object(server.kimi_source, "scan_sessions", return_value=[]),
            mock.patch.object(server.devin_source, "scan_sessions", return_value=[]),
            mock.patch.dict(server._cache, {}, clear=True),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        server._conversation_memo.update(generation=None, at=0.0, index={}, by_record={})
        server._COMPACTION_UUID_OWNERS.clear()
        # The project path is written into every record, so the cwd map resolves the slug.
        server.scan_sessions(force=True)

    def edge(self, parent, child):
        return {"parent": parent, "child": child, "forkPoint": "record-uuid",
                "at": 100, "cwd": CWD}

    def items(self):
        return {item["id"]: item for item in server.enriched_sessions()}


class ApiShapeTests(ApiFixture):
    def test_every_item_carries_its_conversation(self):
        items = self.items()
        for record_id in (R1, R2, R3):
            self.assertEqual(items[record_id]["conversation_id"], CONVERSATION)
            self.assertEqual(items[record_id]["conversation_current_id"], R3)
        self.assertEqual([r["id"] for r in items[R3]["conversation_records"]], [R1, R2, R3])
        self.assertEqual(items[LONE]["conversation_id"], LONE)
        self.assertEqual(items[LONE]["conversation_current_id"], LONE)

    def test_record_ids_and_paths_are_untouched(self):
        for record_id, item in self.items().items():
            self.assertEqual(item["id"], record_id)
            self.assertEqual(Path(item["jsonl_path"]).stem, record_id)

    def test_a_star_set_before_the_rewind_follows_the_conversation(self):
        self.state[R1] = {"starred": True, "starred_at": "2026-01-01T00:00:00+00:00"}
        items = self.items()
        self.assertTrue(items[R3]["starred"])
        self.assertEqual(items[R3]["starred_at"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(items[R3]["scope"], "starred")
        # The superseded card keeps reporting what was done to that file.
        self.assertTrue(items[R1]["starred"])

    def test_an_archived_ancestor_does_not_hide_the_live_conversation(self):
        self.state[R1] = {"archived": True, "archived_at": "2026-01-01T00:00:00+00:00"}
        items = self.items()
        self.assertFalse(items[R3]["archived"])
        self.assertNotEqual(items[R3]["scope"], "archived")
        self.assertTrue(items[R1]["archived"])

    def test_an_older_note_is_surfaced_separately_not_merged_away(self):
        self.state[R1] = {"note": "written before the rewind"}
        self.state[R3] = {"note": "written after"}
        items = self.items()
        self.assertEqual(items[R3]["note"], "written after")
        self.assertEqual(items[R3]["older_notes"],
                         [{"record_id": R1, "note": "written before the rewind"}])

    def test_an_older_title_is_used_and_attributed(self):
        self.state[R2] = {"title_override": "Named before the rewind"}
        items = self.items()
        self.assertEqual(items[R3]["title_override"], "Named before the rewind")
        self.assertEqual(items[R3]["display_title"], "Named before the rewind")
        self.assertEqual(items[R3]["title_override_source_id"], R2)

    def test_a_lone_record_reads_exactly_as_it_did_per_record(self):
        self.state[LONE] = {"starred": True, "starred_at": "then", "note": "n",
                            "title_override": "t", "human_confirmed": True}
        item = self.items()[LONE]
        self.assertEqual((item["starred"], item["starred_at"]), (True, "then"))
        self.assertEqual((item["note"], item["older_notes"]), ("n", []))
        self.assertEqual(item["title_override"], "t")
        self.assertEqual(item["title_override_source_id"], LONE)
        self.assertTrue(item["human_confirmed"])

    def test_the_existing_rewind_fields_are_unchanged(self):
        items = self.items()
        self.assertEqual(items[R1]["rewind_current_session_id"], R3)
        self.assertEqual(items[R3]["desktop_session_id"], CONVERSATION)
        self.assertEqual([h["session_id"] for h in items[R3]["rewind_history"]], [R2, R1])

    def test_a_conversation_id_resolves_to_the_current_record(self):
        self.assertEqual(server._find_jsonl(CONVERSATION),
                         self.projects / SLUG / f"{R3}.jsonl")

    def test_a_record_id_still_resolves_to_exactly_that_record(self):
        for record_id in (R1, R2, R3, LONE):
            self.assertEqual(server._find_jsonl(record_id),
                             self.projects / SLUG / f"{record_id}.jsonl")

    def test_an_unknown_id_resolves_to_nothing(self):
        self.assertIsNone(server._find_jsonl("local_nothing-here"))


class HttpTests(ApiFixture):
    def setUp(self):
        super().setUp()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        patch = mock.patch.object(server, "PORT", self.port)
        patch.start()
        self.addCleanup(patch.stop)
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    def post(self, target, action, body):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/sessions/"
            f"{urllib.parse.quote(target)}/{action}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_stats_counts_conversations_not_records(self):
        stats = self.get("/api/stats")
        self.assertEqual(stats["total"], 2)  # one rewind chain plus one lone record
        self.assertEqual(stats["recent"] + stats["dusty"] + stats["starred"]
                         + stats["archived"], 2)

    def test_stats_and_the_list_agree(self):
        visible = [item for item in self.get("/api/sessions")
                   if item["conversation_current_id"] == item["id"]]
        self.assertEqual(self.get("/api/stats")["total"], len(visible))

    def test_session_choices_offers_only_current_records(self):
        offered = [choice["id"] for choice in self.get("/api/session-choices")]
        self.assertEqual(sorted(offered), sorted([R3, LONE]))

    def test_search_results_name_their_conversation(self):
        hits = {hit["id"]: hit for hit in self.get("/api/search?q=question")}
        self.assertEqual(hits[R1]["conversation_id"], CONVERSATION)
        self.assertEqual(hits[R1]["conversation_current_id"], R3)
        self.assertEqual(hits[LONE]["conversation_id"], LONE)
        # Per-record snippets stay, so the evidence is still traceable to one file.
        self.assertTrue(hits[R1]["snippets"])

    def test_a_write_to_a_superseded_record_lands_on_the_current_one(self):
        self.post(R1, "note", {"note": "typed while looking at the old card"})
        self.assertEqual(self.state[R3]["note"], "typed while looking at the old card")
        self.assertNotIn(R1, self.state)

    def test_a_write_to_the_conversation_id_lands_on_the_current_record(self):
        self.post(CONVERSATION, "star", {"starred": True})
        self.assertTrue(self.state[R3]["starred"])
        self.assertNotIn(CONVERSATION, self.state)

    def test_unstarring_clears_every_starred_member(self):
        self.state[R1] = {"starred": True, "starred_at": "then"}
        self.state[R2] = {"starred": True, "starred_at": "then"}
        self.post(R3, "star", {"starred": True})
        self.post(R3, "star", {"starred": False})
        self.assertFalse(self.items()[R3]["starred"])
        self.assertFalse(self.state[R1]["starred"])
        self.assertNotIn("starred_at", self.state[R1])

    def test_unstarring_a_lone_record_is_unchanged(self):
        self.post(LONE, "star", {"starred": True})
        self.assertTrue(self.state[LONE]["starred"])
        self.post(LONE, "star", {"starred": False})
        self.assertFalse(self.state[LONE]["starred"])

    def test_the_reader_reports_the_conversation_and_the_resolution(self):
        body = self.get(f"/api/sessions/{CONVERSATION}/conversation")
        self.assertEqual(body["id"], R3)
        self.assertEqual(body["conversation_id"], CONVERSATION)
        self.assertEqual(body["resolved_from_conversation_id"], CONVERSATION)

    def test_a_note_change_moves_the_conversation_fingerprint(self):
        first = self.get(f"/api/sessions/{R3}/conversation")["fingerprint"]
        self.post(R1, "note", {"note": "new"})
        server._conversation_memo.update(generation=None, at=0.0)
        second = self.get(f"/api/sessions/{R3}/conversation")["fingerprint"]
        self.assertNotEqual(first, second)


class CliTests(ApiFixture):
    def test_locate_by_record_id_reports_the_conversation(self):
        path = cli.resolve_target(R1)
        facts = cli.conversation_facts(cli.session_metadata(path), R1)
        self.assertEqual(facts["conversation_id"], CONVERSATION)
        self.assertEqual(facts["conversation_current_id"], R3)
        self.assertNotIn("resolved_from_conversation_id", facts)

    def test_locate_by_conversation_id_says_which_record_it_used(self):
        path = cli.resolve_target(CONVERSATION)
        self.assertEqual(path.name, f"{R3}.jsonl")
        facts = cli.conversation_facts(cli.session_metadata(path), CONVERSATION)
        self.assertEqual(facts["resolved_from_conversation_id"], CONVERSATION)
        self.assertIn(R3, facts["resolution_note"])

    def test_a_record_id_is_never_retargeted(self):
        self.assertEqual(cli.resolve_target(R1).name, f"{R1}.jsonl")
        self.assertEqual(cli.resolve_target(R2).name, f"{R2}.jsonl")

    def test_context_header_names_the_conversation_and_its_records(self):
        rendered = cli.render_context(cli.resolve_target(CONVERSATION), target=CONVERSATION)
        self.assertIn(f"# SESSION_ID: {R3}", rendered)
        self.assertIn(f"# CONVERSATION_ID: {CONVERSATION}", rendered)
        self.assertIn(f"# CONVERSATION_RECORDS: {R1} {R2}(rewind) {R3}*(rewind)", rendered)
        self.assertIn("RESOLVED_FROM_CONVERSATION", rendered)

    def test_context_by_record_id_does_not_claim_a_resolution(self):
        rendered = cli.render_context(cli.resolve_target(R1), target=R1)
        self.assertIn(f"# SESSION_ID: {R1}", rendered)
        self.assertNotIn("RESOLVED_FROM_CONVERSATION", rendered)

    def test_status_carries_the_conversation_and_per_record_observations(self):
        status = cli.status_for(cli.resolve_target(R3))
        self.assertEqual(status["id"], R3)
        self.assertEqual(status["conversation_id"], CONVERSATION)
        self.assertEqual(sorted(status["conversation_runtime_observations"]), sorted([R1, R2]))

    def test_search_results_carry_the_conversation(self):
        hits = {hit["id"]: hit for hit in cli.search_sessions("question", source="claude")}
        self.assertEqual(hits[R1]["conversation_id"], CONVERSATION)
        self.assertEqual(hits[LONE]["conversation_id"], LONE)


class CliDeltaFollowTests(ApiFixture):
    """`follow --delta` against a conversation whose current record was itself rewound."""

    def setUp(self):
        super().setUp()
        # Give the current record an in-file rewind, so the delta has something to report.
        rows = [
            self.record("r3-start", None, "question rewound"),
            self.record("r3-old", "r3-start", "abandoned answer", "assistant"),
            self.record("r3-new", "r3-start", "current answer", "assistant"),
            {"type": "last-prompt", "leafUuid": "r3-new", "sessionId": R3},
        ]
        self.current = self.projects / SLUG / f"{R3}.jsonl"
        self.current.write_text("".join(json.dumps(row) + "\n" for row in rows))
        server.scan_sessions(force=True)
        server._conversation_memo.update(generation=None, at=0.0, index={}, by_record={})

    def record(self, uuid, parent, text, role="user"):
        return {"type": role, "uuid": uuid, "parentUuid": parent, "sessionId": R3, "cwd": CWD,
                "timestamp": "2026-08-20T10:00:05Z",
                "message": {"role": role,
                            "content": text if role == "user" else [{"type": "text", "text": text}]}}

    def test_delta_by_conversation_id_reports_the_record_it_used(self):
        # The reader saved CURSOR_SOURCE_PATH with its cursor, so the cursor is attributable
        # and the delta applies while the header still names the substitution.
        text = cli.render_context(cli.resolve_target(CONVERSATION), after_line=3,
                                  target=CONVERSATION, cursor_source_path=str(self.current),
                                  delta=True)
        self.assertIn(f"# SESSION_ID: {R3}", text)
        self.assertIn(f"conversation {CONVERSATION} -> current record {R3}", text)
        self.assertIn("# REMOVED_BEFORE_CURSOR: L2", text)
        self.assertIn("current answer", text)
        self.assertNotIn("question rewound", text)
        self.assertNotIn("abandoned answer", text)

    def test_a_bare_cursor_is_not_applied_to_a_record_it_may_not_come_from(self):
        # Same conversation, but nothing says which record produced line 3. The conversation
        # has three records, so the cursor could belong to a superseded transcript.
        text = cli.render_context(cli.resolve_target(CONVERSATION), after_line=3,
                                  target=CONVERSATION, delta=True)
        self.assertIn("# DELTA_NOT_APPLIED:", text)
        self.assertIn(R3, text.split("# DELTA_NOT_APPLIED:")[1].split("\n")[0])
        self.assertIn("--cursor-source-path", text)
        self.assertNotIn("REMOVED_BEFORE_CURSOR", text)
        # The documented full-branch answer, which any reader can reconcile against.
        self.assertIn("# FOLLOW_MODE: full selected branch", text)
        self.assertIn("question rewound", text)
        self.assertIn("current answer", text)
        self.assertNotIn("abandoned answer", text)

    def test_a_record_id_target_still_gets_the_plain_delta(self):
        text = cli.render_context(cli.resolve_target(R3), after_line=3, target=R3, delta=True)
        self.assertIn("# FOLLOW_MODE: delta from cursor; selected branch verified", text)
        self.assertIn("# REMOVED_BEFORE_CURSOR: L2", text)
        self.assertNotIn("DELTA_NOT_APPLIED", text)
        self.assertNotIn("RESOLVED_FROM_CONVERSATION", text)
        self.assertNotIn("question rewound", text)


if __name__ == "__main__":
    unittest.main()
