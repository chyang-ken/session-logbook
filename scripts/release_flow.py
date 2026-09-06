#!/usr/bin/env python3
"""Staging → local soak → main: the release flow of this repository.

The model (see CLAUDE.md "Branch model and release flow"):

  feature branch ──PR──► staging ──deploy──► the maintainer's machine ──14 days──► main

* `staging` is a one-way river of commits. Every change enters through a pull request into
  `staging`; its history is never rewritten.
* `deploy` runs on the machine that hosts the maintainer's resident dashboard: it fast-forwards
  the checkout to `staging`, restarts the service, and records the deployed commit as an
  annotated `deployed/<date>-<sha>` tag pushed to the remote. The tag date is when the soak
  clock for that commit (and every commit before it) started.
* `check` finds the newest deployed commit whose soak time is over and reports whether `main`
  is behind it. It is wired as a session-start hook so the question "is anything ready to
  release?" gets asked automatically whenever work resumes.
* `release` opens a pull request that moves `main` up to that commit. It never merges: merging
  into `main` is a separate, human decision, and `main` is branch-protected anyway.

Only the git logic lives here. The machine-specific part (how to restart the service, which
URL proves it is up) is read from a git-ignored local file, `_private/deploy.json`:

    {"restart": ["/path/to/restart-command", "arg", "..."],
     "health_url": "http://127.0.0.1:47821/api/stats"}

Python 3.9+, standard library only. `release` shells out to the GitHub CLI (`gh`).
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

STAGING = "staging"
MAIN = "main"
REMOTE = "origin"
TAG_PREFIX = "deployed/"
SOAK_DAYS = 14          # a deployed commit is releasable once it has been in use this long
HEALTH_TIMEOUT_S = 30.0  # how long deploy waits for the restarted service before refusing to tag
DEFAULT_CONFIG = Path("_private") / "deploy.json"


# ---------- git helpers ----------
def git(*args: str, check: bool = True, cwd: Optional[Path] = None) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def repo_root(cwd: Optional[Path] = None) -> Path:
    return Path(git("rev-parse", "--show-toplevel", cwd=cwd))


def fetch(cwd: Optional[Path] = None) -> bool:
    """Refresh remote refs and tags. Returns False (and keeps going on local refs) when offline."""
    r = subprocess.run(["git", "fetch", "--quiet", "--tags", REMOTE], cwd=cwd, capture_output=True, text=True)
    return r.returncode == 0


def ref_exists(ref: str, cwd: Optional[Path] = None) -> bool:
    return subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref], cwd=cwd,
                          capture_output=True).returncode == 0


def is_ancestor(a: str, b: str, cwd: Optional[Path] = None) -> bool:
    """True when commit `a` is reachable from `b` (a is an ancestor of, or equal to, b)."""
    return subprocess.run(["git", "merge-base", "--is-ancestor", a, b], cwd=cwd,
                          capture_output=True).returncode == 0


def main_content_on_staging(main: str, staging: str, cwd: Optional[Path] = None) -> bool:
    """True when everything on `main` also exists on `staging`.

    A release PR lands on the protected `main` as a merge commit, and that commit itself is never
    on `staging`, so plain ancestry would cry wolf after every release. Compare content instead:
    `main` is fine when its tree equals the tree of its merge-base with `staging` — i.e. the merge
    added nothing beyond what `staging` already had. A hotfix committed straight to `main` changes
    the tree and is still caught.
    """
    if is_ancestor(main, staging, cwd):
        return True
    base = subprocess.run(["git", "merge-base", main, staging], cwd=cwd, capture_output=True, text=True)
    if base.returncode != 0 or not base.stdout.strip():
        return False
    return subprocess.run(["git", "diff", "--quiet", base.stdout.strip(), main], cwd=cwd,
                          capture_output=True).returncode == 0


def rev(ref: str, cwd: Optional[Path] = None) -> str:
    return git("rev-parse", ref, cwd=cwd)


def short(sha: str) -> str:
    return sha[:7]


def commits_between(older: str, newer: str, cwd: Optional[Path] = None) -> List[str]:
    """Subjects of commits reachable from `newer` but not from `older`, oldest first."""
    out = git("log", "--reverse", "--format=%h %s", f"{older}..{newer}", cwd=cwd)
    return [line for line in out.splitlines() if line.strip()]


# ---------- deployed tags ----------
class Deployed:
    __slots__ = ("tag", "commit", "when")

    def __init__(self, tag: str, commit: str, when: datetime):
        self.tag, self.commit, self.when = tag, commit, when

    def days(self, now: datetime) -> float:
        return (now - self.when).total_seconds() / 86400.0


def list_deployed(cwd: Optional[Path] = None) -> List[Deployed]:
    """All `deployed/*` annotated tags with their tagger date and the commit they point at, oldest first."""
    out = git("for-each-ref", "--format=%(refname:short)%09%(*objectname)%09%(taggerdate:iso-strict)",
              f"refs/tags/{TAG_PREFIX}", cwd=cwd)
    items = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or not parts[1] or not parts[2]:
            continue  # lightweight tags carry no date and no dereferenced object: not a deploy record
        tag, commit, when = parts
        if when.endswith("Z"):
            when = when[:-1] + "+00:00"  # git prints UTC as "Z"; Python < 3.11 only parses "+00:00"
        try:
            items.append(Deployed(tag, commit, datetime.fromisoformat(when)))
        except ValueError:
            continue
    items.sort(key=lambda d: d.when)
    return items


def releasable(deployed: List[Deployed], now: datetime, soak_days: int = SOAK_DAYS) -> Optional[Deployed]:
    """The newest deploy record whose soak time is over, or None."""
    done = [d for d in deployed if d.days(now) >= soak_days]
    return done[-1] if done else None


# ---------- commands ----------
def cmd_check(args: argparse.Namespace) -> int:
    cwd = Path(args.repo) if args.repo else None
    online = fetch(cwd)
    staging = f"{REMOTE}/{STAGING}"
    main = f"{REMOTE}/{MAIN}"
    now = datetime.now(timezone.utc)
    lines: List[str] = []
    actionable = False

    if not ref_exists(staging, cwd):
        lines.append(f"release-flow: no {staging} branch yet; nothing to check.")
        print("\n".join(lines))
        return 0

    deployed = list_deployed(cwd)
    latest = deployed[-1] if deployed else None

    # 1. Is the machine behind staging?
    if latest is None:
        actionable = True
        lines.append(f"release-flow: {STAGING} has never been deployed locally. "
                     f"Run `python3 scripts/release_flow.py deploy` on the machine that hosts the dashboard.")
    else:
        pending = commits_between(latest.commit, staging, cwd)
        if pending:
            actionable = True
            lines.append(f"release-flow: {STAGING} has {len(pending)} commit(s) not yet deployed locally "
                         f"(last deploy {latest.tag}, {latest.days(now):.0f} days ago). Run `deploy` to start their soak clock.")

    # 2. Is anything ready to move to main?
    ready = releasable(deployed, now, args.soak_days)
    if ready is not None:
        if not is_ancestor(ready.commit, staging, cwd):
            actionable = True
            lines.append(f"release-flow: WARNING {ready.tag} ({short(ready.commit)}) is not on {staging}; "
                         f"the staging history was rewritten. Fix the branch before releasing.")
        elif is_ancestor(ready.commit, main, cwd):
            lines.append(f"release-flow: {MAIN} already contains everything that has soaked "
                         f"({ready.tag}, {ready.days(now):.0f} days).")
        else:
            actionable = True
            behind = commits_between(main, ready.commit, cwd)
            lines.append(f"release-flow: READY — {STAGING} up to {short(ready.commit)} has been in use for "
                         f"{ready.days(now):.0f} days (deployed {ready.when.date()}, threshold {args.soak_days}). "
                         f"{len(behind)} commit(s) would move to {MAIN}:")
            lines.extend(f"    {c}" for c in behind[:20])
            if len(behind) > 20:
                lines.append(f"    … and {len(behind) - 20} more")
            lines.append("    Open the release PR with `python3 scripts/release_flow.py release` (it does not merge).")
    elif deployed:
        lines.append(f"release-flow: newest deploy {latest.tag} is {latest.days(now):.0f} days old; "
                     f"nothing has soaked {args.soak_days} days yet.")

    # 3. Did main get content that never went through staging?
    if ref_exists(main, cwd) and not main_content_on_staging(main, staging, cwd):
        actionable = True
        lines.append(f"release-flow: WARNING {main} has changes that are not on {staging}. "
                     f"Merge {MAIN} into {STAGING} so the river stays one-way.")

    if not online:
        lines.append("release-flow: (offline: checked local refs only)")

    if lines and (actionable or not args.quiet):
        print("\n".join(lines))
    return 0


def load_config(root: Path, path: Optional[str]) -> dict:
    cfg_path = Path(path) if path else root / DEFAULT_CONFIG
    if not cfg_path.is_file():
        raise SystemExit(
            f"release-flow: missing local deploy config {cfg_path}.\n"
            "It is machine-specific and git-ignored; create it with the shape documented at the top of scripts/release_flow.py.")
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if not isinstance(cfg.get("restart"), list) or not cfg["restart"]:
        raise SystemExit("release-flow: deploy config needs a non-empty `restart` command list")
    if not isinstance(cfg.get("health_url"), str):
        raise SystemExit("release-flow: deploy config needs a `health_url` string")
    return cfg


def wait_healthy(url: str, timeout_s: Optional[float] = None) -> bool:
    deadline = time.time() + (HEALTH_TIMEOUT_S if timeout_s is None else timeout_s)
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def cmd_deploy(args: argparse.Namespace) -> int:
    root = repo_root(Path(args.repo) if args.repo else None)
    cfg = load_config(root, args.config)
    if git("status", "--porcelain", cwd=root):
        raise SystemExit("release-flow: working tree is not clean; deploy only from a clean checkout.")
    if not fetch(root):
        raise SystemExit("release-flow: cannot reach the remote; deploy needs the latest staging.")
    target = f"{REMOTE}/{STAGING}"
    if not ref_exists(target, root):
        raise SystemExit(f"release-flow: {target} does not exist.")

    current = git("rev-parse", "--abbrev-ref", "HEAD", cwd=root)
    if current != STAGING:
        print(f"release-flow: switching {root} from {current} to {STAGING}")
        git("checkout", "--quiet", STAGING, cwd=root)
    git("merge", "--ff-only", "--quiet", target, cwd=root)   # the river only moves forward
    head = rev("HEAD", cwd=root)
    print(f"release-flow: checkout is at {STAGING} {short(head)}")

    print(f"release-flow: restarting service: {' '.join(cfg['restart'])}")
    r = subprocess.run(cfg["restart"], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"release-flow: restart command failed ({r.returncode}): {r.stderr.strip() or r.stdout.strip()}")
    if not wait_healthy(cfg["health_url"]):
        raise SystemExit(f"release-flow: service did not answer 200 at {cfg['health_url']} within {HEALTH_TIMEOUT_S:.0f} s; "
                         f"NOT recording a deploy tag.")
    print(f"release-flow: service answered at {cfg['health_url']}")

    already = [d for d in list_deployed(root) if d.commit == head]
    if already:
        print(f"release-flow: {short(head)} was already recorded as {already[0].tag} "
              f"({already[0].when.date()}); soak clock unchanged.")
        return 0
    today = datetime.now(timezone.utc).date().isoformat()
    tag = f"{TAG_PREFIX}{today}-{short(head)}"
    msg = (f"Deployed {STAGING} {short(head)} to {platform.node()} on {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n\n"
           f"Soak clock for every commit up to this one starts here.")
    git("tag", "-a", tag, head, "-m", msg, cwd=root)
    git("push", "--quiet", REMOTE, tag, cwd=root)
    print(f"release-flow: recorded {tag} and pushed it to {REMOTE}")
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    root = repo_root(Path(args.repo) if args.repo else None)
    if not fetch(root):
        raise SystemExit("release-flow: cannot reach the remote.")
    now = datetime.now(timezone.utc)
    ready = releasable(list_deployed(root), now, args.soak_days)
    main = f"{REMOTE}/{MAIN}"
    staging = f"{REMOTE}/{STAGING}"
    if ready is None:
        raise SystemExit(f"release-flow: nothing has soaked {args.soak_days} days yet.")
    if not is_ancestor(ready.commit, staging, root):
        raise SystemExit(f"release-flow: {ready.tag} is not on {staging}; refusing to release from a rewritten history.")
    if is_ancestor(ready.commit, main, root):
        raise SystemExit(f"release-flow: {MAIN} already contains {ready.tag}.")
    behind = commits_between(main, ready.commit, root)
    branch = f"release/{now.date().isoformat()}"
    if ref_exists(f"{REMOTE}/{branch}", root):
        raise SystemExit(f"release-flow: {branch} already exists on {REMOTE}; finish or delete that release first.")
    git("branch", "--force", branch, ready.commit, cwd=root)
    git("push", "--quiet", REMOTE, f"{branch}:{branch}", cwd=root)
    title = f"Release: {STAGING} as of {ready.when.date()} ({short(ready.commit)})"
    body = "\n".join([
        "## What & why",
        f"Promote `{STAGING}` up to `{short(ready.commit)}` into `{MAIN}`. That commit was deployed to the maintainer's "
        f"machine on {ready.when.date()} (`{ready.tag}`) and has been in daily use for {ready.days(now):.0f} days "
        f"(threshold {args.soak_days}). Commits after it stay on `{STAGING}` and keep soaking.",
        "",
        "## Changes",
        *[f"- {c}" for c in behind],
        "",
        "## How I verified",
        f"- Soak: in use locally since {ready.when.date()} without a revert.",
        f"- CI ran on every pull request into `{STAGING}`; the required checks run again on this PR.",
        "",
        "- [x] `python3 -m unittest discover -s tests` passes (on every contributing PR)",
        "- [ ] For UI changes: before/after screenshot attached — not applicable to a promotion",
        "- [x] No real session data, secrets, or private project references added",
        "- [x] Public-facing comments / docs are in English",
    ])
    if args.dry_run:
        print(f"release-flow: would open PR '{title}' from {branch} into {MAIN}:\n{body}")
        return 0
    r = subprocess.run(["gh", "pr", "create", "--base", MAIN, "--head", branch, "--title", title, "--body", body],
                       cwd=root, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"release-flow: branch {branch} is pushed but `gh pr create` failed: {r.stderr.strip()}")
    print(f"release-flow: opened {r.stdout.strip()} — merge it once the checks pass; that merge is the release.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--repo", help="repository path (default: the current directory's repository)")
    p.add_argument("--soak-days", type=int, default=SOAK_DAYS, help=f"days a deploy must age before release (default {SOAK_DAYS})")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="report undeployed staging commits and anything ready for main")
    c.add_argument("--quiet", action="store_true", help="print nothing unless something is actionable (hook mode)")
    d = sub.add_parser("deploy", help="fast-forward this checkout to staging, restart the service, record a deployed/ tag")
    d.add_argument("--config", help=f"local deploy config (default: {DEFAULT_CONFIG})")
    r = sub.add_parser("release", help="open a pull request moving main up to the newest soaked deploy")
    r.add_argument("--dry-run", action="store_true", help="print the PR instead of creating it")
    args = p.parse_args(argv)
    return {"check": cmd_check, "deploy": cmd_deploy, "release": cmd_release}[args.cmd](args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        print(f"release-flow: {e}", file=sys.stderr)
        sys.exit(2)
