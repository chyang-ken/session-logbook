#!/usr/bin/env python3
"""Compare search results and timing between two builds on this machine's real sessions.

Use it for any change that should keep search results identical (performance work,
refactors, source adapters). The synthetic suite (`tests/test_search_contract.py`)
proves the contract; this proves nothing drifted on real, messy data.

    python3 scripts/search_compare.py --baseline origin/staging --queries queries.txt

- The baseline is exported from git into a temporary directory; the candidate is the
  working tree.
- Each build runs in its own process with isolated state, scan cache and history index
  (copies), so neither writes to ~/.session-logbook. Source logs are only read.
- Queries: one per line. Include unique strings, paths, IDs, common words, multi-term,
  and terms with quotes or backslashes.
- Output goes to _private/ (git-ignored): it contains real session IDs and snippets.

Order differences are reported separately. Sessions written between the two runs move
in the activity order; check that any moved session was active before calling it a bug.

Python 3.9+, standard library only.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOME_STATE = Path.home() / ".session-logbook"

RUNNER = r'''
import contextlib, io, json, os, sys, time
from pathlib import Path
sys.path.insert(0, os.getcwd())
import server
state = Path(sys.argv[1])
server.STATE_FILE = state / "state.json"
server.SCAN_CACHE_FILE = state / "scan-cache.json"
server.SCAN_CACHE_BACKUP_DIR = state / "scan-backups"
server.BACKUP_DIR = state / "backups"
server.scan_sessions()
out = {}
for query in json.load(open(sys.argv[2])):
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        start = time.time()
        results = server.search_sessions(query)
        elapsed = time.time() - start
    out[query] = {"results": results, "seconds": elapsed, "warnings": err.getvalue().strip()}
    print(f"{elapsed:7.2f}s {len(results):5} {query[:40]!r}", flush=True)
json.dump(out, open(sys.argv[3], "w"), ensure_ascii=False)
'''


def prepare_state(target):
    target.mkdir(parents=True)
    for name in ("state.json", "scan-cache.json", "history-index.sqlite3"):
        if (HOME_STATE / name).exists():
            shutil.copy2(HOME_STATE / name, target / name)


def run(label, checkout, state, queries_file, out_file):
    print(f"== {label}: {checkout}", flush=True)
    env = dict(os.environ, SESSION_LOGBOOK_HISTORY_INDEX=str(state / "history-index.sqlite3"),
               SESSION_LOGBOOK_EVENTS=str(state / "events.db"))
    runner = state / "runner.py"
    runner.write_text(RUNNER)
    subprocess.run([sys.executable, str(runner), str(state), str(queries_file), str(out_file)],
                   cwd=checkout, env=env, check=True)
    return json.loads(out_file.read_text())


def compare(base, cand):
    same = 0
    for query in base:
        a, b = base[query]["results"], cand[query]["results"]
        if a == b:
            same += 1
            continue
        ia = {r["id"]: r for r in a}
        ib = {r["id"]: r for r in b}
        only_base, only_cand = sorted(set(ia) - set(ib)), sorted(set(ib) - set(ia))
        snippets = sorted(i for i in set(ia) & set(ib) if ia[i] != ib[i])
        kind = "ORDER ONLY" if not (only_base or only_cand or snippets) else "DIFF"
        print(f"{kind:10} {query[:40]!r} only_baseline={only_base[:5]} only_candidate={only_cand[:5]} "
              f"snippet_changes={snippets[:5]}")
    print(f"{same}/{len(base)} queries identical")
    print("seconds  baseline -> candidate")
    for query in base:
        print(f"  {base[query]['seconds']:7.2f} -> {cand[query]['seconds']:7.2f}  {query[:40]!r}"
              + ("  [candidate warned: " + cand[query]["warnings"][:60] + "]" if cand[query]["warnings"] else ""))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--baseline", required=True, help="git ref to compare against, e.g. origin/staging")
    parser.add_argument("--queries", required=True, help="file with one query per line")
    parser.add_argument("--out", default=str(REPO / "_private" / "search-compare"),
                        help="output directory (default: _private/search-compare)")
    args = parser.parse_args()
    queries = [q for q in Path(args.queries).read_text(encoding="utf-8").splitlines() if q.strip()]
    out = Path(args.out) / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True)
    (out / "queries.json").write_text(json.dumps(queries, ensure_ascii=False))
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        baseline = tmp / "baseline"
        baseline.mkdir()
        archive = subprocess.run(["git", "archive", args.baseline], cwd=REPO, capture_output=True, check=True)
        subprocess.run(["tar", "-x", "-C", str(baseline)], input=archive.stdout, check=True)
        for label in ("baseline", "candidate"):
            prepare_state(tmp / f"state-{label}")
        base = run("baseline", baseline, tmp / "state-baseline", out / "queries.json", out / "baseline.json")
        cand = run("candidate", REPO, tmp / "state-candidate", out / "queries.json", out / "candidate.json")
    compare(base, cand)
    print(f"raw results: {out}")


if __name__ == "__main__":
    main()
