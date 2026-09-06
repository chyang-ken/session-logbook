#!/usr/bin/env python3
"""Fail when tracked files contain CJK text marks outside an explicit allowlist.

This repository is English-first (see CONTRIBUTING.md): commit messages, code comments,
docstrings, and documentation are written in English, with `_zh-CN` companion files as the
one sanctioned exception. This script turns that rule into a check CI can enforce.

Run it over the whole repository:

    python3 scripts/check_no_cjk.py

Add an intentionally non-English file with --allow, or to DEFAULT_ALLOWED_PATHS when it is
a permanent companion document.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

DEFAULT_ALLOWED_PATHS = {
    "README_zh-CN.md",
}

BINARY_SUFFIXES = {
    ".avif",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".webp",
    ".woff",
    ".woff2",
}


def is_cjk_text_mark(ch: str) -> bool:
    return (
        "\u3000" <= ch <= "\u303f"  # CJK punctuation and ideographic space
        or "\u3400" <= ch <= "\u4dbf"  # CJK Unified Ideographs Extension A
        or "\u4e00" <= ch <= "\u9fff"  # CJK Unified Ideographs
        or "\uf900" <= ch <= "\ufaff"  # CJK Compatibility Ideographs
        or "\ufe30" <= ch <= "\ufe4f"  # CJK Compatibility Forms
        or "\uff00" <= ch <= "\uffef"  # Halfwidth and Fullwidth Forms
        or "\U00020000" <= ch <= "\U0003134f"  # CJK extensions B-H
    )


def has_cjk_text_mark(text: str) -> bool:
    return any(is_cjk_text_mark(ch) for ch in text)


def first_cjk_column(text: str) -> int:
    for idx, ch in enumerate(text, start=1):
        if is_cjk_text_mark(ch):
            return idx
    return 1


def git_tracked_files(repo: Path) -> list[Path]:
    proc = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
    )
    names = [name for name in proc.stdout.decode("utf-8").split("\0") if name]
    return [Path(name) for name in names]


def scan(repo: Path, allowed: set[str]) -> list[tuple[str, int, int, str]]:
    findings: list[tuple[str, int, int, str]] = []
    for rel_path in git_tracked_files(repo):
        rel = rel_path.as_posix()
        if rel in allowed or rel_path.suffix.lower() in BINARY_SUFFIXES:
            continue
        abs_path = repo / rel_path
        try:
            text = abs_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if has_cjk_text_mark(line):
                snippet = line.strip()
                findings.append((rel, line_no, first_cjk_column(line), snippet))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan tracked public files for Chinese/CJK characters and punctuation."
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="Repository root to scan. Defaults to the current directory.",
    )
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        help="Additional tracked path allowed to contain CJK text marks.",
    )
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    allowed = set(DEFAULT_ALLOWED_PATHS)
    allowed.update(path.strip("/") for path in args.allow)

    findings = scan(repo, allowed)
    if not findings:
        print("OK: no Chinese/CJK characters outside explicitly allowed files.")
        return 0

    print("Found Chinese/CJK characters outside explicitly allowed files:", file=sys.stderr)
    for path, line, col, snippet in findings:
        print(f"{path}:{line}:{col}: {snippet}", file=sys.stderr)
    print("", file=sys.stderr)
    print("Allowed by default:", ", ".join(sorted(allowed)), file=sys.stderr)
    print(
        "Move Chinese copy into an explicitly Chinese file, or update the local allowlist deliberately.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
