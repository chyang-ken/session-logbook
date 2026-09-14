"""Pi adapter and real HTTP/CLI integration, using synthetic records only."""
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import server
import session_logbook_cli as cli
from sources import pi, anchored_transcript, session_identity

FIXTURES = Path(__file__).parent / "fixtures" / "pi"
SID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


class PiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sessions = self.root / "sessions"
        shutil.copytree(FIXTURES, self.sessions)
        self.path = next(self.sessions.glob("*/*.jsonl"))
        self.patches = [
            mock.patch.object(pi, "PI_SESSIONS_ROOT", self.sessions),
            mock.patch.object(server, "PROJECTS_DIR", self.root / "no-claude"),
            mock.patch.object(server, "STATE_FILE", self.root / "state.json"),
            mock.patch.object(server, "SCAN_CACHE_FILE", self.root / "cache.json"),
            mock.patch.object(server, "SCAN_CACHE_BACKUP_DIR", self.root / "backups"),
            mock.patch.object(server, "_cache", {}),
            mock.patch.object(server, "_state", {}),
            mock.patch.object(server, "_state_loaded", True),
        ]
        for source in (server.codex_source, server.ag_source, server.kimi_source, server.devin_source):
            self.patches.append(mock.patch.object(source, "scan_sessions", return_value=[]))
        for patch in self.patches:
            patch.start()
        self.addCleanup(self.tmp.cleanup)
        for patch in self.patches:
            self.addCleanup(patch.stop)

    def append(self, row):
        with self.path.open("a") as handle:
            handle.write(json.dumps(row) + "\n")

    def test_roots_and_scan(self):
        self.assertEqual(pi.sessions_root({}), Path.home() / ".pi/agent/sessions")
        self.assertEqual(pi.sessions_root({"PI_CODING_AGENT_DIR": "/tmp/pi"}), Path("/tmp/pi/sessions"))
        self.assertEqual(pi.sessions_root({"PI_CODING_AGENT_SESSION_DIR": "/tmp/shared"}), Path("/tmp/shared"))
        self.assertTrue(pi.is_pi_path(self.path))
        self.assertFalse(pi.is_pi_path(str(self.sessions) + "-other/a.jsonl"))
        self.assertEqual(list(pi.scan_sessions()), [self.path])
        self.assertEqual(pi.find_session(SID), self.path)
        self.assertIsNone(pi.find_session("missing"))

    def test_metadata_and_fork_not_subagent(self):
        meta = pi.extract_metadata(self.path)
        self.assertEqual(meta["custom_title"], "Orchard review")
        self.assertEqual(meta["id"], SID)
        self.assertEqual(meta["user_turn_count"], 2)
        self.assertFalse(meta["single_turn"])
        self.assertEqual(meta["model"], "example-model")
        self.assertEqual(session_identity.relationship(self.path, "pi"), (False, None))
        self.assertFalse(session_identity.selection_metadata(meta)["is_subagent"])

    def test_active_branch_search_excludes_tools_thinking_injection(self):
        for term in ("abandoned-only", "private-reasoning-only", "tool-only", "injected-only", "synthetic-binary-only"):
            self.assertEqual(pi.search(self.path, [term]), [], term)
            self.assertEqual(cli.search_sessions(term, source="pi"), [], term)
        self.assertTrue(pi.search(self.path, ["orchard", "harvest"]))
        self.assertEqual(len(cli.search_sessions("harvest", source="pi", role="user")), 1)
        self.assertEqual(cli.search_sessions("review", source="pi", role="user"), [])

    def test_conversation_pairs_tools_and_keeps_precompaction_history(self):
        conv = pi.extract_conversation(self.path)
        self.assertEqual(len([t for t in conv["turns"] if t["type"] == "user"]), 2)
        tools = [t for t in conv["turns"] if t["type"] == "tool"]
        self.assertEqual(len(tools), 2)
        self.assertEqual(tools[0]["result"], "tool-only result")
        self.assertFalse(tools[0]["is_error"])
        self.assertTrue(tools[1]["is_error"])
        self.assertNotIn("abandoned-only", json.dumps(conv))
        self.assertNotIn("synthetic-binary-only", json.dumps(conv))
        self.assertIn("[image]", json.dumps(conv))

    def test_anchors_exports_and_evidence(self):
        body = anchored_transcript.render_pi(self.path)
        self.assertIn("[U1] [L2]", body)
        self.assertIn("[L5] TOOL_RESULT OK:", body)
        self.assertIn("[L8] TOOL_RESULT ERROR: failed inspection", body)
        self.assertNotIn("abandoned-only", body)
        self.assertIn("abandoned-only", cli.read_evidence(self.path, 3, 0))
        self.assertIn("## USER", pi.extract_transcript(self.path))
        self.assertEqual(cli.resolve_target(SID, source="pi"), self.path.resolve())
        self.assertIn("Pi session", cli.render_context(self.path))
        self.assertIn("full selected branch", cli.render_context(self.path, 10))
        self.assertIn("Inspect the orchard", cli.render_context(self.path, 10))

    def test_partial_line_and_branch_change(self):
        self.append({"type": "message", "id": "new", "parentId": "u1",
                     "message": {"role": "assistant", "content": "new branch reply"}})
        self.assertEqual(pi.extract_metadata(self.path)["user_turn_count"], 1)
        self.assertTrue(pi.extract_metadata(self.path)["single_turn"])
        self.assertEqual(pi.search(self.path, ["harvest"]), [])
        with self.path.open("a") as handle:
            handle.write('{"type":')
        self.assertIn("new branch reply", anchored_transcript.render_pi(self.path))

    def test_legacy_and_invalid_records(self):
        self.path.write_text(json.dumps({"type": "session", "id": SID, "cwd": "/Users/alice/my-app"}) +
                             '\n{"type":"message","message":{"role":"user","content":"legacy"}}\n')
        self.assertEqual(pi.extract_metadata(self.path)["user_turn_count"], 1)
        self.path.write_text('[]\n{"type":"unknown"}\n')
        self.assertIsNone(pi.extract_metadata(self.path))

    def test_incremental_scan_title_change_and_removal(self):
        self.assertEqual(server.scan_sessions(force=True)[0]["source"], "pi")
        self.append({"type": "session_info", "id": "name2", "parentId": "n1", "name": "Updated orchard"})
        stat = self.path.stat()
        os.utime(self.path, (stat.st_atime, stat.st_mtime + 1))
        self.assertEqual(server.scan_sessions()[0]["custom_title"], "Updated orchard")
        self.path.unlink()
        self.assertEqual(server.scan_sessions(), [])

    def test_http_endpoints_and_local_metadata(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        port = httpd.server_address[1]
        with mock.patch.object(server, "PORT", port):
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                def request(route, body=None):
                    data = json.dumps(body).encode() if body is not None else None
                    req = urllib.request.Request("http://127.0.0.1:" + str(port) + route, data=data,
                                                 headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req) as response:
                        return response.read().decode()
                original = self.path.read_bytes()
                self.assertEqual(json.loads(request("/api/sessions"))[0]["source"], "pi")
                base = "/api/sessions/" + SID
                conv = json.loads(request(base + "/conversation"))
                self.assertEqual(conv["source"], "pi")
                self.assertIn("[U1] [L2]", request(base + "/anchored"))
                self.assertTrue(json.loads(request(base + "/conversation?fingerprint=" + conv["fingerprint"]))["unchanged"])
                request(base + "/title", {"title_override": "Remember fruit"})
                request(base + "/human", {"human_confirmed": True})
                self.assertEqual(json.loads(request("/api/search?q=Remember"))[0]["id"], SID)
                changed = json.loads(request(base + "/conversation"))
                self.assertEqual(changed["display_title"], "Remember fruit")
                self.assertTrue(changed["human_confirmed"])
                self.assertEqual(self.path.read_bytes(), original)
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join()
