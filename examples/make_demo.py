#!/usr/bin/env python3
"""Generate a self-contained demo home so you can try Session Logbook without
touching your real ~/.claude data.

It writes synthetic Claude Code sessions into ./demo-home/.claude/projects/ and
sets file mtimes so the four zones (Recent / Dusty / Starred / Archived) are all
populated. Everything here is fake — no real session content.

Usage:
    python3 examples/make_demo.py
    HOME="$(pwd)/examples/demo-home" python3 server.py   # → http://127.0.0.1:47821
"""
import json
import os
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEMO_HOME = HERE / "demo-home"
PROJECTS = DEMO_HOME / ".claude" / "projects"

NOW = time.time()
DAY = 86400


def folder_key(cwd: str) -> str:
    return cwd.replace("/", "-")


def write_session(cwd, session_id, turns, age_days):
    """turns: list of (role, text). role in {'user','assistant'}."""
    proj_dir = PROJECTS / folder_key(cwd)
    proj_dir.mkdir(parents=True, exist_ok=True)
    path = proj_dir / f"{session_id}.jsonl"
    lines = []
    base_ts = NOW - age_days * DAY
    for i, (role, text) in enumerate(turns):
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(base_ts + i * 60))
        if role == "user":
            content = text
        else:
            content = [{"type": "text", "text": text}]
        lines.append(json.dumps({
            "type": role,
            "uuid": f"{session_id}-{i}",
            "parentUuid": f"{session_id}-{i-1}" if i else None,
            "sessionId": session_id,
            "cwd": cwd,
            "timestamp": ts,
            "isSidechain": False,
            "userType": "external",
            "message": {"role": role, "content": content},
        }, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    mtime = NOW - age_days * DAY
    os.utime(path, (mtime, mtime))
    return session_id


sessions = [
    ("/Users/alice/acme-web", "1a2b3c4d-0001-4001-8001-000000000001", [
        ("user", "Let's split the giant login handler into smaller functions."),
        ("assistant", "Good idea. I'll extract `validate_credentials`, `issue_session`, and `set_cookies` and keep the handler as a thin orchestrator."),
        ("user", "Yes, and add a test for the expired-token path."),
        ("assistant", "Done — extracted the three helpers and added `test_login_expired_token`. All 14 tests pass."),
    ], 0.1),
    ("/Users/alice/acme-web", "1a2b3c4d-0002-4002-8002-000000000002", [
        ("user", "The dashboard header overlaps the content on mobile. Can you fix the sticky positioning?"),
        ("assistant", "The header had `position: fixed` without reserving space. I switched it to `sticky` and added `scroll-margin-top` to the anchors."),
    ], 1.5),
    ("/Users/alice/data-pipeline", "5e6f7a8b-0003-4003-8003-000000000003", [
        ("user", "Profile the nightly ETL — it's taking 40 minutes and it used to be 10."),
        ("assistant", "The regression is a per-row JSON parse inside the join loop. Hoisting the parse out and batching the upsert brings it back to ~9 minutes."),
        ("user", "Ship it."),
        ("assistant", "Merged. Added a benchmark so we catch this kind of regression in CI."),
    ], 0.4),
    ("/Users/alice/data-pipeline", "5e6f7a8b-0004-4004-8004-000000000004", [
        ("user", "Document the schema for the events table."),
        ("assistant", "Added `docs/schema/events.md` with column types, the partition key, and three example rows."),
    ], 12),  # dusty (>7d)
    ("/Users/alice/cli-tool", "9c0d1e2f-0005-4005-8005-000000000005", [
        ("user", "Add a --json flag to the status command so it's scriptable."),
        ("assistant", "Added `--json`; it emits the same fields as the table view through a single formatter so they can't drift apart."),
        ("user", "Nice. Tag a release."),
        ("assistant", "Tagged v1.4.0 and updated the changelog."),
    ], 3),  # will be starred
    ("/Users/alice/cli-tool", "9c0d1e2f-0006-4006-8006-000000000006", [
        ("user", "Migrate the config loader from YAML to TOML."),
        ("assistant", "Swapped to `tomllib`, kept a YAML shim behind a deprecation warning for one release, and updated the docs."),
    ], 9),  # dusty (>7d), not archived → shows the Dusty zone
]

for cwd, sid, turns, age in sessions:
    write_session(cwd, sid, turns, age)

# Pre-set some organizing state so the demo shows Starred + Archived zones too.
state = {
    "9c0d1e2f-0005-4005-8005-000000000005": {
        "starred": True, "starred_at": "2026-01-01T00:00:00+00:00",
        "archived": False, "archived_at": None, "note": "Reference for the --json formatter pattern.",
    },
    "5e6f7a8b-0004-4004-8004-000000000004": {
        "starred": False, "archived": True,
        "archived_at": "2026-01-01T00:00:00+00:00", "note": "",
    },
}
state_dir = DEMO_HOME / ".session-logbook"
state_dir.mkdir(parents=True, exist_ok=True)
(state_dir / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")

print(f"Demo home ready: {DEMO_HOME}")
print(f"  {len(sessions)} synthetic sessions across 3 projects")
print()
print("Run the dashboard against it (without touching your real ~/.claude):")
print(f'  HOME="{DEMO_HOME}" python3 server.py')
print("  open http://127.0.0.1:47821")
