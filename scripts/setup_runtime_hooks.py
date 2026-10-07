#!/usr/bin/env python3
"""Install only Logbook-owned hook entries; preserve other integrations."""

import argparse
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile

MARKER = "# session-logbook-runtime-v1"
EVENTS = {
    "claude": ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
               "PostToolUseFailure", "PermissionRequest", "Stop", "StopFailure",
               "SubagentStart", "SubagentStop", "SessionEnd"),
    "codex": ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
              "PermissionRequest", "Stop", "SubagentStart", "SubagentStop"),
}


def configured(data, source, install=True):
    data = json.loads(json.dumps(data))
    hooks = data.setdefault("hooks", {})
    for event, groups in list(hooks.items()):
        kept = []
        for group in groups:
            original = group.get("hooks", [])
            entries = [h for h in original if not h.get("command", "").endswith(MARKER)]
            if entries or not original:
                kept.append({**group, "hooks": entries})
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if install:
        script = Path(__file__).resolve().with_name("record_runtime_event.py")
        command = " ".join(map(shlex.quote, [sys.executable, str(script), "--source", source])) + " " + MARKER
        for event in EVENTS[source]:
            hooks.setdefault(event, []).append({"hooks": [
                {"type": "command", "command": command, "timeout": 5}]})
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preview", "install", "uninstall", "status"))
    parser.add_argument("--source", choices=EVENTS, required=True)
    parser.add_argument("--config", type=Path, help="explicit config path for isolated tests")
    args = parser.parse_args()
    path = args.config or Path.home() / (".claude/settings.json" if args.source == "claude" else ".codex/hooks.json")
    original = path.read_bytes() if path.exists() else b"{}"
    data = json.loads(original)
    if args.action == "status":
        installed = [e for e, groups in data.get("hooks", {}).items()
                     if any(h.get("command", "").endswith(MARKER)
                            for g in groups for h in g.get("hooks", []))]
        print(json.dumps({"config": str(path), "installed_events": installed,
                          "missing_events": sorted(set(EVENTS[args.source]) - set(installed)),
                          "runtime_delivery": "not_verified"}))
        return
    updated = configured(data, args.source, args.action != "uninstall")
    if args.action == "preview":
        # Only print our additions, never unrelated private configuration.
        print(json.dumps({"config": str(path), "events": list(EVENTS[args.source]),
                          "collector": str(Path(__file__).resolve().with_name("record_runtime_event.py"))}, indent=2))
        return
    if updated == data:
        print("unchanged")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != original:
        raise RuntimeError("configuration changed during installation; retry")
    fd, temporary = tempfile.mkstemp(prefix=".logbook-hooks-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(updated, out, indent=2)
            out.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(json.dumps({"config": str(path), "action": args.action}))


if __name__ == "__main__":
    main()
