"""scripts/release_flow.py against a throwaway git remote.

Builds a bare "origin" plus a clone with `main` and `staging`, then drives the real commands
(`check`, `deploy`, `release --dry-run`) over it. Deploy tags are back-dated through
GIT_COMMITTER_DATE so the soak arithmetic can be exercised without waiting two weeks.
"""
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "release_flow.py"
spec = importlib.util.spec_from_file_location("release_flow", SCRIPT)
rf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rf)

ENV = {**os.environ, "GIT_AUTHOR_NAME": "Alice", "GIT_AUTHOR_EMAIL": "alice@example.com",
       "GIT_COMMITTER_NAME": "Alice", "GIT_COMMITTER_EMAIL": "alice@example.com"}


def sh(args, cwd, env=None):
    r = subprocess.run(args, cwd=cwd, env=env or ENV, capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"{' '.join(args)}: {r.stderr}")
    return r.stdout.strip()


class _OK(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


class ReleaseFlowTests(unittest.TestCase):
    def setUp(self):
        # The script's own git calls inherit the process environment; CI runners have no git
        # identity configured, and `git tag -a` refuses to write a tag without one.
        self._env = mock.patch.dict(os.environ, {k: v for k, v in ENV.items() if k.startswith("GIT_")})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.origin = root / "origin.git"
        self.clone = root / "clone"
        sh(["git", "init", "--bare", "--quiet", "-b", "main", str(self.origin)], cwd=root)
        sh(["git", "clone", "--quiet", str(self.origin), str(self.clone)], cwd=root)
        self.commit("base", "initial")
        sh(["git", "push", "--quiet", "origin", "main"], cwd=self.clone)
        sh(["git", "checkout", "--quiet", "-b", "staging"], cwd=self.clone)
        sh(["git", "push", "--quiet", "-u", "origin", "staging"], cwd=self.clone)
        (self.clone / "_private").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, name, subject):
        (self.clone / f"{name}.txt").write_text(subject + "\n")
        sh(["git", "add", "."], cwd=self.clone)
        sh(["git", "commit", "--quiet", "-m", subject], cwd=self.clone)
        return sh(["git", "rev-parse", "HEAD"], cwd=self.clone)

    def push_staging(self):
        sh(["git", "push", "--quiet", "origin", "staging"], cwd=self.clone)

    def deployed_tag(self, commit, days_ago, label="a"):
        when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        tag = f"deployed/{when[:10]}-{label}"
        sh(["git", "tag", "-a", tag, commit, "-m", "deployed"], cwd=self.clone,
           env={**ENV, "GIT_COMMITTER_DATE": when})
        sh(["git", "push", "--quiet", "origin", tag], cwd=self.clone)
        return tag

    def check(self, *extra):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = rf.main(["--repo", str(self.clone), "check", *extra])
        self.assertEqual(code, 0)
        return buf.getvalue()

    # ---- releasable() arithmetic ----
    def test_releasable_picks_newest_soaked_deploy(self):
        now = datetime(2026, 9, 20, tzinfo=timezone.utc)
        d = lambda days, c: rf.Deployed(f"t{days}", c, now - timedelta(days=days))
        self.assertIsNone(rf.releasable([d(3, "c3")], now))
        self.assertEqual(rf.releasable([d(30, "c30"), d(15, "c15"), d(3, "c3")], now).commit, "c15")
        self.assertEqual(rf.releasable([d(14, "c14")], now).commit, "c14")  # threshold is inclusive
        self.assertIsNone(rf.releasable([], now))

    # ---- check ----
    def test_check_reports_never_deployed(self):
        self.commit("f1", "feature one")
        self.push_staging()
        out = self.check()
        self.assertIn("never been deployed", out)
        self.assertNotIn("READY", out)

    def test_check_reports_ready_and_pending(self):
        f1 = self.commit("f1", "feature one")
        self.commit("f2", "feature two")
        self.push_staging()
        self.deployed_tag(f1, days_ago=20)
        out = self.check()
        self.assertIn("READY", out)
        self.assertIn("feature one", out)
        self.assertNotIn("    " + "feature two", out)  # f2 is not part of the promotion
        self.assertIn("1 commit(s) not yet deployed", out)

    def test_check_quiet_when_nothing_actionable(self):
        f1 = self.commit("f1", "feature one")
        self.push_staging()
        self.deployed_tag(f1, days_ago=3)
        self.assertIn("nothing has soaked", self.check())
        self.assertEqual(self.check("--quiet"), "")

    def test_check_knows_when_main_already_has_it(self):
        f1 = self.commit("f1", "feature one")
        self.push_staging()
        self.deployed_tag(f1, days_ago=20)
        sh(["git", "push", "--quiet", "origin", "staging:main"], cwd=self.clone)
        out = self.check()
        self.assertIn("already contains", out)
        self.assertNotIn("READY", out)

    def test_check_warns_when_main_bypassed_staging(self):
        sh(["git", "checkout", "--quiet", "main"], cwd=self.clone)
        self.commit("hot", "hotfix straight to main")
        sh(["git", "push", "--quiet", "origin", "main"], cwd=self.clone)
        sh(["git", "checkout", "--quiet", "staging"], cwd=self.clone)
        self.assertIn("not on origin/staging", self.check())

    def test_check_accepts_release_merge_commit_on_main(self):
        # A promotion lands on main as a merge commit (main is branch-protected, so the release
        # branch cannot be fast-forwarded). That commit is not on staging, but its content is.
        f1 = self.commit("f1", "feature one")
        self.commit("f2", "feature two")
        self.push_staging()
        self.deployed_tag(f1, days_ago=20)
        sh(["git", "checkout", "--quiet", "main"], cwd=self.clone)
        sh(["git", "merge", "--quiet", "--no-ff", "-m", "Merge release", f1], cwd=self.clone)
        sh(["git", "push", "--quiet", "origin", "main"], cwd=self.clone)
        sh(["git", "checkout", "--quiet", "staging"], cwd=self.clone)
        out = self.check()
        self.assertNotIn("WARNING", out)
        self.assertIn("already contains", out)

    # ---- deploy ----
    def _serve(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _OK)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{httpd.server_address[1]}/api/stats"

    def _config(self, restart, url, timeout=None):
        cfg = {"restart": restart, "health_url": url}
        if timeout is not None:
            cfg["health_timeout_s"] = timeout
        (self.clone / "_private" / "deploy.json").write_text(json.dumps(cfg))
        sh(["git", "checkout", "--quiet", "--", "."], cwd=self.clone)
        # _private is untracked in this throwaway repo; ignore it so the clean-tree check passes
        (self.clone / ".git" / "info" / "exclude").write_text("_private/\n")

    def test_deploy_fast_forwards_restarts_and_tags(self):
        self.commit("f1", "feature one")
        self.push_staging()
        sh(["git", "reset", "--quiet", "--hard", "HEAD~1"], cwd=self.clone)  # local checkout lags origin
        marker = self.clone / "_private" / "restarted"
        self._config(["sh", "-c", f"touch {marker}"], self._serve())
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = rf.main(["--repo", str(self.clone), "deploy"])
        self.assertEqual(code, 0)
        self.assertTrue(marker.exists(), "restart command ran")
        head = sh(["git", "rev-parse", "HEAD"], cwd=self.clone)
        self.assertEqual(head, sh(["git", "rev-parse", "origin/staging"], cwd=self.clone))
        tags = sh(["git", "ls-remote", "--tags", "origin"], cwd=self.clone)
        self.assertIn("refs/tags/deployed/", tags)
        self.assertEqual(len(rf.list_deployed(self.clone)), 1)
        # Deploying the same commit again restarts but does not add a second clock
        with redirect_stdout(io.StringIO()):
            rf.main(["--repo", str(self.clone), "deploy"])
        self.assertEqual(len(rf.list_deployed(self.clone)), 1)

    def test_deploy_refuses_when_service_stays_down(self):
        self.commit("f1", "feature one")
        self.push_staging()
        # A tiny configured limit keeps the test instant; the refusal path is the same one.
        self._config(["true"], "http://127.0.0.1:9/never", timeout=0.01)
        with self.assertRaises(SystemExit) as raised, redirect_stdout(io.StringIO()):
            rf.main(["--repo", str(self.clone), "deploy"])
        self.assertEqual(rf.list_deployed(self.clone), [])
        # The refusal has to name the usual cause and say that repeating deploy is safe,
        # or the reader concludes the deploy itself broke.
        message = str(raised.exception)
        self.assertIn("scan-cache schema", message)
        self.assertIn("run `deploy` again", message)
        self.assertIn("health_timeout_s", message)

    def test_the_health_wait_is_long_enough_for_a_cold_rescan(self):
        # A schema bump makes the restarted service re-read the whole library before it answers;
        # that took between 60 and 120 s on the maintainer's history and used to be refused at 30.
        self.assertGreaterEqual(rf.HEALTH_TIMEOUT_S, 120.0)

    def test_the_deploy_config_can_override_the_health_wait(self):
        self._config(["true"], "http://127.0.0.1:9/never")
        self.assertEqual(rf.load_config(self.clone, None)["health_timeout_s"], rf.HEALTH_TIMEOUT_S)
        self._config(["true"], "http://127.0.0.1:9/never", timeout=45)
        self.assertEqual(rf.load_config(self.clone, None)["health_timeout_s"], 45.0)
        for bad in (0, -1, "soon", True):
            self._config(["true"], "http://127.0.0.1:9/never", timeout=bad)
            with self.assertRaises(SystemExit):
                rf.load_config(self.clone, None)
        # An explicit null is a malformed value, not an omission: it must not read as the default.
        (self.clone / "_private" / "deploy.json").write_text(json.dumps(
            {"restart": ["true"], "health_url": "http://127.0.0.1:9/never",
             "health_timeout_s": None}))
        with self.assertRaises(SystemExit):
            rf.load_config(self.clone, None)

    def test_deploy_waits_for_the_configured_limit(self):
        self.commit("f1", "feature one")
        self.push_staging()
        self._config(["true"], "http://127.0.0.1:9/never", timeout=45)
        seen = []
        with mock.patch.object(rf, "wait_healthy",
                               lambda url, timeout_s=None: seen.append(timeout_s) or True):
            with redirect_stdout(io.StringIO()):
                rf.main(["--repo", str(self.clone), "deploy"])
        self.assertEqual(seen, [45.0])

    def test_the_health_wait_stops_at_its_deadline(self):
        started = time.monotonic()
        self.assertFalse(rf.wait_healthy("http://127.0.0.1:9/never", 0.05))
        # The poll used to sleep a whole second after the deadline had passed.
        self.assertLess(time.monotonic() - started, 0.9)

    def test_deploy_refuses_dirty_tree(self):
        self._config(["true"], "http://127.0.0.1:9/never")
        (self.clone / "base.txt").write_text("edited\n")
        with self.assertRaises(SystemExit):
            rf.main(["--repo", str(self.clone), "deploy"])

    # ---- release ----
    def test_release_dry_run_prepares_branch_and_pr(self):
        f1 = self.commit("f1", "feature one")
        self.commit("f2", "feature two")
        self.push_staging()
        self.deployed_tag(f1, days_ago=20)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = rf.main(["--repo", str(self.clone), "release", "--dry-run"])
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("- ", out)
        self.assertIn("feature one", out)
        self.assertNotIn("feature two", out)
        branches = sh(["git", "ls-remote", "--heads", "origin", "release/*"], cwd=self.clone)
        self.assertIn(f1, branches)

    def test_release_refuses_when_nothing_soaked(self):
        f1 = self.commit("f1", "feature one")
        self.push_staging()
        self.deployed_tag(f1, days_ago=2)
        with self.assertRaises(SystemExit):
            rf.main(["--repo", str(self.clone), "release", "--dry-run"])


if __name__ == "__main__":
    unittest.main()
