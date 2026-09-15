#!/usr/bin/env python3
"""Observation-only hook: never emits approval decisions or blocks on failure."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sources.runtime_events import record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, choices=("claude", "codex", "kimi", "pi"))
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("hook payload exceeds 4 MiB")
        record(args.source, json.loads(raw))
    except Exception as exc:
        print("session-logbook event not recorded: " + type(exc).__name__, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
