"""Scan hot-cache tests."""
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402


class ScanCacheTestCase(unittest.TestCase):
    def setUp(self):
        self.old_cache = dict(server._cache)
        self.old_truth = dict(server._CWD_TRUTH_MAP)
        self.old_seen = set(server._CWD_INDEX_SEEN)
        self.old_seq = {k: list(v) for k, v in server._CWD_SEQ.items()}
        self.old_dirty = server._scan_cache_dirty
        self.old_decode = dict(server._DECODE_DIR_CACHE)
        server._cache.clear()
        server._CWD_TRUTH_MAP.clear()
        server._CWD_INDEX_SEEN.clear()
        server._CWD_SEQ.clear()
        server._DECODE_DIR_CACHE.clear()
        server._scan_cache_dirty = False

    def tearDown(self):
        server._cache.clear()
        server._cache.update(self.old_cache)
        server._CWD_TRUTH_MAP.clear()
        server._CWD_TRUTH_MAP.update(self.old_truth)
        server._CWD_INDEX_SEEN.clear()
        server._CWD_INDEX_SEEN.update(self.old_seen)
        server._CWD_SEQ.clear()
        server._CWD_SEQ.update({k: list(v) for k, v in self.old_seq.items()})
        server._DECODE_DIR_CACHE.clear()
        server._DECODE_DIR_CACHE.update(self.old_decode)
        server._scan_cache_dirty = self.old_dirty

    def _patch_cache_paths(self, root: Path):
        return mock.patch.multiple(
            server,
            SCAN_CACHE_FILE=root / "scan-cache.json",
            SCAN_CACHE_BACKUP_DIR=root / "backups",
        )


class TestScanCachePersistence(ScanCacheTestCase):
    def test_cache_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self._patch_cache_paths(root):
                server._cache["/tmp/a.jsonl"] = {
                    "id": "a",
                    "project_path": "/tmp",
                    "jsonl_path": "/tmp/a.jsonl",
                    "mtime": 123.0,
                    "mtime_iso": "2026-01-01T00:00:00+00:00",
                    "size": 10,
                    "recent_msgs": [],
                    "last_stop_reason": None,
                    "user_turn_count": 1,
                    "custom_title": None,
                }
                server._CWD_TRUTH_MAP["-tmp"] = "/tmp"
                server._CWD_INDEX_SEEN.add("/tmp/a.jsonl")
                server._CWD_SEQ["/tmp/a.jsonl"] = ["/tmp"]
                expected = (
                    dict(server._cache),
                    dict(server._CWD_TRUTH_MAP),
                    set(server._CWD_INDEX_SEEN),
                    {k: list(v) for k, v in server._CWD_SEQ.items()},
                )

                server._scan_cache_dirty = True
                self.assertTrue(server.save_scan_cache())

                server._cache.clear()
                server._CWD_TRUTH_MAP.clear()
                server._CWD_INDEX_SEEN.clear()
                server._CWD_SEQ.clear()

                self.assertTrue(server.load_scan_cache())
                self.assertEqual(server._cache, expected[0])
                self.assertEqual(server._CWD_TRUTH_MAP, expected[1])
                self.assertEqual(server._CWD_INDEX_SEEN, expected[2])
                self.assertEqual(server._CWD_SEQ, expected[3])

    def test_schema_version_mismatch_returns_false_and_leaves_globals(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self._patch_cache_paths(root):
                server.SCAN_CACHE_FILE.write_text(
                    json.dumps({
                        "schema_version": server.CACHE_SCHEMA_VERSION + 1,
                        "cache": {},
                        "cwd_truth_map": {},
                        "cwd_index_seen": [],
                        "cwd_seq": {},
                    }),
                    encoding="utf-8",
                )
                server._cache["sentinel"] = {"id": "sentinel"}
                server._CWD_TRUTH_MAP["encoded"] = "/sentinel"
                server._CWD_INDEX_SEEN.add("seen")
                server._CWD_SEQ["seen"] = ["/sentinel"]
                before = (
                    dict(server._cache),
                    dict(server._CWD_TRUTH_MAP),
                    set(server._CWD_INDEX_SEEN),
                    {k: list(v) for k, v in server._CWD_SEQ.items()},
                )

                self.assertFalse(server.load_scan_cache())
                self.assertEqual(server._cache, before[0])
                self.assertEqual(server._CWD_TRUTH_MAP, before[1])
                self.assertEqual(server._CWD_INDEX_SEEN, before[2])
                self.assertEqual(server._CWD_SEQ, before[3])

    def test_corrupt_file_returns_false_without_crash(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self._patch_cache_paths(root):
                server.SCAN_CACHE_FILE.write_text("{not-json", encoding="utf-8")
                self.assertFalse(server.load_scan_cache())

    def test_missing_file_returns_false_without_crash(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self._patch_cache_paths(root):
                self.assertFalse(server.load_scan_cache())


class TestScanCacheIncremental(ScanCacheTestCase):
    def _write_session(self, path: Path, cwd: str, text: str):
        path.write_text(
            json.dumps({
                "type": "user",
                "cwd": cwd,
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"role": "user", "content": text},
            }) + "\n",
            encoding="utf-8",
        )

    def _fake_meta(self, path: Path):
        st = path.stat()
        return {
            "id": path.stem,
            "project_path": "/tmp/project",
            "jsonl_path": str(path),
            "mtime": st.st_mtime,
            "mtime_iso": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            "size": st.st_size,
            "recent_msgs": [],
            "last_stop_reason": None,
            "user_turn_count": 1,
            "custom_title": None,
        }

    def test_changed_mtime_is_reread_and_unchanged_sessions_are_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            projects_dir = root / "projects"
            project = projects_dir / "-tmp-project"
            project.mkdir(parents=True)
            a = project / "a.jsonl"
            b = project / "b.jsonl"
            self._write_session(a, "/tmp/project", "a1")
            self._write_session(b, "/tmp/project", "b1")
            base = time.time() - 100
            os.utime(a, (base, base))
            os.utime(b, (base, base))

            calls = []

            def fake_extract(path):
                calls.append(Path(path).name)
                return self._fake_meta(Path(path))

            with self._patch_cache_paths(root), \
                    mock.patch.object(server, "PROJECTS_DIR", projects_dir), \
                    mock.patch.object(server.codex_source, "scan_sessions", return_value=[]), \
                    mock.patch.object(server.ag_source, "scan_sessions", return_value=[]), \
                    mock.patch.object(server, "extract_metadata", side_effect=fake_extract):
                server.scan_sessions(force=True)
                self.assertCountEqual(calls, ["a.jsonl", "b.jsonl"])

                calls.clear()
                server.scan_sessions(force=False)
                self.assertEqual(calls, [])

                self._write_session(a, "/tmp/project", "a2")
                new_time = base + 50
                os.utime(a, (new_time, new_time))

                server.scan_sessions(force=False)
                self.assertEqual(calls, ["a.jsonl"])


if __name__ == "__main__":
    unittest.main()
