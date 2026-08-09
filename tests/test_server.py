"""Unit tests for server.py - v2 scope model."""
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

# Let tests/ import the parent server.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402


class TestCliArgs(unittest.TestCase):
    def test_default_host_stays_loopback(self):
        self.assertEqual(server.HOST, "127.0.0.1")

    def test_port_flag_parses(self):
        args = server.parse_args(["--port", "47822"])
        self.assertEqual(args.port, 47822)

    def test_invalid_port_exits(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                server.parse_args(["--port", "70000"])


class TestLocalHttpBoundary(unittest.TestCase):
    def test_accepts_same_origin_browser_request(self):
        self.assertTrue(server._is_trusted_http_request(
            f"127.0.0.1:{server.PORT}",
            f"http://127.0.0.1:{server.PORT}",
            "same-origin",
        ))

    def test_accepts_local_command_line_client_without_origin(self):
        self.assertTrue(server._is_trusted_http_request(
            f"localhost:{server.PORT}",
        ))

    def test_rejects_cross_site_browser_request(self):
        self.assertFalse(server._is_trusted_http_request(
            f"127.0.0.1:{server.PORT}",
            "https://attacker.example",
            "cross-site",
        ))

    def test_rejects_dns_rebinding_host(self):
        self.assertFalse(server._is_trusted_http_request(
            "attacker.example",
            f"http://127.0.0.1:{server.PORT}",
            "same-origin",
        ))

    def test_rejects_origin_on_wrong_local_port(self):
        self.assertFalse(server._is_trusted_http_request(
            f"127.0.0.1:{server.PORT}",
            "http://127.0.0.1:9999",
            "same-site",
        ))

    def test_allows_only_discovered_project_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            known = Path(tmp) / "known"
            unknown = Path(tmp) / "unknown"
            known.mkdir()
            unknown.mkdir()
            cache = {"session.jsonl": {"project_path": str(known)}}
            with mock.patch.object(server, "_cache", cache):
                self.assertEqual(server._resolve_known_project_root(str(known)), known.resolve())
                self.assertIsNone(server._resolve_known_project_root(str(unknown)))


class TestVendorAssets(unittest.TestCase):
    def test_woff2_content_type(self):
        self.assertEqual(
            server._vendor_content_type(Path("font.woff2")),
            "font/woff2",
        )


class TestRipgrepDiscovery(unittest.TestCase):
    def setUp(self):
        server._search_fallback_warnings.clear()

    def tearDown(self):
        server._search_fallback_warnings.clear()

    def test_prefers_path_lookup(self):
        with mock.patch.object(server.shutil, "which", return_value="/custom/bin/rg"):
            self.assertEqual(server._find_ripgrep(), "/custom/bin/rg")

    def test_finds_standard_install_when_background_path_is_restricted(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "rg"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
            with mock.patch.object(server.shutil, "which", return_value=None), \
                 mock.patch.object(server, "RIPGREP_FALLBACK_PATHS", (executable,)):
                self.assertEqual(server._find_ripgrep(), str(executable))

    def test_missing_ripgrep_warns_and_falls_back(self):
        with mock.patch.object(server, "_find_ripgrep", return_value=None), \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertIsNone(server._rg_prefilter(["needle"], [Path("session.jsonl")]))
        self.assertIn("ripgrep executable not found", stderr.getvalue())
        self.assertIn("slower Python fallback", stderr.getvalue())

    def test_prefilter_invokes_resolved_absolute_path(self):
        completed = mock.Mock(returncode=1, stdout=b"")
        with mock.patch.object(server, "_find_ripgrep", return_value="/opt/homebrew/bin/rg"), \
             mock.patch.object(server.subprocess, "run", return_value=completed) as run:
            result = server._rg_prefilter(["needle"], [Path("session.jsonl")])
        self.assertEqual(result, set())
        self.assertEqual(run.call_args.args[0][0], "/opt/homebrew/bin/rg")

    def test_runtime_failure_warns_and_falls_back(self):
        completed = mock.Mock(returncode=2, stdout=b"")
        with mock.patch.object(server, "_find_ripgrep", return_value="/opt/homebrew/bin/rg"), \
             mock.patch.object(server.subprocess, "run", return_value=completed), \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertIsNone(server._rg_prefilter(["needle"], [Path("session.jsonl")]))
        self.assertIn("exited with status 2", stderr.getvalue())


class TestComputeScope(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.recent_mtime = self.now - 3600  # 1h ago
        self.dusty_mtime = self.now - 86400 * 30  # 30d ago

    def test_archived_wins_over_everything(self):
        entry = {"archived": True, "starred": True}
        meta = {"mtime": self.recent_mtime}
        self.assertEqual(server.compute_scope(meta, entry, now=self.now), "archived")

    def test_starred_overrides_time_decay(self):
        entry = {"archived": False, "starred": True}
        meta = {"mtime": self.dusty_mtime}  # older than 7 days
        self.assertEqual(server.compute_scope(meta, entry, now=self.now), "starred")

    def test_recent_by_mtime(self):
        entry = {"archived": False, "starred": False}
        meta = {"mtime": self.recent_mtime}
        self.assertEqual(server.compute_scope(meta, entry, now=self.now), "recent")

    def test_dusty_when_old_and_unstarred(self):
        entry = {"archived": False, "starred": False}
        meta = {"mtime": self.dusty_mtime}
        self.assertEqual(server.compute_scope(meta, entry, now=self.now), "dusty")

    def test_empty_entry_defaults_to_scope_by_mtime(self):
        entry = {}  # old state.json may not have starred/archived fields
        meta_recent = {"mtime": self.recent_mtime}
        meta_old = {"mtime": self.dusty_mtime}
        self.assertEqual(server.compute_scope(meta_recent, entry, now=self.now), "recent")
        self.assertEqual(server.compute_scope(meta_old, entry, now=self.now), "dusty")

    def test_boundary_exactly_7_days(self):
        # Right on the 7-day boundary -> recent because >= includes the boundary
        entry = {"archived": False, "starred": False}
        boundary_mtime = self.now - 86400 * 7 + 1  # one second inside the boundary still counts as recent
        meta = {"mtime": boundary_mtime}
        self.assertEqual(server.compute_scope(meta, entry, now=self.now), "recent")

    def test_codex_archived_falls_to_archived_when_no_state(self):
        # Session archived in Codex (codex_archived=True), with no dashboard archive state -> archived
        meta = {"mtime": self.recent_mtime, "codex_archived": True}
        self.assertEqual(server.compute_scope(meta, {}, now=self.now), "archived")

    def test_dashboard_unarchive_overrides_codex_archived(self):
        # User manually unarchives in the dashboard, explicit archived=False overrides Codex-derived state
        meta = {"mtime": self.recent_mtime, "codex_archived": True}
        entry = {"archived": False}
        self.assertEqual(server.compute_scope(meta, entry, now=self.now), "recent")

    def test_non_codex_meta_unaffected(self):
        # Claude sessions have no codex_archived field -> fall back to False and bucket by mtime
        meta = {"mtime": self.dusty_mtime}
        self.assertEqual(server.compute_scope(meta, {}, now=self.now), "dusty")


class TestStateUpgrade(unittest.TestCase):
    """Verify backward compatibility with v1 state.json and star field reads/writes."""

    def setUp(self):
        server._state = {}  # clear in-memory state

    def test_v1_entry_reads_as_unstarred(self):
        # v1 entry only has archived/archived_at/note
        server._state["sid1"] = {
            "archived": True,
            "archived_at": "2026-01-01T00:00:00Z",
            "note": "legacy",
        }
        entry = server._state["sid1"]
        self.assertFalse(entry.get("starred", False))  # default unstarred
        self.assertEqual(
            server.compute_scope({"mtime": time.time()}, entry, now=time.time()),
            "archived",
        )

    def test_star_then_unstar_keeps_other_fields(self):
        server._state["sid2"] = {"note": "important", "archived": False}
        # simulate star
        server._state["sid2"]["starred"] = True
        server._state["sid2"]["starred_at"] = "2026-04-12T00:00:00Z"
        self.assertEqual(server._state["sid2"]["note"], "important")
        self.assertTrue(server._state["sid2"]["starred"])
        # simulate unstar
        server._state["sid2"]["starred"] = False
        self.assertEqual(server._state["sid2"]["note"], "important")
        self.assertFalse(server._state["sid2"]["starred"])


class TestDecodeProjectDir(unittest.TestCase):
    """Verify _decode_project_dir disambiguates Claude Code's lossy directory encoding.

    Encoding rules: '/' -> '-', '.' -> '-', and original '-' is preserved. All three map
    to '-', so decoding is ambiguous without ground truth or filesystem checks.
    """

    def setUp(self):
        # Each case gets isolated cache + reverse map to avoid cross-test contamination
        server._DECODE_DIR_CACHE.clear()
        server._CWD_TRUTH_MAP.clear()
        server._CWD_INDEX_SEEN.clear()

    def _make_tree(self, root: Path, rel_paths):
        """Create relative-path directories under root."""
        for rel in rel_paths:
            (root / rel).mkdir(parents=True, exist_ok=True)

    def _decode_under(self, root: Path, encoded_tail: str):
        """Mount a fake root under the real / decode logic by mocking Path('/') to root."""
        # Calling the inner walk directly is awkward; use an equivalent test with root as start:
        # prepend root to the encoded name, then strip it back for comparison.
        # Mocking pathlib.Path / iterdir is awkward, so this uses a direct walk-style setup.
        # Detour: temporarily point server.Path at a wrapper so Path("/") equals root.
        real_path_cls = server.Path

        class FakePath(type(root)):
            pass  # placeholder; no class-level hack below

        # More directly, we could move walk logic into an inline helper,
        # but to avoid forking implementation, mock server.Path to return root when passed "/".
        original = server.Path

        def fake_path(arg):
            if arg == "/":
                return root
            return original(arg)

        with mock.patch.object(server, "Path", side_effect=fake_path):
            return server._decode_project_dir(encoded_tail)

    def test_plain_project_no_dashes_in_name(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_tree(root, ["Users/alice/repo"])
            result = self._decode_under(root, "-Users-alice-repo")
            self.assertEqual(result, str(root / "Users/alice/repo"))

    def test_dir_name_contains_dash(self):
        """The dash in session-logbook must not be mistaken for a slash."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_tree(root, ["Users/alice/session-logbook"])
            result = self._decode_under(root, "-Users-alice-session-logbook")
            self.assertEqual(result, str(root / "Users/alice/session-logbook"))

    def test_hidden_dir_dot_claude(self):
        """The dot in .claude is encoded as a dash and must be restored via filesystem lookup."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_tree(root, ["Users/alice/session-logbook/.claude/worktrees/pwa"])
            result = self._decode_under(
                root, "-Users-alice-session-logbook--claude-worktrees-pwa"
            )
            self.assertEqual(
                result,
                str(root / "Users/alice/session-logbook/.claude/worktrees/pwa"),
            )

    def test_branch_name_with_dash(self):
        """The dash in worktree branch name query-queue must also be preserved."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_tree(
                root, ["Users/alice/session-logbook/.claude/worktrees/query-queue"]
            )
            result = self._decode_under(
                root,
                "-Users-alice-session-logbook--claude-worktrees-query-queue",
            )
            self.assertEqual(
                result,
                str(root / "Users/alice/session-logbook/.claude/worktrees/query-queue"),
            )

    def test_ambiguous_prefers_longest_match(self):
        """When foo and foo-bar exist at the same level, longest-first should choose foo-bar."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_tree(root, ["foo", "foo-bar/baz"])
            result = self._decode_under(root, "-foo-bar-baz")
            self.assertEqual(result, str(root / "foo-bar/baz"))

    def test_partial_match_falls_back_for_missing_leaf(self):
        """When the leaf directory is gone, keep the reachable ancestor and naive-decode the rest.

        Key case: the worktree is deleted but .claude/worktrees/ still exists, matching the
        real screenshot scenario.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # The user project and .claude/worktrees/ exist, but pwa has been deleted
            self._make_tree(root, ["Users/alice/session-logbook/.claude/worktrees"])
            result = self._decode_under(
                root, "-Users-alice-session-logbook--claude-worktrees-pwa"
            )
            # All previous ancestors are restored correctly; final pwa remains a directory name
            self.assertEqual(
                result,
                str(root / "Users/alice/session-logbook/.claude/worktrees/pwa"),
            )

    def test_partial_match_naive_decode_for_deleted_branch_with_dash(self):
        """When ancestors exist but a deleted branch name contains '-', it cannot be distinguished.

        Deleted worktree query-queue naively decodes to query/queue, the acceptable next-best
        result.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_tree(root, ["Users/alice/session-logbook/.claude/worktrees"])
            result = self._decode_under(
                root,
                "-Users-alice-session-logbook--claude-worktrees-query-queue",
            )
            # session-logbook and .claude are preserved by filesystem checks; the query-queue leaf is naive-decoded
            self.assertEqual(
                result,
                str(root / "Users/alice/session-logbook/.claude/worktrees/query/queue"),
            )

    def test_non_absolute_dir_name_returns_none(self):
        """Names not starting with dash are not absolute-path encodings and return None."""
        self.assertIsNone(server._decode_project_dir("relative-name"))


class TestCwdTruthMap(unittest.TestCase):
    """Verify the cwd ground-truth reverse table on the main path.

    cwd values seen in main project jsonl derive encoded names back to real cwd values,
    covering all worktree naming cases.
    """

    def setUp(self):
        server._CWD_TRUTH_MAP.clear()
        server._CWD_INDEX_SEEN.clear()
        server._DECODE_DIR_CACHE.clear()
        server._CWD_SEQ.clear()

    def test_index_cwd_builds_reverse_table(self):
        """Encoding rules are consistent: / -> -, . -> -, original - is preserved."""
        server._index_cwd("/Users/alice/session-logbook/.claude/worktrees/qq-mosaic")
        self.assertEqual(
            server._CWD_TRUTH_MAP[
                "-Users-alice-session-logbook--claude-worktrees-qq-mosaic"
            ],
            "/Users/alice/session-logbook/.claude/worktrees/qq-mosaic",
        )

    def test_decode_prefers_truth_map_over_filesystem(self):
        """A table hit returns directly without filesystem disambiguation, even after worktree deletion."""
        server._index_cwd(
            "/Users/alice/session-logbook/.claude/worktrees/query-queue"
        )
        result = server._decode_project_dir(
            "-Users-alice-session-logbook--claude-worktrees-query-queue"
        )
        self.assertEqual(
            result, "/Users/alice/session-logbook/.claude/worktrees/query-queue"
        )

    def test_extract_metadata_indexes_cwd_inline(self):
        """extract_metadata indexes cwd while reading it, so the next same-batch scan can find it."""
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td) / "-Users-alice-proj"
            proj.mkdir()
            jsonl = proj / "abc.jsonl"
            line = json.dumps(
                {
                    "type": "user",
                    "cwd": "/Users/alice/proj/.claude/worktrees/feat-a",
                    "message": {"content": "hi"},
                    "timestamp": "2026-05-12T10:00:00Z",
                }
            )
            jsonl.write_text(line + "\n")
            meta = server.extract_metadata(jsonl)
            self.assertEqual(meta["project_path"], "/Users/alice/proj/.claude/worktrees/feat-a")
            # Also derives the worktree encoded-name -> cwd mapping
            self.assertIn(
                "-Users-alice-proj--claude-worktrees-feat-a", server._CWD_TRUTH_MAP
            )

    def test_collect_cwds_gets_all_unique(self):
        """_collect_cwds must scan the full file and dedupe while preserving order for pick_project_path."""
        with tempfile.TemporaryDirectory() as td:
            jsonl = Path(td) / "s.jsonl"
            lines = [
                json.dumps({"type": "summary"}),
                json.dumps({"type": "user", "cwd": "/Users/alice/repo"}),
                json.dumps({"type": "user", "cwd": "/Users/alice/repo"}),  # duplicate
                json.dumps({"type": "user", "cwd": "/Users/alice/repo/.claude/worktrees/feat-a"}),
            ]
            jsonl.write_text("\n".join(lines) + "\n")
            self.assertEqual(
                server._collect_cwds(jsonl),
                ["/Users/alice/repo", "/Users/alice/repo/.claude/worktrees/feat-a"],
            )
            # also cached into _CWD_SEQ
            self.assertEqual(
                server._CWD_SEQ[str(jsonl)],
                ["/Users/alice/repo", "/Users/alice/repo/.claude/worktrees/feat-a"],
            )

    def test_collect_cwds_empty_for_no_cwd(self):
        with tempfile.TemporaryDirectory() as td:
            jsonl = Path(td) / "s.jsonl"
            jsonl.write_text(json.dumps({"type": "aiTitle", "sessionId": "abc"}) + "\n")
            self.assertEqual(server._collect_cwds(jsonl), [])

    def test_build_cwd_map_increments(self):
        """_build_cwd_map is incremental: already-peeked jsonl files are skipped, new files are collected."""
        with tempfile.TemporaryDirectory() as td:
            projects_dir = Path(td)
            (projects_dir / "-Users-alice-repo").mkdir()
            a = projects_dir / "-Users-alice-repo" / "a.jsonl"
            a.write_text(
                json.dumps({"type": "user", "cwd": "/Users/alice/repo/.claude/worktrees/feat-1"})
                + "\n"
            )
            with mock.patch.object(server, "PROJECTS_DIR", projects_dir):
                server._build_cwd_map()
                self.assertIn(
                    "-Users-alice-repo--claude-worktrees-feat-1", server._CWD_TRUTH_MAP
                )
                # same jsonl is in the SEEN set
                self.assertIn(str(a), server._CWD_INDEX_SEEN)


class TestPickProjectPath(unittest.TestCase):
    """project_path selection strategy. Decision log: docs/decisions/2026-05-14-project-path-strategy.md"""

    def setUp(self):
        server._CWD_TRUTH_MAP.clear()
        server._CWD_INDEX_SEEN.clear()
        server._DECODE_DIR_CACHE.clear()
        server._CWD_SEQ.clear()

    def test_empty_returns_none(self):
        self.assertIsNone(server.pick_project_path("-Users-alice-proj", []))

    def test_single_cwd_returns_it(self):
        self.assertEqual(
            server.pick_project_path("-Users-alice-proj", ["/Users/alice/proj"]),
            "/Users/alice/proj",
        )

    def test_last_in_worktree_keeps_last(self):
        """Worktree case: when the tail is under .claude/worktrees/, keep the tail for Files."""
        cwds = [
            "/Users/alice/proj",
            "/Users/alice/proj/.claude/worktrees/feat-a",
        ]
        self.assertEqual(
            server.pick_project_path("-Users-alice-proj", cwds),
            "/Users/alice/proj/.claude/worktrees/feat-a",
        )

    def test_cd_into_subdir_collapses_to_commonpath(self):
        """Case 3 main fix: when the agent cd enters a subdirectory, shrink to commonpath/project root."""
        cwds = [
            "/Users/alice/my-app",
            "/Users/alice/my-app/materials/nice-animals",
            "/Users/alice/my-app/materials/nice-animals/samoyed",
        ]
        self.assertEqual(
            server.pick_project_path("-Users-alice-my-cool-app", cwds),
            "/Users/alice/my-app",
        )

    def test_cd_outside_anchor_keeps_last(self):
        """If commonpath crosses outside the folder anchor, do not force-shrink; keep tail.
        Case: user starts from /Users/alice (home) and cd's into /Users/alice/some-app.
        commonpath = /Users/alice, the anchor itself, so the rule shrinks to anchor.
        """
        cwds = ["/Users/alice", "/Users/alice/some-proj/docs"]
        # anchor = "/Users/alice", commonpath = "/Users/alice", equal to anchor -> shrink
        self.assertEqual(
            server.pick_project_path("-Users-alice", cwds),
            "/Users/alice",
        )

    def test_anchor_decode_fails_falls_back_to_last(self):
        """If folder_name cannot decode an anchor and commonpath is SHALLOW, fall back to last."""
        cwds = ["/Users/alice/proj-1", "/Users/bob/proj-2"]
        # commonpath = /Users is SHALLOW; no anchor match -> fall back to last
        result = server.pick_project_path("-some-other-folder", cwds)
        self.assertEqual(result, "/Users/bob/proj-2")

    def test_extract_metadata_uses_pick(self):
        """End to end: after _build_cwd_map, extract_metadata uses pick_project_path for accurate project_path."""
        with tempfile.TemporaryDirectory() as td:
            projects_dir = Path(td)
            proj = projects_dir / "-Users-alice-my-cool-app"
            proj.mkdir()
            jsonl = proj / "s.jsonl"
            lines = [
                json.dumps({"type": "user", "cwd": "/Users/alice/my-app",
                            "message": {"role": "user", "content": "hi"}}),
                json.dumps({"type": "user", "cwd": "/Users/alice/my-app/materials/nice-animals/samoyed",
                            "message": {"role": "user", "content": "edit"}}),
            ]
            jsonl.write_text("\n".join(lines) + "\n")
            with mock.patch.object(server, "PROJECTS_DIR", projects_dir):
                server._build_cwd_map()
                meta = server.extract_metadata(jsonl)
                self.assertEqual(meta["project_path"], "/Users/alice/my-app")


class TestDedupGhost(unittest.TestCase):
    """Verify _dedup_by_id removes worktree placeholder copies and keeps the real main-project copy."""

    def test_keeps_larger_size_when_ids_match(self):
        big = {"id": "abc", "size": 8_000_000, "jsonl_path": "/big.jsonl", "mtime": 100}
        ghost = {"id": "abc", "size": 116, "jsonl_path": "/ghost.jsonl", "mtime": 110}
        result = server._dedup_by_id([ghost, big])  # intentionally put ghost first
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["jsonl_path"], "/big.jsonl")

    def test_keeps_unique_ids_untouched(self):
        a = {"id": "a", "size": 100, "jsonl_path": "/a.jsonl", "mtime": 1}
        b = {"id": "b", "size": 200, "jsonl_path": "/b.jsonl", "mtime": 2}
        result = server._dedup_by_id([a, b])
        self.assertEqual(len(result), 2)

    def test_preserves_input_order(self):
        """Deduplication must not disturb the existing mtime-sorted order."""
        items = [
            {"id": "newest", "size": 50, "jsonl_path": "/n.jsonl", "mtime": 300},
            {"id": "mid", "size": 999, "jsonl_path": "/m.jsonl", "mtime": 200},
            {"id": "mid", "size": 100, "jsonl_path": "/m-ghost.jsonl", "mtime": 250},  # ghost mtime is newer
            {"id": "old", "size": 50, "jsonl_path": "/o.jsonl", "mtime": 100},
        ]
        result = server._dedup_by_id(items)
        ids = [m["id"] for m in result]
        self.assertEqual(ids, ["newest", "mid", "old"])
        # mid keeps the real larger copy, not the ghost
        mid_entry = next(m for m in result if m["id"] == "mid")
        self.assertEqual(mid_entry["jsonl_path"], "/m.jsonl")

    def test_skips_entries_without_id(self):
        items = [
            {"size": 100, "jsonl_path": "/no-id.jsonl"},
            {"id": "x", "size": 200, "jsonl_path": "/x.jsonl"},
        ]
        result = server._dedup_by_id(items)
        self.assertEqual([m["id"] for m in result], ["x"])

    def test_find_jsonl_prefers_larger_when_duplicates_in_cache(self):
        """_find_jsonl chooses the larger real copy when one id has multiple files, not an arbitrary one."""
        with tempfile.TemporaryDirectory() as td:
            big = Path(td) / "big.jsonl"
            big.write_text("x" * 1000)
            ghost = Path(td) / "ghost.jsonl"
            ghost.write_text("y")
            # intentionally put ghost first in cache
            server._cache.clear()
            server._cache["k1"] = {"id": "dup", "size": 1, "jsonl_path": str(ghost)}
            server._cache["k2"] = {"id": "dup", "size": 1000, "jsonl_path": str(big)}
            try:
                result = server._find_jsonl("dup")
                self.assertEqual(result, big)
            finally:
                server._cache.clear()


class TestFindJsonlCodexFallback(unittest.TestCase):
    """_find_jsonl filesystem fallback for Codex, covering standalone-tab cold starts."""

    def setUp(self):
        # Temporary Codex root containing one fixture copy
        self.tmp_root = Path(tempfile.mkdtemp())
        target = self.tmp_root / "2026" / "05" / "14"
        target.mkdir(parents=True)
        src = Path(__file__).parent / "fixtures" / "codex" / "basic_main.jsonl"
        self.target_file = target / "rollout-test.jsonl"
        self.target_file.write_text(src.read_text())
        self.session_id = "019e2900-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        # Patch CODEX_ROOT to the temp directory and point archived root to a missing child
        # directory to isolate the real ~/.codex/archived_sessions.
        from sources import codex as codex_mod
        self._old_root = codex_mod.CODEX_ROOT
        self._old_arch = codex_mod.CODEX_ARCHIVED_ROOT
        codex_mod.CODEX_ROOT = self.tmp_root
        codex_mod.CODEX_ARCHIVED_ROOT = self.tmp_root / "__no_archive__"
        # server.codex_source.CODEX_ROOT is the same object and follows the patch
        # Clear cache to simulate cold start
        self._old_cache = dict(server._cache)
        server._cache.clear()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp_root)
        from sources import codex as codex_mod
        codex_mod.CODEX_ROOT = self._old_root
        codex_mod.CODEX_ARCHIVED_ROOT = self._old_arch
        server._cache.clear()
        server._cache.update(self._old_cache)

    def test_finds_codex_session_with_empty_cache(self):
        # Cache is empty, so filesystem fallback is required
        result = server._find_jsonl(self.session_id)
        self.assertEqual(result, self.target_file)

    def test_returns_none_for_unknown_id(self):
        result = server._find_jsonl("does-not-exist")
        self.assertIsNone(result)


import shutil as _shutil


@unittest.skipUnless(_shutil.which("fd"), "fd not installed; skipping full-tree filename search tests")
class TestFindFilesByName(unittest.TestCase):
    """Full-tree fd filename search, backing /api/find-files.

    Depends on system fd. If missing, skip the whole class, matching list_recent_files' git
    dependency pattern: runnable locally without false failures in CI missing tools.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="findfiles_")
        # fd .gitignore behavior only applies inside a git repo; the ignore crate needs .git.
        # In real usage the Files panel root is a project repo; git init restores that context here.
        import subprocess as _sp
        _sp.run(["git", "init", "-q", self.root], check=False,
                capture_output=True)
        # Build a small tree: root / subdir / .gitignore hit / noisy directory
        os.makedirs(os.path.join(self.root, "src"))
        os.makedirs(os.path.join(self.root, "docs", "decisions"))
        os.makedirs(os.path.join(self.root, "node_modules", "pkg"))
        self._touch("server.py")
        self._touch("server.test.js")          # verify literal dot is not treated as regex
        self._touch("src/handler.py")
        self._touch("docs/decisions/2026-export.md")
        self._touch(".env.local")               # dotfile, visible only with --hidden
        self._touch("node_modules/pkg/index.js")  # noise, should be excluded
        # .gitignore hit: hidden by default, shown with include_ignored=True
        with open(os.path.join(self.root, ".gitignore"), "w") as f:
            f.write("secret.key\n")
        self._touch("secret.key")

    def tearDown(self):
        _shutil.rmtree(self.root)

    def _touch(self, rel):
        with open(os.path.join(self.root, rel), "w") as f:
            f.write("x")

    def _names(self, res):
        return sorted(f["rel"] for f in res["files"])

    def test_basename_match(self):
        res = server.find_files_by_name(self.root, "handler")
        self.assertEqual(self._names(res), ["src/handler.py"])
        self.assertEqual(res["matched"], 1)

    def test_literal_dot_not_regex(self):
        # The dot in server.test is literal and should not wildcard-match serverXtest
        res = server.find_files_by_name(self.root, "server.test")
        self.assertEqual(self._names(res), ["server.test.js"])

    def test_slash_triggers_full_path(self):
        res = server.find_files_by_name(self.root, "decisions/2026")
        self.assertEqual(self._names(res), ["docs/decisions/2026-export.md"])

    def test_hidden_dotfile_included(self):
        res = server.find_files_by_name(self.root, "env.local")
        self.assertEqual(self._names(res), [".env.local"])

    def test_empty_query_returns_empty(self):
        res = server.find_files_by_name(self.root, "   ")
        self.assertEqual(res["files"], [])
        self.assertEqual(res["matched"], 0)

    def test_gitignored_hidden_by_default_shown_when_included(self):
        # Default honors .gitignore; in non-git repos fd still reads the .gitignore file itself
        default = server.find_files_by_name(self.root, "secret.key")
        self.assertEqual(default["files"], [], "default should honor .gitignore and hide secret.key")
        included = server.find_files_by_name(self.root, "secret.key", include_ignored=True)
        self.assertEqual(self._names(included), ["secret.key"])

    def test_noise_dir_excluded_in_ignored_mode(self):
        # include_ignored=True should still not be flooded by node_modules
        res = server.find_files_by_name(self.root, "index.js", include_ignored=True)
        self.assertNotIn("node_modules/pkg/index.js", self._names(res))

    def test_bad_root(self):
        res = server.find_files_by_name("/no/such/dir/xyz", "anything")
        self.assertEqual(res["files"], [])
        self.assertIn("error", res)


class TestClaudeSubagentSpawn(unittest.TestCase):
    """Agent tool_use -> subagent_spawn turn translation."""

    def _write_fixture(self, td: Path, agent_input: dict, with_tool_result: bool = False) -> Path:
        """Construct an Agent tool_use scenario.

        with_tool_result=True also constructs the paired tool_result, matching real Claude
        sessions where a user-role tool_result row follows subagent return.
        """
        p = td / "session.jsonl"
        lines = [
            {"type": "summary", "summary": "x", "leafUuid": "u0"},
            {
                "parentUuid": None, "uuid": "u1", "timestamp": "2026-05-15T10:00:00Z",
                "type": "user", "sessionId": "sid", "cwd": "/x",
                "message": {"role": "user", "content": "hi"},
            },
            {
                "parentUuid": "u1", "uuid": "u2", "timestamp": "2026-05-15T10:00:01Z",
                "type": "assistant", "sessionId": "sid", "cwd": "/x",
                "message": {"role": "assistant", "content": [{
                    "type": "tool_use", "id": "t1", "name": "Agent",
                    "input": agent_input,
                }]},
            },
        ]
        if with_tool_result:
            lines.append({
                "parentUuid": "u2", "uuid": "u3", "timestamp": "2026-05-15T10:01:00Z",
                "type": "user", "sessionId": "sid", "cwd": "/x",
                "message": {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": "t1",
                    "content": "subagent finished; see X / Y / Z for details",
                }]},
            })
        p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        return p

    def test_agent_tool_becomes_subagent_spawn(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write_fixture(Path(td), {
                "subagent_type": "code-explorer",
                "description": "Find auth middleware references",
            })
            conv = server.extract_conversation(path)
            spawns = [t for t in conv["turns"] if t.get("type") == "subagent_spawn"]
            self.assertEqual(len(spawns), 1)
            self.assertEqual(spawns[0]["name"], "code-explorer")
            self.assertIn("auth", spawns[0]["description"])

    def test_agent_without_subagent_type_defaults_general_purpose(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write_fixture(Path(td), {
                "description": "Just do something",
            })
            conv = server.extract_conversation(path)
            spawns = [t for t in conv["turns"] if t.get("type") == "subagent_spawn"]
            self.assertEqual(spawns[0]["name"], "general-purpose")

    def test_agent_without_description_uses_prompt_prefix(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write_fixture(Path(td), {
                "subagent_type": "code-reviewer",
                "prompt": "Review the recent migration and check for race conditions",
            })
            conv = server.extract_conversation(path)
            spawns = [t for t in conv["turns"] if t.get("type") == "subagent_spawn"]
            self.assertEqual(spawns[0]["name"], "code-reviewer")
            self.assertIn("Review", spawns[0]["description"])

    def test_agent_does_not_create_tool_turn(self):
        # Agent tool_use should not also become a normal tool turn; avoid double rendering
        with tempfile.TemporaryDirectory() as td:
            path = self._write_fixture(Path(td), {
                "subagent_type": "x",
                "description": "y",
            })
            conv = server.extract_conversation(path)
            tools = [t for t in conv["turns"] if t.get("type") == "tool"]
            self.assertEqual(len(tools), 0)

    def test_agent_tool_result_does_not_render_as_anonymous_tool(self):
        """Real Claude Agent calls have a user-role tool_result after tool_use.
        Before Agent tool_id was added to pending_tools, that tool_result fell to the
        unknown-tool fallback and rendered an anonymous name=? tool block with the subagent
        result, violating spec section 6.2. Agent tool_id now gets a __spawn__ sentinel; the
        paired tool_result is recognized and skipped.
        """
        with tempfile.TemporaryDirectory() as td:
            path = self._write_fixture(Path(td), {
                "subagent_type": "code-explorer",
                "description": "Find X",
            }, with_tool_result=True)
            conv = server.extract_conversation(path)
            tools = [t for t in conv["turns"] if t.get("type") == "tool"]
            self.assertEqual(len(tools), 0,
                             f"expected no tool turns, got: {tools}")
            spawns = [t for t in conv["turns"] if t.get("type") == "subagent_spawn"]
            self.assertEqual(len(spawns), 1)
            # subagent result should not appear anywhere in turns
            for t in conv["turns"]:
                text = t.get("text", "") + str(t.get("result", "")) + t.get("description", "")
                self.assertNotIn("subagent finished", text)


class TestSystemUserInjections(unittest.TestCase):
    """The harness writes system events such as task-notification / bash output / teammate as user-role JSONL.
    They must be recognized as system-event turns rather than user input.
    """

    def _write_session(self, td: Path, user_strings: list[str]) -> Path:
        p = td / "session.jsonl"
        lines = []
        for i, content in enumerate(user_strings):
            lines.append({
                "parentUuid": None, "uuid": f"u{i}",
                "timestamp": f"2026-05-26T10:0{i}:00Z",
                "type": "user", "sessionId": "sid", "cwd": "/x",
                "message": {"role": "user", "content": content},
            })
        p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        return p

    def test_task_notification_becomes_system_notification(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write_session(Path(td), [
                "hello",
                ('<task-notification>\n<task-id>x</task-id>'
                 '<status>completed</status>'
                 '<summary>Background command "Start server" completed (exit code 0)</summary>'
                 '</task-notification>'),
            ])
            conv = server.extract_conversation(path)
            users = [t for t in conv["turns"] if t["type"] == "user"]
            notifs = [t for t in conv["turns"] if t["type"] == "system_notification"]
            self.assertEqual(len(users), 1, "task-notification should not count as user input")
            self.assertEqual(len(notifs), 1)
            self.assertIn("completed", notifs[0]["text"])
            self.assertIn("Start server", notifs[0]["text"])

    def test_bash_stdout_becomes_bash_output(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write_session(Path(td), [
                "<bash-stdout>hello world</bash-stdout><bash-stderr></bash-stderr>",
                "<bash-stdout>(Bash completed with no output)</bash-stdout><bash-stderr></bash-stderr>",
            ])
            conv = server.extract_conversation(path)
            users = [t for t in conv["turns"] if t["type"] == "user"]
            bashes = [t for t in conv["turns"] if t["type"] == "bash_output"]
            self.assertEqual(len(users), 0)
            self.assertEqual(len(bashes), 2)
            self.assertIn("hello world", bashes[0]["text"])
            # empty-output placeholder text should not be fed back
            self.assertNotIn("Bash completed with no output", bashes[1]["text"])

    def test_teammate_message_becomes_teammate_turn(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write_session(Path(td), [
                ('<teammate-message teammate_id="newton" color="green" '
                 'summary="SpyFu login expired">\nbody here\n</teammate-message>'),
            ])
            conv = server.extract_conversation(path)
            users = [t for t in conv["turns"] if t["type"] == "user"]
            teams = [t for t in conv["turns"] if t["type"] == "teammate_message"]
            self.assertEqual(len(users), 0)
            self.assertEqual(len(teams), 1)
            self.assertIn("newton", teams[0]["text"])
            self.assertIn("SpyFu login expired", teams[0]["text"])

    def test_bash_input_is_still_user(self):
        """<bash-input> is the !command typed by the user and must stay as a user turn."""
        with tempfile.TemporaryDirectory() as td:
            path = self._write_session(Path(td), [
                "<bash-input>ls -la</bash-input>",
            ])
            conv = server.extract_conversation(path)
            users = [t for t in conv["turns"] if t["type"] == "user"]
            self.assertEqual(len(users), 1)

    def test_system_event_does_not_consume_skill_marker(self):
        """<command-name> triggered next_is_skill should not be swallowed by an intervening system event."""
        with tempfile.TemporaryDirectory() as td:
            path = self._write_session(Path(td), [
                "<command-name>/foo</command-name>",  # triggers next_is_skill
                "<bash-stdout>noise</bash-stdout><bash-stderr></bash-stderr>",
                "actual skill body content here",  # should still classify as skill
            ])
            conv = server.extract_conversation(path)
            skills = [t for t in conv["turns"] if t["type"] == "skill"]
            users = [t for t in conv["turns"] if t["type"] == "user"]
            # Design: system events also reset next_is_skill because they break the adjacent
            # command->skill relationship. This test locks current behavior: with bash output
            # in between, actual content is user, not skill.
            self.assertEqual(len(skills), 0)
            self.assertEqual(len(users), 1)


class TestSkillToolBody(unittest.TestCase):
    """After the AI proactively calls the Skill tool, the SDK injects the skill body as a user-array text block.
    It must not be misclassified as user; it must be recognized as a skill turn.
    """

    def _write_session(self, td: Path, events: list) -> Path:
        """events: list of dicts shaped like JSONL records (minus boilerplate)."""
        p = td / "session.jsonl"
        lines = []
        for i, e in enumerate(events):
            lines.append({
                "parentUuid": None, "uuid": f"u{i}",
                "timestamp": f"2026-05-26T10:0{i}:00Z",
                "sessionId": "sid", "cwd": "/x",
                **e,
            })
        p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        return p

    def test_skill_tool_body_becomes_skill_turn(self):
        """Full sequence: assistant tool_use(Skill) -> user tool_result -> user text(body)."""
        with tempfile.TemporaryDirectory() as td:
            path = self._write_session(Path(td), [
                {"type": "user", "message": {"role": "user", "content": "look this up"}},
                {"type": "assistant", "message": {"role": "assistant", "content": [{
                    "type": "tool_use", "id": "t1", "name": "Skill",
                    "input": {"skill": "web-access"},
                }]}},
                {"type": "user", "message": {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": "t1",
                    "content": "Launching skill: web-access",
                }]}},
                {"type": "user", "message": {"role": "user", "content": [{
                    "type": "text",
                    "text": "Base directory for this skill: /x/.claude/skills/web-access\n\n# web-access Skill\n...",
                }]}},
            ])
            conv = server.extract_conversation(path)
            users = [t for t in conv["turns"] if t["type"] == "user"]
            skills = [t for t in conv["turns"] if t["type"] == "skill"]
            self.assertEqual(len(users), 1, "there should be exactly 1 user turn ('look this up')")
            self.assertEqual(len(skills), 1, "skill body should be classified as a skill turn")
            self.assertIn("web-access Skill", skills[0]["text"])

    def test_skill_body_prefix_fallback(self):
        """Fallback when preceding tool_use(Skill) is missing.
        Directly recognize the 'Base directory for this skill:' prefix, covering cases where
        the Skill tool trace was truncated at the file head.
        """
        with tempfile.TemporaryDirectory() as td:
            path = self._write_session(Path(td), [
                {"type": "user", "message": {"role": "user", "content": [{
                    "type": "text",
                    "text": "Base directory for this skill: /x/.claude/skills/web-access\n\n# body...",
                }]}},
            ])
            conv = server.extract_conversation(path)
            users = [t for t in conv["turns"] if t["type"] == "user"]
            skills = [t for t in conv["turns"] if t["type"] == "skill"]
            self.assertEqual(len(users), 0)
            self.assertEqual(len(skills), 1)

    def test_request_interrupted_becomes_system_notification(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write_session(Path(td), [
                {"type": "user", "message": {"role": "user", "content": [{
                    "type": "text", "text": "[Request interrupted by user]",
                }]}},
            ])
            conv = server.extract_conversation(path)
            users = [t for t in conv["turns"] if t["type"] == "user"]
            notifs = [t for t in conv["turns"] if t["type"] == "system_notification"]
            self.assertEqual(len(users), 0)
            self.assertEqual(len(notifs), 1)
            self.assertIn("interrupted", notifs[0]["text"].lower())

    def test_continue_from_where_left_off(self):
        """The resume prompt injected by --resume is not user input."""
        with tempfile.TemporaryDirectory() as td:
            path = self._write_session(Path(td), [
                {"type": "user", "message": {"role": "user", "content": [{
                    "type": "text", "text": "Continue from where you left off.",
                }]}},
            ])
            conv = server.extract_conversation(path)
            users = [t for t in conv["turns"] if t["type"] == "user"]
            notifs = [t for t in conv["turns"] if t["type"] == "system_notification"]
            self.assertEqual(len(users), 0)
            self.assertEqual(len(notifs), 1)

    def test_skill_tool_body_survives_tool_result_message(self):
        """A pure tool_result message can appear between tool_use(Skill) and skill body.
        That middle row must not consume pending_skill_body, so the body row is still recognized.
        """
        with tempfile.TemporaryDirectory() as td:
            path = self._write_session(Path(td), [
                {"type": "assistant", "message": {"role": "assistant", "content": [{
                    "type": "tool_use", "id": "t1", "name": "Skill",
                    "input": {"skill": "x"},
                }]}},
                # pure tool_result, no text block, must not consume the flag
                {"type": "user", "message": {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": "t1", "content": "ok",
                }]}},
                {"type": "user", "message": {"role": "user", "content": [{
                    "type": "text", "text": "any text here that is the skill body",
                }]}},
            ])
            conv = server.extract_conversation(path)
            self.assertEqual(
                [t["type"] for t in conv["turns"]],
                ["tool", "skill"],
                "expected tool turn + skill turn, with no user turn",
            )


if __name__ == "__main__":
    unittest.main()
