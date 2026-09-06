"""The /conversation endpoint's file fingerprint, which drives the standalone reader's live refresh.

Contract:
  * every full response carries `fingerprint` (mtime + size of the session file);
  * `?fingerprint=<seen>` answers `{unchanged: true}` without re-parsing when the file is untouched;
  * once the file grows, the same query returns the full, longer conversation with a new fingerprint.
"""
import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import server

SID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CWD = "/Users/alice/my-app"


def _user(text, sec):
    return {"type": "user", "timestamp": f"2026-08-20T10:00:{sec:02d}Z", "cwd": CWD,
            "message": {"content": text}}


def _assistant(text, sec):
    return {"type": "assistant", "timestamp": f"2026-08-20T10:00:{sec:02d}Z", "cwd": CWD,
            "message": {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}}


class FileFingerprintTests(unittest.TestCase):
    def test_changes_when_file_grows(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.jsonl"
            p.write_text("a\n")
            first = server.file_fingerprint(p)
            self.assertRegex(first, r"^\d+:2$")
            with open(p, "a") as f:
                f.write("bb\n")
            self.assertNotEqual(first, server.file_fingerprint(p))


class ConversationFingerprintEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.projects = root / "projects"
        self.jsonl = self.projects / "-Users-alice-my-app" / f"{SID}.jsonl"
        self.jsonl.parent.mkdir(parents=True)
        self.jsonl.write_text(json.dumps(_user("first question", 0)) + "\n"
                              + json.dumps(_assistant("first answer", 1)) + "\n")
        self.patches = [
            mock.patch.object(server, "PROJECTS_DIR", self.projects),
            mock.patch.object(server, "STATE_FILE", root / "state.json"),
            mock.patch.object(server, "SCAN_CACHE_FILE", root / "scan-cache.json"),
            mock.patch.object(server.codex_source, "scan_sessions", return_value=[]),
            mock.patch.object(server.ag_source, "scan_sessions", return_value=[]),
            mock.patch.dict(server._cache, {}, clear=True),
        ]
        for p in self.patches:
            p.start()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        # The handler only trusts requests whose Host matches the configured port
        port_patch = mock.patch.object(server, "PORT", self.port)
        port_patch.start()
        self.patches.append(port_patch)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _get(self, query=""):
        url = f"http://127.0.0.1:{self.port}/api/sessions/{SID}/conversation{query}"
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))

    def test_fingerprint_roundtrip(self):
        status, first = self._get()
        self.assertEqual(status, 200)
        self.assertIn("fingerprint", first)
        self.assertEqual(len(first["turns"]), 2)

        status, same = self._get(f"?fingerprint={first['fingerprint']}")
        self.assertEqual(status, 200)
        self.assertEqual(same, {"id": SID, "unchanged": True, "fingerprint": first["fingerprint"]})

        # A stale or bogus fingerprint must never short-circuit
        status, full = self._get("?fingerprint=bogus")
        self.assertEqual(len(full["turns"]), 2)

        with open(self.jsonl, "a") as f:
            f.write(json.dumps(_user("second question", 30)) + "\n")
        status, grown = self._get(f"?fingerprint={first['fingerprint']}")
        self.assertNotIn("unchanged", grown)
        self.assertNotEqual(grown["fingerprint"], first["fingerprint"])
        self.assertEqual(len(grown["turns"]), 3)
        self.assertEqual(grown["total_lines"], 3)


if __name__ == "__main__":
    unittest.main()
