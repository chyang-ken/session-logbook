#!/usr/bin/env python3
"""Run the repository's Agent CLI from an installed or symlinked Skill."""

from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from session_logbook_cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
