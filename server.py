#!/usr/bin/env python3
"""Session Logbook - a minimal, zero-dependency, local dashboard for AI-agent sessions."""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sources import antigravity as ag_source
from sources import anchored_transcript
from sources import codex as codex_source

# ---------- Config ----------
HOST = "127.0.0.1"
PORT = 47821
PROJECTS_DIR = Path.home() / ".claude" / "projects"

# The state file lives in its own home `~/.session-logbook/`. DEFAULT_STATE_FILE is the
# constant production path; STATE_FILE is the path actually in use, which tests can
# monkey-patch to a temp dir. Backup behavior only triggers when the two are equal,
# keeping tests isolated.
DEFAULT_STATE_FILE = Path.home() / ".session-logbook" / "state.json"
STATE_FILE = DEFAULT_STATE_FILE
BACKUP_DIR = DEFAULT_STATE_FILE.parent / "backups"  # `~/.session-logbook/backups/`
BACKUP_RETENTION_DAYS = 30  # backup retention in days; older backups are auto-cleaned

# bump this whenever extract_metadata schema changes
CACHE_SCHEMA_VERSION = 1
SCAN_CACHE_FILE = Path.home() / ".session-logbook" / "scan-cache.json"
SCAN_CACHE_BACKUP_DIR = Path.home() / ".session-logbook" / "scan-cache-backups"

DASHBOARD_DIR = Path(__file__).resolve().parent
INDEX_HTML = DASHBOARD_DIR / "index.html"
VENDOR_DIR = DASHBOARD_DIR / "vendor"

RECENT_USER_N = 3       # grab the last N real user messages from the tail
RECENT_ASSISTANT_N = 3  # grab the last N assistant text messages from the tail
RECENT_QA_N = 2         # grab the last N AskUserQuestion exchanges from the tail (QA is a conversation subtype)
SNIPPET_MAX = 180
TAIL_BUFFER = 300 * 1024  # read trailing 300KB so tool-heavy sessions still capture user input in the middle
DUSTY_AFTER_DAYS = 7  # v2: a session older than this many days and not starred moves to Dusty

# Conversation view limits
# user / assistant / skill body: the server sends full text and the frontend folds it
# by toggling between fold-summary and fold-full DOM nodes. A cap remains to prevent an
# abnormally huge paste from blowing up the response; 200KB is far beyond human scenarios.
# tool_result still uses hard head/tail truncation because tool blocks already have a caret
# fold and are collapsed by default.
CONV_USER_MAX = 200_000
CONV_ASSISTANT_MAX = 200_000
CONV_TOOL_RESULT_MAX = 1500
CONV_TOOL_INPUT_MAX = 300

# Transcript export (docs/decisions/2026-05-11-export-format.md)
TRANSCRIPT_TOOL_RESULT_MAX = 200
BRIEF_TIMEOUT_SEC = 180  # claude -p call timeout; measured ~10-15s, leaving 12x headroom

# Search
SEARCH_SNIPPET_CONTEXT = 60   # how many characters to take on each side of a match
SEARCH_MAX_SNIPPETS = 3       # max snippets returned per session
MAX_JSON_BODY = 1024 * 1024   # local state updates should never need more than 1 MiB
RIPGREP_FALLBACK_PATHS = (
    Path("/opt/homebrew/bin/rg"),  # Apple Silicon Homebrew GUI/background services
    Path("/usr/local/bin/rg"),    # Intel Homebrew and common local installs
)

# ---------- In-memory state ----------
_cache = {}   # jsonl_path_str -> session meta dict
_state = {}   # session_id -> { archived, archived_at, note }
# Whether load_state() has run successfully. Distinguishes genuinely empty state from
# forgotten initialization. Every HTTP handler entry point falls back to load_state()
# (idempotent) to prevent startup paths such as `import server; ThreadingHTTPServer(...)`
# from skipping init and overwriting 260 disk entries with an empty _state.
# Root cause of the 5/17 data-loss incident; see docs/decisions/2026-05-24-state-load-discipline.md.
_state_loaded = False

# Ground-truth reverse table: encode every seen cwd using Claude Code's rules
# ('/' -> '-', '.' -> '-') and map that key back to the original cwd literal. Deleting a
# worktree does not matter because cwd is historical data written by the Claude process.
_CWD_TRUTH_MAP = {}    # encoded_dir_name -> real_cwd
_CWD_INDEX_SEEN = set()  # jsonl_path_str values already peeked; reused by incremental scanning
_CWD_SEQ = {}  # jsonl_path_str -> list[str] order-preserving deduped cwd sequence for pick_project_path
# Mark dirty only when scanning actually changes the cache; frequent /api/sessions polling must not write repeatedly.
_scan_cache_dirty = False


def _is_trusted_http_request(host: str, origin: str = "", fetch_site: str = "") -> bool:
    """Accept browser traffic only from this loopback server's own origin.

    Binding to 127.0.0.1 prevents network access, but does not stop a hostile web page
    from sending requests to localhost. Host validation blocks DNS rebinding, while
    Origin and Sec-Fetch-Site validation block ordinary cross-site browser requests.
    Command-line clients may omit Origin and Sec-Fetch-Site, but must still use a local
    Host header.
    """
    host = (host or "").strip().lower()
    if host not in {HOST, f"{HOST}:{PORT}", "localhost", f"localhost:{PORT}"}:
        return False

    if (fetch_site or "").strip().lower() == "cross-site":
        return False

    origin = (origin or "").strip()
    if not origin:
        return True
    try:
        parsed = urllib.parse.urlparse(origin)
        origin_port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname in {HOST, "localhost"}
        and origin_port == PORT
    )


# ---------- State persistence ----------
def _backup_state_file(state_file, backup_dir, retention_days):
    """Copy state_file into backup_dir with a timestamp suffix and clean old backups.

    Backup failure must NEVER raise. The main flow's atomic write already succeeded; the
    backup is nice-to-have and must not make the save appear failed. A stderr warning is enough.
    """
    if not state_file.exists():
        return
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        shutil.copy2(state_file, backup_dir / f"state-{ts}.json")
        cutoff = time.time() - retention_days * 86400
        for p in backup_dir.glob("state-*.json"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass  # another process may race to read/delete; ignore
    except Exception as e:
        print(f"[warn] backup failed (state still saved): {e}", file=sys.stderr)


def load_state():
    """Load _state from disk. Idempotent: only the first call actually reads from disk.

    On parse failure, log loudly to stderr and leave _state_loaded as False so the next
    call retries instead of getting permanently stuck on a transient failure.
    """
    global _state, _state_loaded
    if _state_loaded:
        return
    if STATE_FILE.exists():
        try:
            _state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            _state_loaded = True
        except Exception as e:
            # Must not silently fall back to {}; that was one root cause of the 5/17 data loss.
            # Leaving _state_loaded=False lets later calls retry, while save_state's sanity check
            # refuses to overwrite a non-empty disk file with empty state.
            print(f"[critical] failed to load state from {STATE_FILE}: {e}. "
                  f"_state stays empty, save_state will refuse to overwrite non-empty disk file.",
                  file=sys.stderr)
    else:
        # A missing file is a valid initial state on first run; mark as loaded.
        _state_loaded = True


def save_state():
    """Persist _state to disk. Two sanity checks guard against overwriting user data.

    Primary: if load_state never succeeded (_state_loaded=False), refuse to write.
      Triggers: (1) starting the server while bypassing main(); (2) disk parse failure.
      In both cases _state is untrusted, so writing would be an overwrite.
    Secondary: if _state_loaded=True but _state is empty while disk is non-empty, refuse
      to write. This should not happen, but catches code bugs as defense in depth.
    """
    if not _state_loaded:
        print(f"[critical] save_state refused: _state has not been loaded from disk "
              f"(load_state() never succeeded). Possible causes: "
              f"(1) server started via `import server; ThreadingHTTPServer(...)` bypassing main(); "
              f"(2) state file at {STATE_FILE} is corrupted and parse failed. "
              f"Aborting save to protect user data.",
              file=sys.stderr)
        return
    if not _state and STATE_FILE.exists():
        try:
            disk_size = STATE_FILE.stat().st_size
        except OSError:
            disk_size = 0
        if disk_size > 100:
            print(f"[critical] save_state refused: empty _state would overwrite "
                  f"{disk_size}B disk file ({STATE_FILE}). _state_loaded=True but "
                  f"_state is empty — possible code bug, not auto-saving.",
                  file=sys.stderr)
            return
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)
    # production-only: after a successful write, do a rotating backup. Failure never raises
    # and does not affect the main save path. Tests monkey-patch STATE_FILE to a temp path,
    # so this auto-skips and leaves the real backup directory untouched.
    if STATE_FILE == DEFAULT_STATE_FILE:
        _backup_state_file(STATE_FILE, BACKUP_DIR, BACKUP_RETENTION_DAYS)


# ---------- Scan cache persistence ----------
def _mark_scan_cache_dirty():
    global _scan_cache_dirty
    _scan_cache_dirty = True


def _backup_scan_cache_file():
    """Copy the scan cache into the backup directory with a timestamp suffix and prune old backups.

    Same discipline as state backups: backup failure does not affect the main write, but it
    must be reported to stderr.
    """
    if not SCAN_CACHE_FILE.exists():
        return
    try:
        SCAN_CACHE_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        shutil.copy2(SCAN_CACHE_FILE, SCAN_CACHE_BACKUP_DIR / f"scan-cache-{ts}.json")
        cutoff = time.time() - BACKUP_RETENTION_DAYS * 86400
        for p in SCAN_CACHE_BACKUP_DIR.glob("scan-cache-*.json"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass
    except Exception as e:
        print(f"[warn] backup failed (scan cache still saved): {e}", file=sys.stderr)


def _scan_cache_payload():
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "cache": _cache,
        "cwd_truth_map": _CWD_TRUTH_MAP,
        "cwd_index_seen": sorted(_CWD_INDEX_SEEN),
        "cwd_seq": _CWD_SEQ,
    }


def _validate_scan_cache_payload(payload):
    if not isinstance(payload, dict):
        return False, "payload is not an object"
    if payload.get("schema_version") != CACHE_SCHEMA_VERSION:
        return False, "schema version mismatch"
    cache = payload.get("cache")
    truth = payload.get("cwd_truth_map")
    seen = payload.get("cwd_index_seen")
    seq = payload.get("cwd_seq")
    if not isinstance(cache, dict):
        return False, "cache is not an object"
    if not isinstance(truth, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in truth.items()
    ):
        return False, "cwd_truth_map is not a string map"
    if not isinstance(seen, list) or not all(isinstance(v, str) for v in seen):
        return False, "cwd_index_seen is not a string list"
    if not isinstance(seq, dict) or not all(
        isinstance(k, str)
        and isinstance(v, list)
        and all(isinstance(item, str) for item in v)
        for k, v in seq.items()
    ):
        return False, "cwd_seq is not a string-list map"
    return True, ""


def load_scan_cache():
    """Restore the warm scan cache from disk. Any failure returns False so the caller falls back to a full scan."""
    global _cache, _CWD_TRUTH_MAP, _CWD_INDEX_SEEN, _CWD_SEQ, _scan_cache_dirty
    if not SCAN_CACHE_FILE.exists():
        return False
    try:
        payload = json.loads(SCAN_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[warn] failed to load scan cache from {SCAN_CACHE_FILE}: {e}", file=sys.stderr)
        return False

    ok, reason = _validate_scan_cache_payload(payload)
    if not ok:
        if reason == "schema version mismatch":
            got = payload.get("schema_version") if isinstance(payload, dict) else None
            print(
                f"[info] scan cache schema mismatch: got {got}, "
                f"want {CACHE_SCHEMA_VERSION}; forcing full rescan.",
                file=sys.stderr,
            )
        else:
            print(f"[warn] invalid scan cache at {SCAN_CACHE_FILE}: {reason}", file=sys.stderr)
        return False

    _cache = dict(payload["cache"])
    _CWD_TRUTH_MAP = dict(payload["cwd_truth_map"])
    _CWD_INDEX_SEEN = set(payload["cwd_index_seen"])
    _CWD_SEQ = {k: list(v) for k, v in payload["cwd_seq"].items()}
    _DECODE_DIR_CACHE.clear()
    _scan_cache_dirty = False
    return True


def save_scan_cache():
    """Persist the warm scan cache. Skip when it is not dirty to avoid write amplification from polling."""
    global _scan_cache_dirty
    if not _scan_cache_dirty:
        return False
    payload = _scan_cache_payload()
    ok, reason = _validate_scan_cache_payload(payload)
    if not ok:
        print(f"[critical] save_scan_cache refused: {reason}", file=sys.stderr)
        return False
    try:
        SCAN_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SCAN_CACHE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(SCAN_CACHE_FILE)
        _scan_cache_dirty = False
        _backup_scan_cache_file()
        return True
    except Exception as e:
        print(f"[warn] failed to save scan cache to {SCAN_CACHE_FILE}: {e}", file=sys.stderr)
        return False


# ---------- JSONL tail extraction ----------
def _assistant_text(content):
    """Extract text blocks from the assistant's block array and join them into one string."""
    if not isinstance(content, list):
        return ""
    parts = []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "text":
            t = b.get("text", "")
            if t:
                parts.append(t)
    return " ".join(parts)


# System-injection prefixes for user-role records that are not user input. The harness
# writes these events into JSONL as user messages, but semantically they belong to the
# system/agent side. Once recognized, classify them separately so they do not count as
# user input.
#   <command-*> / <local-command-*>   slash command injection + hook stdout
#   <system-reminder>                 system prompt
#   <task-notification>               background-task completion notice
#   <bash-stdout> / <bash-stderr>     bash-mode output from `!command`; unlike <bash-input>
#   <teammate-message ...>            teammate agent report with variable attributes
_SYSTEM_USER_PREFIXES_SKIP = ("<local-command-", "<command-", "<system-reminder>")
_SYSTEM_USER_PREFIXES_EVENT = ("<task-notification>", "<bash-stdout>", "<bash-stderr>", "<teammate-message")


def _is_system_user_string(stripped):
    """Whether user-role string content is a system-side injection rather than real user input. stripped should be lstrip output."""
    return stripped.startswith(_SYSTEM_USER_PREFIXES_SKIP) or stripped.startswith(_SYSTEM_USER_PREFIXES_EVENT)


def _user_text(content):
    """Return string content only; arrays are tool_result and are skipped.
    Also skip slash-command injections and system-side pseudo-user messages such as
    task-notification / bash output / teammate.
    """
    if isinstance(content, str):
        stripped = content.lstrip()
        if _is_system_user_string(stripped):
            return ""
        return content
    return ""


def _truncate(text: str, n: int) -> str:
    text = re.sub(r"[^\S\n]+", " ", text)  # collapse spaces/tabs while keeping \n
    text = re.sub(r"\n{3,}", "\n\n", text)  # collapse consecutive blank lines to at most one
    text = text.strip()
    if len(text) > n:
        return text[:n].rstrip() + "…"
    return text


def _extract_first_user_msg(f):
    """Read forward from the file head and return the first user message with string content. f is at the start."""
    for _ in range(500):  # scan at most 500 lines to avoid an oversized head
        line = f.readline()
        if not line:
            break
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") == "user":
            text = _user_text(d.get("message", {}).get("content"))
            if text:
                return {
                    "role": "user",
                    "text": _truncate(text, SNIPPET_MAX),
                    "ts": d.get("timestamp"),
                    "is_first": True,
                }
    return None


def _index_cwd(cwd: str):
    """Derive the corresponding directory name from a real cwd using Claude Code's encoding rules and store it in _CWD_TRUTH_MAP."""
    if not cwd or not isinstance(cwd, str):
        return
    encoded = cwd.replace("/", "-").replace(".", "-")
    if _CWD_TRUTH_MAP.get(encoded) != cwd:
        _CWD_TRUTH_MAP[encoded] = cwd
        _DECODE_DIR_CACHE.pop(encoded, None)
        _mark_scan_cache_dirty()


def _collect_cwds(jsonl_path: Path):
    """Scan the full jsonl and collect every cwd, deduped while preserving file order.

    Also cache the sequence into _CWD_SEQ for pick_project_path so extract_metadata does
    not scan again. Cold-start total cost is around 1s in measured data (200MB / 40k
    lines); after incremental scanning, the next run is essentially zero.
    """
    cwds_seq = []  # order-preserving
    seen = set()
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                cwd = obj.get("cwd")
                if cwd and cwd not in seen:
                    seen.add(cwd)
                    cwds_seq.append(cwd)
    except Exception:
        pass
    key = str(jsonl_path)
    if _CWD_SEQ.get(key) != cwds_seq:
        _CWD_SEQ[key] = cwds_seq
        _mark_scan_cache_dirty()
    return cwds_seq


def _rebuild_cwd_truth_map_from_seq():
    """Rebuild the truth map from current _CWD_SEQ so deleted/rewritten jsonl files do not leave stale cwd values."""
    new_truth = {}
    for cwds in _CWD_SEQ.values():
        for cwd in cwds:
            if cwd and isinstance(cwd, str):
                encoded = cwd.replace("/", "-").replace(".", "-")
                new_truth[encoded] = cwd
    if new_truth != _CWD_TRUTH_MAP:
        _CWD_TRUTH_MAP.clear()
        _CWD_TRUTH_MAP.update(new_truth)
        _DECODE_DIR_CACHE.clear()
        _mark_scan_cache_dirty()


def _remove_cwd_index_for_path(key: str):
    removed = False
    if key in _CWD_INDEX_SEEN:
        _CWD_INDEX_SEEN.remove(key)
        removed = True
    if key in _CWD_SEQ:
        del _CWD_SEQ[key]
        removed = True
    if removed:
        _mark_scan_cache_dirty()
    return removed


def _build_cwd_map(force: bool = False):
    """Scan all jsonl files to build the dir_name -> cwd reverse map and cwd sequence cache.

    Incremental mode skips files already scanned. This must run before extract_metadata so
    fallback can see ground truth and does not cache the wrong path when a cwd-less worktree
    session is extracted first.
    """
    if not PROJECTS_DIR.exists():
        return
    cwd_seq_changed = False
    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl_path in project_dir.glob("*.jsonl"):
            key = str(jsonl_path)
            try:
                cur_mtime = jsonl_path.stat().st_mtime
            except FileNotFoundError:
                continue
            cached = _cache.get(key)
            unchanged = (
                cached is not None
                and cached.get("mtime") == cur_mtime
                and key in _CWD_INDEX_SEEN
            )
            if not force and unchanged:
                continue
            old_seq = _CWD_SEQ.get(key)
            if key not in _CWD_INDEX_SEEN:
                _mark_scan_cache_dirty()
            _CWD_INDEX_SEEN.add(key)
            cwds = _collect_cwds(jsonl_path)
            if old_seq != cwds:
                cwd_seq_changed = True
    if cwd_seq_changed:
        _rebuild_cwd_truth_map_from_seq()


# ---------- project_path selection ----------
# Philosophy: the folder name is Claude Code's encoded startup cwd, a naturally stable anchor.
# Agent cd commands during a session are implementation details, not project intent. See
# docs/decisions/2026-05-14-project-path-strategy.md.
_PROJECT_PATH_SHALLOW = {"/", "/Users", "/home"}


def _find_anchor_from_folder(folder_name: str, cwds_seq: list):
    """Find the cwd whose encoded form equals folder_name, i.e. the user's startup cwd.
    Return None when decoding is uncertain (directory names containing '-' or '.' are ambiguous),
    so the caller can use its fallback.
    """
    if not folder_name or not folder_name.startswith("-"):
        return None
    for c in cwds_seq:
        if c.replace("/", "-").replace(".", "-") == folder_name:
            return c
    return None


def pick_project_path(folder_name: str, cwds_seq: list):
    """Choose project_path from the full jsonl cwd sequence plus folder name.

    Priority:
      1. If the last cwd is under .claude/worktrees/, keep the last cwd (worktree work-root).
      2. If there is a single cwd, use it directly.
      3. For multiple cwd values, compute commonpath; use it only when it stays under the
         folder anchor, otherwise fall back to the last cwd.
    """
    if not cwds_seq:
        return None
    last = cwds_seq[-1]
    if "/.claude/worktrees/" in last or "/.worktrees/" in last:
        return last
    if len(cwds_seq) == 1:
        return last
    try:
        common = os.path.commonpath(cwds_seq)
    except ValueError:
        return last
    anchor = _find_anchor_from_folder(folder_name, cwds_seq)
    if anchor and (common == anchor or common.startswith(anchor + "/")):
        return common
    # If no anchor matched, use SHALLOW as a guard against degenerate commonpath values above /Users.
    if common in _PROJECT_PATH_SHALLOW:
        return last
    return common


_DECODE_DIR_CACHE = {}


def _decode_project_dir(dir_name: str):
    """Decode a Claude Code project directory name.

    Encoding replaces '/' and '.' with '-' while keeping original '-' characters, making
    the three cases ambiguous. Priority:
      1. _CWD_TRUTH_MAP lookup: ground truth derived from cwd fields in historical jsonl.
      2. Filesystem disambiguation fallback: walk from '/' one directory at a time. When
         walking cannot continue, keep the confirmed ancestor and naively decode the rest.

    Names not starting with '-' are treated as non-encoded and return None.
    """
    if not dir_name.startswith("-"):
        return None
    # 1. Ground-truth lookup
    truth = _CWD_TRUTH_MAP.get(dir_name)
    if truth:
        return truth
    # 2. Filesystem disambiguation
    if dir_name in _DECODE_DIR_CACHE:
        return _DECODE_DIR_CACHE[dir_name]

    def walk(cur: Path, rem: str):
        """Return (deepest_real_path, leftover_encoded_str).
        Empty leftover means decoding fully succeeded; non-empty means no matching filesystem
        path exists after that level.
        """
        if not rem:
            return cur, ""
        try:
            children = list(cur.iterdir())
        except (FileNotFoundError, PermissionError, NotADirectoryError, OSError):
            return cur, rem
        cands = []
        for c in children:
            enc = c.name.replace(".", "-")  # keep aligned with Claude Code encoding rules
            if rem == enc:
                cands.append((c, len(enc), True))
            elif rem.startswith(enc + "-"):
                cands.append((c, len(enc), False))
        cands.sort(key=lambda x: -x[1])  # try longer prefixes first
        best = (cur, rem)  # fallback for no match at this level: current path plus all remaining text
        for c, ln, exact in cands:
            if exact:
                return c, ""
            sub = walk(c, rem[ln + 1:])
            # choose the branch with the shortest leftover, i.e. deepest walk
            if len(sub[1]) < len(best[1]):
                best = sub
                if not sub[1]:
                    break  # fully matched; exit early
        return best

    deepest, leftover = walk(Path("/"), dir_name[1:])
    if leftover:
        # The leaf disappeared; naively decode '-' as '/' because '.' and original '-' are indistinguishable.
        decoded = str(deepest / leftover.replace("-", "/"))
    else:
        decoded = str(deepest)
    _DECODE_DIR_CACHE[dir_name] = decoded
    return decoded


def _clean_custom_title(raw):
    """Normalize customTitle: trim whitespace and filter injection prefixes that can leak from Claude Code auto titles."""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    if s.startswith(("<local-command-", "<command-", "<system-reminder>")):
        return None
    return s


def _extract_custom_title(jsonl_path: Path):
    """Scan the full JSONL and return the customTitle from the last custom-title row.

    Each /title call appends a {"type":"custom-title","customTitle":"..."} row; the last
    one is the currently effective title. A bytes substring prefilter avoids parsing JSON
    for every line.
    """
    needle = b'"type":"custom-title"'
    last = None
    try:
        with open(jsonl_path, "rb") as f:
            for line in f:
                if needle not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") == "custom-title":
                    cleaned = _clean_custom_title(d.get("customTitle"))
                    if cleaned:
                        last = cleaned
    except Exception:
        return None
    return last


def extract_metadata(jsonl_path: Path):
    """Read jsonl head (first user message) plus tail, then merge a conversation preview.

    Key design: collect the recent N user and assistant messages on separate tracks, then
    merge by timestamp. This prevents assistant/tool-heavy sessions from using the whole
    preview budget and pushing user input out of view.
    """
    try:
        st = jsonl_path.stat()
    except FileNotFoundError:
        return None
    size = st.st_size
    mtime = st.st_mtime

    session_id = jsonl_path.stem
    project_path = None
    last_stop_reason = None
    first_user_msg = None

    try:
        with open(jsonl_path, "rb") as f:
            # 1) Read the file head first to capture the first user message as the opener
            first_user_msg = _extract_first_user_msg(f)

            # 2) Then read the tail
            if size > TAIL_BUFFER:
                f.seek(-TAIL_BUFFER, os.SEEK_END)
                f.readline()  # Drop the possibly incomplete first line
                tail_bytes = f.read()
            else:
                f.seek(0)
                tail_bytes = f.read()
    except Exception as e:
        print(f"[warn] read failed {jsonl_path}: {e}", file=sys.stderr)
        return {
            "id": session_id,
            "project_path": "",
            "jsonl_path": str(jsonl_path),
            "mtime": mtime,
            "mtime_iso": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
            "size": size,
            "recent_msgs": [],
            "last_stop_reason": None,
            "parse_error": str(e),
        }

    tail_text = tail_bytes.decode("utf-8", errors="replace")
    lines = [l for l in tail_text.split("\n") if l.strip()]

    # Count real user turns to detect claude -p one-shot sessions, which have only the head
    # user message. When tail covers the whole file (size <= TAIL_BUFFER), the count is exact;
    # otherwise the file is large enough to be multi-turn, so clamp to 2 to avoid one-shot
    # misclassification. tool_result array content and command/system-reminder injections do not count.
    user_turn_count = 0
    for line in lines:
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") != "user":
            continue
        msg_content = d.get("message", {}).get("content")
        if isinstance(msg_content, str):
            stripped = msg_content.lstrip()
            if stripped and not _is_system_user_string(stripped):
                user_turn_count += 1
        elif isinstance(msg_content, list):
            for b in msg_content:
                if isinstance(b, dict) and b.get("type") == "text":
                    txt = (b.get("text") or "").lstrip()
                    if txt and not txt.startswith("<system-reminder>"):
                        user_turn_count += 1
                        break
    if size > TAIL_BUFFER:
        user_turn_count = max(user_turn_count, 2)

    # Track separately: take the last N user and assistant messages independently
    tail_users = []
    tail_asts = []

    for line in reversed(lines):
        try:
            d = json.loads(line)
        except Exception:
            continue
        t = d.get("type")
        if project_path is None:
            cwd = d.get("cwd")
            if cwd:
                project_path = cwd
                _index_cwd(cwd)  # also build the reverse table for cwd-less sibling sessions in the same scan batch
        if last_stop_reason is None and t == "assistant":
            last_stop_reason = d.get("message", {}).get("stop_reason")

        if t == "assistant" and len(tail_asts) < RECENT_ASSISTANT_N:
            text = _assistant_text(d.get("message", {}).get("content"))
            if text:
                tail_asts.append({
                    "role": "assistant",
                    "text": _truncate(text, SNIPPET_MAX),
                    "ts": d.get("timestamp"),
                })
        elif t == "user" and len(tail_users) < RECENT_USER_N:
            text = _user_text(d.get("message", {}).get("content"))
            if text:
                tail_users.append({
                    "role": "user",
                    "text": _truncate(text, SNIPPET_MAX),
                    "ts": d.get("timestamp"),
                })

        # Stop once both quotas are full
        if len(tail_users) >= RECENT_USER_N and len(tail_asts) >= RECENT_ASSISTANT_N:
            break

    # QA third track: scan the tail forward to pair AskUserQuestion tool_use with tool_result,
    # then keep the most recent RECENT_QA_N. This is a separate pass rather than part of the
    # reversed loop because QA spans two jsonl rows, so forward pairing is simpler. If
    # TAIL_BUFFER cuts an incomplete pair, drop it silently instead of displaying or crashing.
    qa_pending_meta = {}
    tail_qas = []
    for line in lines:
        try:
            d = json.loads(line)
        except Exception:
            continue
        tt = d.get("type")
        if tt == "assistant":
            content = d.get("message", {}).get("content")
            if isinstance(content, list):
                for b in content:
                    if (isinstance(b, dict) and b.get("type") == "tool_use"
                            and b.get("name") == "AskUserQuestion"):
                        qa_pending_meta[b.get("id", "")] = {
                            "input": b.get("input", {}),
                            "ts": d.get("timestamp", ""),
                        }
        elif tt == "user":
            msg_content = d.get("message", {}).get("content")
            if isinstance(msg_content, list):
                for b in msg_content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        tid = b.get("tool_use_id", "")
                        if tid in qa_pending_meta:
                            pinfo = qa_pending_meta.pop(tid)
                            qa_unit = _build_qa_unit(
                                pinfo["input"], d.get("toolUseResult"))
                            if qa_unit:
                                tail_qas.append({
                                    "role": "qa",
                                    "ts": pinfo["ts"],
                                    "text": _format_qa_preview(qa_unit, SNIPPET_MAX),
                                })
                                if len(tail_qas) > RECENT_QA_N:
                                    tail_qas = tail_qas[-RECENT_QA_N:]

    # Upgrade project_path by reselecting from the full cwd sequence cached by _build_cwd_map,
    # correcting for cd drift. The reversed tail only sees 300KB and may miss early cwd values;
    # pick_project_path sees the full sequence.
    folder_name = jsonl_path.parent.name
    cwd_seq = _CWD_SEQ.get(str(jsonl_path))
    if cwd_seq:
        picked = pick_project_path(folder_name, cwd_seq)
        if picked:
            project_path = picked

    if project_path is None:
        decoded = _decode_project_dir(folder_name)
        project_path = decoded if decoded else folder_name

    # Merge the tail by timestamp ascending (old to new)
    all_tail = tail_users + tail_asts + tail_qas
    all_tail.sort(key=lambda m: m.get("ts") or "")

    # Prepend first_user_msg unless it is already in tail_users for a short session
    recent_msgs = []
    if first_user_msg:
        tail_user_ts = {m.get("ts") for m in tail_users}
        if first_user_msg.get("ts") not in tail_user_ts:
            recent_msgs.append(first_user_msg)
    recent_msgs.extend(all_tail)

    return {
        "id": session_id,
        "project_path": project_path,
        "jsonl_path": str(jsonl_path),
        "mtime": mtime,
        "mtime_iso": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
        "size": size,
        "recent_msgs": recent_msgs,
        "last_stop_reason": last_stop_reason,
        "user_turn_count": user_turn_count,
        "custom_title": _extract_custom_title(jsonl_path),
    }


# ---------- Conversation extraction ----------
def _tool_input_summary(name, inp):
    """Generate a one-line summary of tool call input."""
    if not isinstance(inp, dict):
        return str(inp)[:CONV_TOOL_INPUT_MAX]
    if name in ('Read', 'Write'):
        return inp.get('file_path', '')
    if name == 'Edit':
        return inp.get('file_path', '')
    if name == 'Bash':
        return (inp.get('command') or '')[:CONV_TOOL_INPUT_MAX]
    if name == 'Grep':
        p = inp.get('pattern', '')
        d = inp.get('path', '')
        return f'{p}' + (f' in {d}' if d else '')
    if name == 'Glob':
        return inp.get('pattern', '')
    if name == 'Agent':
        return inp.get('description', '') or (inp.get('prompt') or '')[:100]
    if name in ('TaskCreate', 'TaskUpdate'):
        return inp.get('description', '') or inp.get('id', '')
    if name == 'Skill':
        return inp.get('skill', '')
    parts = []
    for k, v in list(inp.items())[:3]:
        parts.append(f'{k}={str(v)[:80]}')
    return ', '.join(parts)[:CONV_TOOL_INPUT_MAX]


def _tool_result_content(content):
    """Extract plain text from tool_result content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get('type') == 'text':
                    parts.append(b.get('text', ''))
                elif b.get('type') == 'image':
                    parts.append('[image]')
            elif isinstance(b, str):
                parts.append(b)
        return '\n'.join(parts)
    return str(content)


def _truncate_conv(text, max_chars):
    """Truncate text, keeping the head."""
    if not text or len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + ' …'


def _extract_inner_xml(text, tag):
    """Extract the first body from `<tag>...</tag>`, returning an empty string if absent.
    Used to parse pseudo-XML system events injected by the harness: task-notification,
    bash-output, and teammate.
    """
    open_tag = f'<{tag}>'
    close_tag = f'</{tag}>'
    i = text.find(open_tag)
    if i < 0:
        return ''
    j = text.find(close_tag, i + len(open_tag))
    if j < 0:
        return ''
    return text[i + len(open_tag):j]


def _parse_system_event(stripped):
    """Recognize user-role pseudo messages as system-event turns. stripped is lstrip output.
    Return dict({type, text}) or None; None means it is not an event and the original skip
    logic should apply.
    """
    if stripped.startswith('<task-notification>'):
        status = _extract_inner_xml(stripped, 'status') or '?'
        summary = _extract_inner_xml(stripped, 'summary') or '(no summary)'
        return {'type': 'system_notification',
                'text': f'[{status}] {summary}'}
    if stripped.startswith('<bash-stdout>') or stripped.startswith('<bash-stderr>'):
        out = _extract_inner_xml(stripped, 'bash-stdout')
        err = _extract_inner_xml(stripped, 'bash-stderr')
        parts = []
        if out and out != '(Bash completed with no output)':
            parts.append(out)
        if err:
            parts.append(f'[stderr] {err}')
        body = '\n'.join(parts) if parts else '(no output)'
        return {'type': 'bash_output', 'text': body}
    if stripped.startswith('<teammate-message'):
        # Attributes may include teammate_id / summary; body follows the closing >
        head_end = stripped.find('>')
        head = stripped[:head_end] if head_end > 0 else ''
        # Extract teammate_id and summary attributes
        def _attr(name):
            key = f'{name}="'
            i = head.find(key)
            if i < 0:
                return ''
            i += len(key)
            j = head.find('"', i)
            return head[i:j] if j > i else ''
        teammate = _attr('teammate_id') or 'teammate'
        summary = _attr('summary')
        body_text = stripped[head_end + 1:].rstrip()
        # Drop the trailing </teammate-message> from the body
        close_tag = '</teammate-message>'
        if body_text.endswith(close_tag):
            body_text = body_text[:-len(close_tag)].rstrip()
        display = summary or body_text or '(empty)'
        return {'type': 'teammate_message',
                'text': f'[{teammate}] {display}'}
    return None


def _truncate_tool_result(text, max_chars):
    """Truncate tool result, keeping head and tail."""
    if not text or len(text) <= max_chars:
        return text
    head = int(max_chars * 0.65)
    tail = max_chars - head - 25
    if tail < 50:
        return text[:max_chars].rstrip() + ' …'
    return text[:head].rstrip() + '\n⋯ truncated ⋯\n' + text[-tail:].lstrip()


# ---------- QA (AskUserQuestion) parsing ----------
# Decision log: docs/decisions/2026-05-13-qa-turn-type.md
# QA is a converged-conversation subtype at the same level as USER / ASSISTANT, not a truncated tool_result.
# Input = assistant tool_use(AskUserQuestion).input; output = the user jsonl row toolUseResult.

def _build_qa_unit(tool_use_input, tool_use_result):
    """Build a structured QA unit from AskUserQuestion input plus the user's answer snapshot.

    Returns a dict with questions: [{question, header, multiSelect, options, answer, matched_option}].
    matched_option is the index in options; None means a free-form Other answer, often the
    most important user correction signal. Return None on upstream anomalies so the caller
    can fall back to the generic tool path.
    """
    questions_in = (tool_use_input or {}).get('questions') or []
    if not questions_in:
        return None
    answers_map = {}
    if isinstance(tool_use_result, dict):
        raw_ans = tool_use_result.get('answers')
        if isinstance(raw_ans, dict):
            answers_map = raw_ans

    out_q = []
    for q in questions_in:
        if not isinstance(q, dict):
            continue
        qtext = q.get('question', '') or ''
        options = []
        for opt in q.get('options') or []:
            if not isinstance(opt, dict):
                continue
            options.append({
                'label': opt.get('label', '') or '',
                'description': opt.get('description', '') or '',
                'preview': opt.get('preview') or None,
            })
        ans = answers_map.get(qtext, '') or ''
        matched = None
        for i, opt in enumerate(options):
            if opt['label'] and opt['label'] == ans:
                matched = i
                break
        out_q.append({
            'question': qtext,
            'header': q.get('header', '') or '',
            'multiSelect': bool(q.get('multiSelect')),
            'options': options,
            'answer': ans,
            'matched_option': matched,
        })

    if not out_q:
        return None
    return {'questions': out_q}


def _format_qa_transcript(qa_unit):
    """Render a QA unit as an export markdown block.

    When matched, omit option descriptions to save tokens. For Other, keep descriptions so
    the handoff agent can see what the user rejected. Answers are never truncated.
    """
    lines = []
    for q in qa_unit['questions']:
        lines.append(f"Q: {q['question']}")
        is_other = q['matched_option'] is None and q['answer']
        for i, opt in enumerate(q['options']):
            letter = chr(ord('A') + i)
            lines.append(f"  [{letter}] {opt['label']}")
            if is_other and opt['description']:
                lines.append(f"      {opt['description']}")
        if q['answer']:
            if q['matched_option'] is not None:
                letter = chr(ord('A') + q['matched_option'])
                lines.append(f"→ {letter} (selected): {q['answer']}")
            else:
                lines.append(f"→ Other: {q['answer']}")
        else:
            lines.append("→ (no answer)")
        lines.append("")
    return '\n'.join(lines).rstrip()


def _format_qa_preview(qa_unit, max_chars):
    """Render a compact QA preview: one Q line plus one A line, merging multiple questions."""
    parts = []
    for q in qa_unit['questions']:
        ans = q['answer'] or '(no answer)'
        is_other = q['matched_option'] is None and q['answer']
        prefix = '↳ Other: ' if is_other else '↳ '
        parts.append(f"Q: {q['question']}\n{prefix}{ans}")
    return _truncate('\n\n'.join(parts), max_chars)


def extract_conversation(jsonl_path):
    """Read the full JSONL and extract conversation content, filtering metadata/thinking/image and pairing tool_use with result."""
    turns = []
    pending_tools = {}  # tool_use_id -> {name, summary, ts}
    next_is_skill = False  # previous user turn was <command-*>, so the next one is a skill injection
    # When the AI calls the Skill tool proactively (not via a user slash command), the SDK
    # inserts a user-array text block as the skill body after tool_result. That row used to
    # be misclassified as user. Track it with a separate flag rather than reusing next_is_skill:
    # tool_result does not consume it; the later skill body row consumes it.
    pending_skill_body = False
    total_lines = 0
    project_path = None
    custom_title = None  # title set by /title; take the last one in the file

    with open(jsonl_path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            total_lines += 1
            try:
                d = json.loads(line)
            except Exception:
                continue

            t = d.get('type')
            ts = d.get('timestamp', '')

            if project_path is None and d.get('cwd'):
                project_path = d['cwd']

            if t == 'custom-title':
                cleaned = _clean_custom_title(d.get('customTitle'))
                if cleaned:
                    custom_title = cleaned
                continue

            if t == 'assistant':
                content = d.get('message', {}).get('content', [])
                if not isinstance(content, list):
                    continue
                text_parts = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    bt = block.get('type')
                    if bt == 'text':
                        txt = block.get('text', '')
                        if txt:
                            text_parts.append(txt)
                    elif bt == 'tool_use':
                        tool_id = block.get('id', '')
                        tname = block.get('name', '?')
                        # Translate Agent (subagent dispatch) into a subagent_spawn turn. The
                        # corresponding tool_result is not expanded yet, but the tool_id must be
                        # recorded with a spawn sentinel so the later tool_result branch skips it.
                        # Otherwise it falls through to the unknown-tool fallback and renders an
                        # anonymous tool block alongside subagent_spawn, violating spec section 6.2.
                        if tname == 'Agent':
                            inp = block.get('input', {}) or {}
                            sub_name = inp.get('subagent_type') or 'general-purpose'
                            desc = inp.get('description') or ''
                            if not desc:
                                # Fallback: when description is missing, use the first 120 prompt characters
                                prompt = inp.get('prompt', '') or ''
                                desc = prompt[:120]
                            turns.append({
                                'type': 'subagent_spawn',
                                'name': sub_name,
                                'description': _truncate_conv(desc, 500),
                                'ts': ts,
                            })
                            pending_tools[tool_id] = {'__spawn__': True}
                            continue
                        pending_tools[tool_id] = {
                            'name': tname,
                            'summary': _tool_input_summary(
                                tname, block.get('input', {})),
                            'ts': ts,
                            # Only AskUserQuestion keeps raw input for later QA unit synthesis
                            '_raw_input': block.get('input', {}) if tname == 'AskUserQuestion' else None,
                        }
                        # AI called the Skill tool: mark the next user-array text block as the skill body
                        if tname == 'Skill':
                            pending_skill_body = True
                    # thinking: skip entirely
                if text_parts:
                    turns.append({
                        'type': 'assistant',
                        'text': _truncate_conv(
                            '\n'.join(text_parts), CONV_ASSISTANT_MAX),
                        'ts': ts,
                    })

            elif t == 'user':
                msg_content = d.get('message', {}).get('content')
                if isinstance(msg_content, str):
                    text = msg_content.strip()
                    if not text:
                        next_is_skill = False
                    elif text.startswith('<command-'):
                        # slash-command injection: the next user message is the skill body
                        next_is_skill = True
                    elif text.startswith(('<local-command-', '<system-reminder>')):
                        # silent injection only: skip without affecting next_is_skill
                        next_is_skill = False
                    elif (event := _parse_system_event(text)) is not None:
                        # System events (task-notification / bash output / teammate) become
                        # independent turns, not user/skill, and do not affect next_is_skill.
                        next_is_skill = False
                        turns.append({
                            **event,
                            'text': _truncate_conv(event['text'], CONV_USER_MAX),
                            'ts': ts,
                        })
                    else:
                        turn_type = 'skill' if next_is_skill else 'user'
                        next_is_skill = False
                        turns.append({
                            'type': turn_type,
                            'text': _truncate_conv(text, CONV_USER_MAX),
                            'ts': ts,
                        })
                elif isinstance(msg_content, list):
                    user_texts = []
                    for block in msg_content:
                        if not isinstance(block, dict):
                            continue
                        bt = block.get('type')
                        if bt == 'tool_result':
                            tool_id = block.get('tool_use_id', '')
                            raw = _tool_result_content(
                                block.get('content', ''))
                            is_err = block.get('is_error', False)
                            if tool_id in pending_tools:
                                tc = pending_tools.pop(tool_id)
                                # Agent (subagent spawn) tool_result is not expanded yet
                                if tc.get('__spawn__'):
                                    continue
                                # AskUserQuestion becomes a QA turn, a conversation subtype, not a truncated tool path
                                if tc['name'] == 'AskUserQuestion':
                                    qa_unit = _build_qa_unit(
                                        tc.get('_raw_input'),
                                        d.get('toolUseResult'))
                                    if qa_unit:
                                        turns.append({
                                            'type': 'qa',
                                            'questions': qa_unit['questions'],
                                            'ts': tc.get('ts', ts),
                                        })
                                        continue
                                    # fallback: on parse failure, use the tool path
                                tc.pop('_raw_input', None)
                                tc['result'] = _truncate_tool_result(
                                    raw, CONV_TOOL_RESULT_MAX)
                                tc['is_error'] = is_err
                                turns.append({'type': 'tool', **tc})
                            else:
                                turns.append({
                                    'type': 'tool', 'name': '?',
                                    'summary': '',
                                    'result': _truncate_tool_result(
                                        raw, CONV_TOOL_RESULT_MAX),
                                    'is_error': is_err, 'ts': ts,
                                })
                        elif bt == 'text':
                            txt = block.get('text', '')
                            if txt and not txt.lstrip().startswith(
                                    '<system-reminder>'):
                                user_texts.append(txt)
                        elif bt == 'image':
                            user_texts.append('[image]')
                    if user_texts:
                        full = '\n'.join(user_texts).strip()
                        if not full or full.startswith((
                                '<local-command-', '<command-')):
                            next_is_skill = False
                            pending_skill_body = False
                        elif full.startswith('[Request interrupted by user'):
                            # User interrupted the agent: system-injected event text, not user input
                            turns.append({
                                'type': 'system_notification',
                                'text': 'Request interrupted by user',
                                'ts': ts,
                            })
                            next_is_skill = False
                            pending_skill_body = False
                        elif full == 'Continue from where you left off.':
                            # Resume prompt injected by --resume / Continue button, not user input
                            turns.append({
                                'type': 'system_notification',
                                'text': 'Continue from where you left off',
                                'ts': ts,
                            })
                            next_is_skill = False
                            pending_skill_body = False
                        elif pending_skill_body or full.startswith(
                                'Base directory for this skill: '):
                            # Body injected after the AI called Skill: classify as skill, not user
                            turns.append({
                                'type': 'skill',
                                'text': _truncate_conv(full, CONV_USER_MAX),
                                'ts': ts,
                            })
                            next_is_skill = False
                            pending_skill_body = False
                        else:
                            turn_type = 'skill' if next_is_skill else 'user'
                            next_is_skill = False
                            pending_skill_body = False
                            turns.append({
                                'type': turn_type,
                                'text': _truncate_conv(
                                    full, CONV_USER_MAX),
                                'ts': ts,
                            })
                    else:
                        # Pure tool_result messages (for example Skill launch reports) do not
                        # consume pending_skill_body / next_is_skill; the following real text inherits it.
                        pass

    return {
        'id': jsonl_path.stem,
        'project_path': project_path or '',
        'total_lines': total_lines,
        'turns': turns,
        'custom_title': custom_title,
    }


def extract_transcript(jsonl_path: Path) -> str:
    """Token-optimized export: v1 markdown shape, untruncated user/assistant, 200-char tool_result.

    Design basis: docs/decisions/2026-05-11-export-format.md. The key difference from
    extract_conversation is that messages are the core signal in export and are never truncated.
    """
    out_blocks = []
    pending_tools = {}
    next_is_skill = False
    project_path = None
    total_lines = 0

    with open(jsonl_path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            total_lines += 1
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = d.get('type')
            if project_path is None and d.get('cwd'):
                project_path = d['cwd']

            if t == 'assistant':
                content = d.get('message', {}).get('content', [])
                if not isinstance(content, list):
                    continue
                text_parts = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    bt = block.get('type')
                    if bt == 'text':
                        txt = block.get('text', '')
                        if txt:
                            text_parts.append(txt)
                    elif bt == 'tool_use':
                        tname = block.get('name', '?')
                        pending_tools[block.get('id', '')] = {
                            'name': tname,
                            'summary': _tool_input_summary(
                                tname, block.get('input', {})),
                            # Keep raw AskUserQuestion input for QA synthesis
                            '_raw_input': block.get('input', {}) if tname == 'AskUserQuestion' else None,
                        }
                if text_parts:
                    out_blocks.append('## ASSISTANT\n' + '\n'.join(text_parts) + '\n')

            elif t == 'user':
                msg_content = d.get('message', {}).get('content')
                if isinstance(msg_content, str):
                    text = msg_content.strip()
                    if not text:
                        next_is_skill = False
                    elif text.startswith('<command-'):
                        next_is_skill = True
                    elif text.startswith(('<local-command-', '<system-reminder>')):
                        next_is_skill = False
                    elif (event := _parse_system_event(text)) is not None:
                        # Keep system events as independent labeled export blocks so later LLM readers can distinguish actors
                        next_is_skill = False
                        label_map = {
                            'system_notification': 'NOTIFICATION',
                            'bash_output': 'BASH-OUTPUT',
                            'teammate_message': 'TEAMMATE',
                        }
                        label = label_map.get(event['type'], 'SYSTEM')
                        out_blocks.append(f'## {label}\n{event["text"]}\n')
                    else:
                        label = 'SKILL' if next_is_skill else 'USER'
                        next_is_skill = False
                        out_blocks.append(f'## {label}\n{text}\n')
                elif isinstance(msg_content, list):
                    user_texts = []
                    for block in msg_content:
                        if not isinstance(block, dict):
                            continue
                        bt = block.get('type')
                        if bt == 'tool_result':
                            tool_id = block.get('tool_use_id', '')
                            # AskUserQuestion becomes a ## QA block; answer is not truncated
                            if tool_id in pending_tools and pending_tools[tool_id]['name'] == 'AskUserQuestion':
                                tc = pending_tools.pop(tool_id)
                                qa_unit = _build_qa_unit(
                                    tc.get('_raw_input'),
                                    d.get('toolUseResult'))
                                if qa_unit:
                                    out_blocks.append(
                                        '## QA\n' + _format_qa_transcript(qa_unit) + '\n')
                                    continue
                                # fallback: treat input/result as a tool path, a rare defensive case
                                pending_tools[tool_id] = tc
                            raw = _tool_result_content(block.get('content', ''))
                            result = (raw or '').strip()
                            if len(result) > TRANSCRIPT_TOOL_RESULT_MAX:
                                n = TRANSCRIPT_TOOL_RESULT_MAX
                                lines = result.count('\n') + 1
                                result = (result[:n].rstrip() +
                                          f' …[+{len(result)-n} chars, ~{lines} lines]')
                            err_tag = ' [ERROR]' if block.get('is_error') else ''
                            if tool_id in pending_tools:
                                tc = pending_tools.pop(tool_id)
                                tc.pop('_raw_input', None)
                                head = f"[tool: {tc['name']}({tc['summary']})]{err_tag}"
                            else:
                                head = f"[tool: ?()]{err_tag}"
                            out_blocks.append(head + ('\n' + result + '\n' if result else '\n'))
                        elif bt == 'text':
                            txt = block.get('text', '')
                            if txt and not txt.lstrip().startswith('<system-reminder>'):
                                user_texts.append(txt)
                        elif bt == 'image':
                            user_texts.append('[image]')
                    if user_texts:
                        full = '\n'.join(user_texts).strip()
                        if full and not full.startswith(('<local-command-', '<command-')):
                            label = 'SKILL' if next_is_skill else 'USER'
                            next_is_skill = False
                            out_blocks.append(f'## {label}\n{full}\n')
                        else:
                            next_is_skill = False
                    else:
                        next_is_skill = False

    header = (f'# Session {jsonl_path.stem}\n'
              f'Project: {project_path or "(unknown)"}\n'
              f'Raw lines: {total_lines}\n\n')
    return header + '\n'.join(out_blocks)


def get_or_generate_brief(sid: str, jsonl_path: Path, transcript_text: str):
    """Core brief cache: reuse on content-hash (size+mtime) hit, regenerate otherwise.

    Returns (brief_text, status, generated_at_iso), where status is one of
    {'cached', 'generated', 'regenerated', 'failed'}. Failures are not cached. See the
    "Next steps" section of docs/decisions/2026-05-11-export-format.md.
    """
    st = jsonl_path.stat()
    size, mtime = st.st_size, st.st_mtime
    entry = _state.get(sid, {})
    cached = entry.get('brief')

    if (cached
            and cached.get('size_at_gen') == size
            and abs(cached.get('mtime_at_gen', 0) - mtime) < 0.001):
        return cached['text'], 'cached', cached.get('generated_at', '')

    is_regen = bool(cached)  # had a brief before, now stale -> regenerate
    text = generate_briefing(transcript_text)
    now_iso = datetime.now(timezone.utc).isoformat()

    # Do not cache failed [briefing ...] strings
    if text.startswith('[briefing '):
        return text, 'failed', now_iso

    entry = dict(entry)
    entry['brief'] = {
        'text': text,
        'size_at_gen': size,
        'mtime_at_gen': mtime,
        'generated_at': now_iso,
    }
    _state[sid] = entry
    save_state()
    return text, ('regenerated' if is_regen else 'generated'), now_iso


def generate_briefing(transcript_text: str) -> str:
    """Call local `claude -p` to generate a state-of-the-world briefing.

    Failures do not raise; return a [briefing ...: reason] string for the caller to include
    in output as-is. This lets the brief endpoint return the transcript body even if the LLM
    call fails.
    """
    prompt = (
        "Below is a Claude Code session log in compact form with system noise removed. "
        "Write a state-of-the-world briefing in 250 words or fewer so a fresh agent can "
        "resume the work immediately. Cover: 1) the task/goal; 2) key decisions and "
        "changes already made, including rationale; 3) the current state of relevant files "
        "or code; 4) the concrete next step. Output plain text only, with no markdown "
        "heading and no greeting."
        "\n\n--- SESSION LOG START ---\n" + transcript_text + "\n--- SESSION LOG END ---"
    )
    try:
        result = subprocess.run(
            ['claude', '-p', '--model', 'opus'],
            input=prompt, capture_output=True, text=True,
            timeout=BRIEF_TIMEOUT_SEC,
        )
        if result.returncode != 0:
            return f"[briefing failed rc={result.returncode}: {result.stderr[:200].strip()}]"
        brief = result.stdout.strip()
        return brief if brief else f"[briefing empty: {result.stderr[:200].strip()}]"
    except subprocess.TimeoutExpired:
        return f"[briefing TIMEOUT after {BRIEF_TIMEOUT_SEC}s]"
    except FileNotFoundError:
        return "[briefing failed: `claude` CLI not found in PATH]"
    except Exception as e:
        return f"[briefing exception: {e}]"


def _find_jsonl(session_id):
    """Locate a JSONL file by session ID, checking cache first and filesystem fallback next.

    When multiple files share a session_id, choose the largest one. Worktree ghost files are
    around 100B while real project copies are usually hundreds of KB; arbitrary traversal
    order can otherwise open the wrong modal. See _dedup_by_id.

    Codex fallback covers standalone-tab cold starts (`/?session=<id>`) where _cache has not
    been populated yet but the Codex session can be found on disk.
    """
    candidates = [m for m in _cache.values() if m.get('id') == session_id]
    candidates.sort(key=lambda m: m.get('size', 0), reverse=True)
    for meta in candidates:
        p = Path(meta['jsonl_path'])
        if p.exists():
            return p
    # Claude filesystem fallback
    if PROJECTS_DIR.exists():
        fs_candidates = []
        for pd in PROJECTS_DIR.iterdir():
            if not pd.is_dir():
                continue
            c = pd / f'{session_id}.jsonl'
            if c.exists():
                try:
                    fs_candidates.append((c.stat().st_size, c))
                except FileNotFoundError:
                    continue
        if fs_candidates:
            fs_candidates.sort(reverse=True)
            return fs_candidates[0][1]
    # Codex filesystem fallback: first direct-hit by session_meta.id; if that misses, follow
    # fork lineage downward because a Codex Desktop parent thread root has no rollout file of
    # its own. Search active and archived roots so archived sessions still work on standalone
    # cold starts. If nothing is found, return None rather than constructing a nonexistent path.
    p, _forked_child = codex_source.find_rollout_by_session_id(session_id)
    if p is not None:
        return p
    # Antigravity filesystem fallback; id is the brain directory name
    ag_tp = ag_source.AG_BRAIN / session_id / ".system_generated" / "logs" / "transcript.jsonl"
    if ag_tp.exists():
        return ag_tp
    return None


# ---------- Scope model (v2) ----------
def _effective_archived(session_meta, state_entry):
    """Final archived decision for a session.

    Priority: explicit archived value in dashboard state > codex_archived derived from Codex
    file location. If the user never touched archive state in the dashboard, fall back to
    codex_archived so sessions archived in Codex land in Archived by default. If the user
    manually archives or unarchives in the dashboard, the explicit True/False state overrides
    the derived value, so manually unarchiving a Codex-archived session keeps it active.
    """
    entry = state_entry or {}
    if "archived" in entry:
        return bool(entry["archived"])
    return bool((session_meta or {}).get("codex_archived"))


def compute_scope(session_meta, state_entry, now=None):
    """Compute a session's section from session meta plus user state.

    Priority: archived > starred > mtime-based (recent/dusty).
    """
    if now is None:
        import time as _time
        now = _time.time()
    entry = state_entry or {}
    if _effective_archived(session_meta, entry):
        return "archived"
    if entry.get("starred"):
        return "starred"
    mtime = session_meta.get("mtime", 0)
    cutoff = now - DUSTY_AFTER_DAYS * 86400
    if mtime >= cutoff:
        return "recent"
    return "dusty"


# ---------- Scanning ----------
def scan_sessions(force=False):
    """Scan all Claude + Codex sessions, update cache incrementally, and return items by descending mtime."""
    if force:
        if _cache or _CWD_TRUTH_MAP or _CWD_INDEX_SEEN or _CWD_SEQ:
            _mark_scan_cache_dirty()
        _cache.clear()
        _CWD_TRUTH_MAP.clear()
        _CWD_INDEX_SEEN.clear()
        _CWD_SEQ.clear()
        _DECODE_DIR_CACHE.clear()

    # --- Claude paths ---
    claude_seen = set()
    if PROJECTS_DIR.exists():
        # Pass 1: prebuild the cwd reverse table incrementally, skipping jsonl files already
        # peeked. This must come first so later extract_metadata fallback can see ground truth;
        # otherwise a cwd-less orphan worktree session can be disambiguated by filesystem and
        # cached under the wrong path.
        _build_cwd_map(force)

        for project_dir in PROJECTS_DIR.iterdir():
            if not project_dir.is_dir():
                continue
            for jsonl_path in project_dir.glob("*.jsonl"):
                key = str(jsonl_path)
                claude_seen.add(key)
                try:
                    cur_mtime = jsonl_path.stat().st_mtime
                except FileNotFoundError:
                    continue
                cached = _cache.get(key)
                if force or cached is None or cached.get("mtime") != cur_mtime:
                    meta = extract_metadata(jsonl_path)
                    if meta and _cache.get(key) != meta:
                        _cache[key] = meta
                        _mark_scan_cache_dirty()

    # --- Codex paths ---
    codex_seen = set()
    for jsonl_path in codex_source.scan_sessions():
        key = str(jsonl_path)
        codex_seen.add(key)
        try:
            cur_mtime = jsonl_path.stat().st_mtime
        except FileNotFoundError:
            continue
        cached = _cache.get(key)
        if force or cached is None or cached.get("mtime") != cur_mtime:
            meta = codex_source.extract_metadata(jsonl_path)
            if meta and _cache.get(key) != meta:
                _cache[key] = meta
                _mark_scan_cache_dirty()

    # --- Antigravity paths ---
    ag_seen = set()
    for jsonl_path in ag_source.scan_sessions():
        key = str(jsonl_path)
        ag_seen.add(key)
        try:
            cur_mtime = jsonl_path.stat().st_mtime
        except FileNotFoundError:
            continue
        cached = _cache.get(key)
        if force or cached is None or cached.get("mtime") != cur_mtime:
            meta = ag_source.extract_metadata(jsonl_path)
            if meta and _cache.get(key) != meta:
                _cache[key] = meta
                _mark_scan_cache_dirty()

    # --- Clean stale cache keys per root prefix so sources do not delete each other. ---
    # Codex uses is_codex_path to recognize both roots (sessions + archived_sessions); otherwise
    # stale keys under the archived root would never be removed because they are not under CODEX_ROOT.
    projects_root_str = str(PROJECTS_DIR)
    ag_root_str = str(ag_source.AG_BRAIN)
    cwd_index_removed = False
    for p in list(_cache.keys()):
        if p.startswith(projects_root_str) and p not in claude_seen:
            del _cache[p]
            _mark_scan_cache_dirty()
            cwd_index_removed = _remove_cwd_index_for_path(p) or cwd_index_removed
        elif codex_source.is_codex_path(p) and p not in codex_seen:
            del _cache[p]
            _mark_scan_cache_dirty()
        elif p.startswith(ag_root_str) and p not in ag_seen:
            del _cache[p]
            _mark_scan_cache_dirty()
    for p in list(_CWD_SEQ.keys()):
        if p.startswith(projects_root_str) and p not in claude_seen:
            cwd_index_removed = _remove_cwd_index_for_path(p) or cwd_index_removed
    for p in list(_CWD_INDEX_SEEN):
        if p.startswith(projects_root_str) and p not in claude_seen:
            cwd_index_removed = _remove_cwd_index_for_path(p) or cwd_index_removed
    if cwd_index_removed:
        _rebuild_cwd_truth_map_from_seq()

    save_scan_cache()
    return _dedup_by_id(sorted(_cache.values(), key=lambda m: m["mtime"], reverse=True))


def _dedup_by_id(metas):
    """When multiple files share a session_id, keep only the largest substantial copy.

    In the EnterWorktree flow, Claude Code can create a ~116B placeholder jsonl in the
    worktree-encoded directory containing only aiTitle and no cwd, while the real
    conversation continues in the main project session directory. With the same session_id,
    the dashboard would show two cards: a full main-project card and an empty worktree ghost,
    and _find_jsonl would be ambiguous. In observed data, ghosts were ~100B while real copies
    were 200KB+, so size comparison is a robust discriminator.
    """
    by_id = {}
    for m in metas:
        sid = m.get("id")
        if not sid:
            continue
        prev = by_id.get(sid)
        if prev is None or m.get("size", 0) > prev.get("size", 0):
            by_id[sid] = m
    # Preserve original order (descending mtime)
    seen_ids = set()
    result = []
    for m in metas:
        sid = m.get("id")
        if sid in seen_ids:
            continue
        winner = by_id.get(sid)
        if winner is m:
            result.append(m)
            seen_ids.add(sid)
    return result


def enriched_sessions():
    """Return all sessions without filtering, adding scope/archive/star/note fields.

    Also backfill source / cli_version / model for Claude-path metadata; Codex already
    includes them. The frontend labels cards by source.
    """
    items = scan_sessions()
    import time as _time
    now = _time.time()
    result = []
    for m in items:
        sid = m["id"]
        st = _state.get(sid, {})
        scope = compute_scope(m, st, now=now)
        item = dict(m)
        item.setdefault("source", "claude")
        item.setdefault("cli_version", None)
        item.setdefault("model", None)
        item["scope"] = scope
        item["archived"] = _effective_archived(m, st)
        item["archived_at"] = st.get("archived_at")
        item["starred"] = bool(st.get("starred"))
        item["starred_at"] = st.get("starred_at")
        item["note"] = st.get("note", "")
        result.append(item)
    return result


# ---------- Full-text search ----------
_search_fallback_warnings = set()


def _warn_search_fallback(reason: str):
    """Report a degraded search path once instead of silently becoming slow."""
    if reason in _search_fallback_warnings:
        return
    _search_fallback_warnings.add(reason)
    print(
        f"[warn] full-text search: {reason}; using slower Python fallback",
        file=sys.stderr,
    )


def _find_ripgrep():
    """Resolve ripgrep in shells and restricted GUI/background environments.

    Hammerspoon and launchd do not inherit the user's interactive-shell PATH. Prefer PATH
    when available, then probe the standard Homebrew locations used on macOS. Return an
    absolute executable path so subprocesses do not depend on their caller's environment.
    """
    found = shutil.which("rg")
    if found:
        return found
    for candidate in RIPGREP_FALLBACK_PATHS:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _search_session(jsonl_path: Path, terms: list[str]):
    """Full-text search one session by scanning user/assistant messages and returning snippets.

    terms is a lowercase search-term list with AND semantics: every term must appear in the
    same session to count as a hit. Return [] for no match.
    """
    # Claude filename = session id (jsonl_path.stem is the UUID). Codex filenames are
    # rollout-<date>-<uuid>.jsonl, so stem is not the ID shown on the card. Read it from
    # session_meta.payload.id, otherwise pasted Codex card IDs miss id_hit and fallback
    # snippets show the wrong Session ID.
    is_codex = codex_source.is_codex_path(jsonl_path)
    is_ag = str(jsonl_path).startswith(str(ag_source.AG_BRAIN))
    if is_codex:
        meta = codex_source._read_session_meta(jsonl_path)
        session_id = (meta or {}).get('id') or jsonl_path.stem
    elif is_ag:
        # Antigravity filenames are transcript.jsonl; id is the brain/<id>/ directory name
        session_id = ag_source._conv_id(jsonl_path)
    else:
        session_id = jsonl_path.stem

    # 1) Session ID match: any term matching the ID returns a snippet
    id_lower = session_id.lower()
    id_hit = any(t in id_lower for t in terms)

    # 2) Scan JSONL and collect all text lines
    snippets = []
    term_found = set()  # track which terms were found

    # Cheap line-level prefilter: substring-match the raw line first and skip expensive
    # json.loads for lines that cannot match. Body text is contained in the raw line, so if
    # the raw line lacks a term, the body cannot contain it either. This is only safe when
    # terms do not contain JSON-escaped characters; rare queries containing those characters
    # disable the prefilter and parse every line for correctness. This helps common high-
    # frequency terms that rg cannot narrow at file level but most lines still do not contain.
    prefilter_safe = not any(c in t for t in terms for c in '"\\\n\r\t')

    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if prefilter_safe:
                    line_lower = line.lower()
                    if not any(term in line_lower for term in terms):
                        continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue

                t = d.get("type")
                text = ""
                if t == "user":
                    text = _user_text(d.get("message", {}).get("content"))
                elif t == "assistant":
                    text = _assistant_text(d.get("message", {}).get("content"))
                elif t == "response_item":
                    # Codex row: extract message.role=user/assistant text and search it too
                    p = d.get("payload") or {}
                    if p.get("type") == "message" and p.get("role") in ("user", "assistant"):
                        text = codex_source._extract_text_from_message_content(p.get("content"))
                elif t in ("USER_INPUT", "PLANNER_RESPONSE"):
                    # Antigravity row: extract user/assistant text and search it too
                    text = ag_source.search_text_from_line(d)

                if not text:
                    continue

                text_lower = text.lower()
                for term in terms:
                    if term in text_lower:
                        term_found.add(term)

                # Find the match position and extract a snippet
                if len(snippets) < SEARCH_MAX_SNIPPETS:
                    for term in terms:
                        idx = text_lower.find(term)
                        if idx >= 0:
                            start = max(0, idx - SEARCH_SNIPPET_CONTEXT)
                            end = min(len(text), idx + len(term) + SEARCH_SNIPPET_CONTEXT)
                            snippet = text[start:end].strip()
                            snippet = re.sub(r"\s+", " ", snippet)
                            prefix = "…" if start > 0 else ""
                            suffix = "…" if end < len(text) else ""
                            role = "you" if t == "user" else ""
                            snippets.append({
                                "text": prefix + snippet + suffix,
                                "role": role,
                                "term": term,
                            })
                            break  # take one snippet per line

                # Stop early once all terms are found and the snippet quota is full
                if len(term_found) == len(terms) and len(snippets) >= SEARCH_MAX_SNIPPETS:
                    break

    except Exception as e:
        print(f"[warn] search read failed {jsonl_path}: {e}", file=sys.stderr)
        return []

    # AND semantics: unless the ID matched, every term must appear in text
    if id_hit:
        return snippets or [{"text": f"Session ID: {session_id}", "role": "", "term": ""}]
    if len(term_found) < len(terms):
        return []
    return snippets


def _rg_prefilter(terms, paths):
    """Use ripgrep to quickly prefilter file paths whose content contains every term.

    rg searches raw file bytes, so the hit set is a superset of true text hits: terms may
    appear only in tool output or JSON keys. Final correctness is enforced by downstream
    _search_session; rg only narrows thousands of files to a candidate set and avoids JSON
    parsing for files that cannot match.

    No-miss proof: user/assistant text is contained in raw file content, so every file that
    _search_session can match in message text must contain the term in raw content and will
    be selected by rg -l. Session-id matches are handled separately by the caller.

    Return set[str] path strings, or None when rg is unavailable/fails so the caller can
    fall back to a full scan.
    """
    rg_executable = _find_ripgrep()
    if not rg_executable:
        _warn_search_fallback("ripgrep executable not found")
        return None
    path_strs = [str(p) for p in paths]
    candidate = None  # None = unconstrained so far; intersect per term to implement AND
    for term in terms:
        # -i is case-insensitive and -F is literal matching, aligned with _search_session lower()+substring semantics
        cmd = [rg_executable, "-l", "-i", "-F", "--no-messages", "--", term, *path_strs]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=20)
        except subprocess.TimeoutExpired:
            _warn_search_fallback("ripgrep prefilter timed out")
            return None
        except (FileNotFoundError, OSError) as e:
            _warn_search_fallback(f"ripgrep prefilter could not start ({type(e).__name__})")
            return None  # includes ARG_MAX overflow (E2BIG); fall back to full scan
        # rg exit codes: 0=matches, 1=no matches (both normal), >=2=real error
        if r.returncode not in (0, 1):
            _warn_search_fallback(f"ripgrep prefilter exited with status {r.returncode}")
            return None
        hit = {ln for ln in r.stdout.decode("utf-8", "replace").splitlines() if ln}
        candidate = hit if candidate is None else (candidate & hit)
        if not candidate:
            break  # one term already has no intersection, so AND result is empty
    return candidate or set()


def search_sessions(query: str):
    """Full-text search all sessions. Return [{id, snippets}, ...] sorted by existing cache mtime descending."""
    terms = [t.lower() for t in query.split() if t]
    if not terms:
        return []

    # Prefer the known session list in cache (descending mtime), with disk scan fallback
    if not _cache:
        scan_sessions()

    # (key, meta) list matching the original traversal order
    ordered = sorted(_cache.items(), key=lambda kv: kv[1].get("mtime", 0), reverse=True)

    # ripgrep prefilter: select files whose content contains every term and skip expensive
    # per-line JSON parsing for the rest. prefiltered=None means rg is unavailable, so scan
    # every file as before.
    prefiltered = _rg_prefilter(terms, [Path(m["jsonl_path"]) for _, m in ordered])

    results = []
    for key, meta in ordered:
        jsonl_path = Path(meta["jsonl_path"])
        if not jsonl_path.exists():
            continue
        if prefiltered is not None and str(jsonl_path) not in prefiltered:
            # Content does not contain all terms. The only exception is a term matching the
            # session id itself, because rg searches content and does not cover pure id-substring
            # search. Use meta.id plus filename stem as an id fallback, covering Claude
            # (id=stem) and Codex (id in meta). Prefer one extra downstream check over skipping
            # a true hit.
            id_blob = (meta.get("id", "") + " " + jsonl_path.stem).lower()
            if not any(t in id_blob for t in terms):
                continue
        snippets = _search_session(jsonl_path, terms)
        if snippets:
            results.append({"id": meta["id"], "snippets": snippets})

    return results


# ---------- Recent files (project-level) ----------
# Exclude list for non-git-repo fallback traversal
_FALLBACK_EXCLUDE_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    ".next", ".nuxt", "dist", "build", "out", ".turbo",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache",
    ".gradle", ".idea", ".vscode-test", "target",
}
_FALLBACK_EXCLUDE_FILES = {".DS_Store"}


def _resolve_known_project_root(root: str):
    """Resolve root only when it belongs to a project discovered from a session.

    The Files panel intentionally reads project files, but an HTTP caller must not be
    able to replace that project root with an arbitrary directory such as the user's
    home directory. Return the canonical Path or None when it is not in the session
    cache.
    """
    if not _cache:
        scan_sessions()
    for meta in _cache.values():
        project_path = meta.get("project_path")
        # The frontend receives this exact value from session metadata and returns it.
        # Do not construct a Path from the HTTP value; select the trusted cached value.
        if not project_path or root != project_path:
            continue
        try:
            return Path(project_path).resolve()
        except (OSError, RuntimeError):
            return None
    return None


def _stat_safe(p: Path):
    try:
        return p.stat()
    except OSError:
        return None


def _get_file_status_map(root_path: Path):
    """Run `git status --porcelain=v1 -b -z` to get per-file status.

    Return (branch, {rel_path: status_char}). For non-git repos or command failures, return
    (None, {}). Status character rules:
      - `XY == '??'` -> '??' (untracked)
      - `XY == '!!'` -> skip ignored files
      - X != ' ' (including A/M/D/R/C) wins as staged status
      - otherwise use Y as unstaged status
    Rename/copy records (X or Y is R/C) are followed by a NUL-separated from-path; skip it.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(root_path), "status", "--porcelain=v1", "-b", "-z"],
            capture_output=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None, {}
    if result.returncode != 0:
        return None, {}

    parts = result.stdout.split(b"\0")
    branch = None
    if parts and parts[0].startswith(b"## "):
        head = parts[0][3:].decode("utf-8", errors="replace")
        # "main...origin/main [ahead 1, behind 2]", "main", or "HEAD (no branch)"
        m = re.match(r"^([^.\s]+)", head)
        if m:
            branch = m.group(1)
        records = parts[1:]
    else:
        records = parts

    status_map = {}
    skip_next = False
    for r in records:
        if skip_next:
            skip_next = False
            continue
        if len(r) < 3:
            continue
        x, y = chr(r[0]), chr(r[1])
        rel = r[3:].decode("utf-8", errors="replace")
        if x in ("R", "C") or y in ("R", "C"):
            # rename / copy: next NUL record is from-path; skip it
            skip_next = True
        xy = x + y
        if xy == "!!":
            continue  # ignored; skip as requested
        if xy == "??":
            status_map[rel] = "??"
            continue
        # staged wins: X column is non-empty
        if x != " ":
            status_map[rel] = x
        else:
            status_map[rel] = y

    return branch, status_map


def list_recent_files(root: str, limit: int = 50, include_ignored: bool = False):
    """List recently modified files under root, honoring .gitignore by default.

    Prefer `git ls-files` to collect tracked and untracked files. On failure or outside a git
    repo, fall back to os.walk with the exclude list. Merge git status markers in the same pass.

    Arguments:
      include_ignored=False (default): ls-files uses --exclude-standard and honors gitignore.
      include_ignored=True: omit --exclude-standard so ignored files appear, then filter obvious
                            noisy directories with _FALLBACK_EXCLUDE_DIRS.

    Return {root, git, branch, files:[{rel, path, mtime, size, status}, ...]}.
    """
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        return {
            "root": str(root_path), "git": False, "branch": None,
            "files": [], "error": "root not a directory",
        }

    # Get the per-file status map first; command failure is non-fatal and leaves the map empty
    branch, status_map = _get_file_status_map(root_path)

    files = []
    used_git = False
    try:
        ls_cmd = ["git", "-C", str(root_path), "ls-files", "--cached", "--others"]
        if not include_ignored:
            ls_cmd.append("--exclude-standard")
        ls_cmd.append("-z")
        result = subprocess.run(ls_cmd, capture_output=True, timeout=10)
        if result.returncode == 0:
            used_git = True
            seen = set()
            for rel_b in result.stdout.split(b"\0"):
                if not rel_b:
                    continue
                rel = rel_b.decode("utf-8", errors="replace")
                # In include_ignored mode, run the Python-side exclude list to avoid node_modules/.next noise
                if include_ignored and any(
                    seg in _FALLBACK_EXCLUDE_DIRS for seg in rel.split("/")
                ):
                    continue
                full = root_path / rel
                st = _stat_safe(full)
                if not st or not full.is_file():
                    continue
                seen.add(rel)
                files.append({
                    "rel": rel,
                    "path": str(full),
                    "mtime": st.st_mtime,
                    "size": st.st_size,
                    "status": status_map.get(rel, ""),
                })
            # Add deleted files present in status_map but missing from ls-files (deleted worktree / staged delete)
            if status_map:
                import time as _time
                now = _time.time()
                for rel, status in status_map.items():
                    if rel in seen:
                        continue
                    if status != "D":
                        continue
                    full = root_path / rel
                    files.append({
                        "rel": rel,
                        "path": str(full),
                        "mtime": now,
                        "size": 0,
                        "status": "D",
                    })
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    if not used_git:
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames[:] = [d for d in dirnames if d not in _FALLBACK_EXCLUDE_DIRS]
            for fn in filenames:
                if fn in _FALLBACK_EXCLUDE_FILES:
                    continue
                full = Path(dirpath) / fn
                st = _stat_safe(full)
                if not st:
                    continue
                rel = str(full.relative_to(root_path))
                files.append({
                    "rel": rel,
                    "path": str(full),
                    "mtime": st.st_mtime,
                    "size": st.st_size,
                    "status": status_map.get(rel, ""),
                })

    files.sort(key=lambda x: x["mtime"], reverse=True)
    return {
        "root": str(root_path),
        "git": used_git,
        "branch": branch if used_git else None,
        "files": files[:limit],
    }


def find_files_by_name(root: str, query: str, limit: int = 300, include_ignored: bool = False):
    """Search for files by filename across the whole root subtree using fd.

    This aligns ignored-file semantics with list_recent_files. It is separate from Recent
    files, which lists by mtime; this answers "I know a file named xxx exists, find it in
    this tree" and avoids awkward Finder filename filtering.

    Matching rules:
      - fd matches basename by default, i.e. filename search. If query contains `/`, switch
        to --full-path so directory/name fragments such as `docs/decision` can match.
      - --fixed-strings treats query literally, so `.` in `server.py` is not a regex wildcard.
      - smart case: all-lowercase query is case-insensitive; uppercase makes it sensitive.

    Ignored semantics match list_recent_files:
      - include_ignored=False (default): honor .gitignore; --hidden includes tracked dotfiles,
        and fd skips .git automatically.
      - include_ignored=True: use --no-ignore and explicit _FALLBACK_EXCLUDE_DIRS excludes for
        noisy directories such as node_modules/.next/__pycache__, matching recent-files.

    Return {root, query, files:[{rel, path, mtime, size, status}], matched, truncated, error?}.
    status reuses the git status map for visual consistency with recent-files; empty outside git.
    """
    root_path = Path(root).resolve()
    q = (query or "").strip()
    if not root_path.is_dir():
        return {"root": str(root_path), "query": q, "files": [],
                "matched": 0, "truncated": False, "error": "root not a directory"}
    if not q:
        return {"root": str(root_path), "query": "", "files": [],
                "matched": 0, "truncated": False}

    cmd = ["fd", "--type", "f", "--hidden", "--color", "never",
           "--absolute-path", "--fixed-strings"]
    if "/" in q:
        cmd.append("--full-path")
    if include_ignored:
        cmd.append("--no-ignore")
        for d in _FALLBACK_EXCLUDE_DIRS:
            cmd += ["--exclude", d]
    else:
        cmd += ["--exclude", ".git"]  # redundant guard; fd also skips .git by default
    cmd += ["--", q, str(root_path)]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=10)
    except FileNotFoundError:
        return {"root": str(root_path), "query": q, "files": [],
                "matched": 0, "truncated": False,
                "error": "fd not installed (brew install fd)"}
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"root": str(root_path), "query": q, "files": [],
                "matched": 0, "truncated": False, "error": f"fd failed: {e}"}

    # fd exit code: 0 is normal, including zero hits; nonzero with no output is an error
    if result.returncode not in (0,) and not result.stdout:
        err = result.stderr.decode("utf-8", errors="replace").strip()
        return {"root": str(root_path), "query": q, "files": [],
                "matched": 0, "truncated": False,
                "error": err or f"fd exit {result.returncode}"}

    # Reuse recent-files per-file git status for visual consistency
    _branch, status_map = _get_file_status_map(root_path)

    files = []
    for line in result.stdout.split(b"\n"):
        if not line:
            continue
        full_s = line.decode("utf-8", errors="replace")
        full = Path(full_s)
        st = _stat_safe(full)
        if not st or not full.is_file():
            continue
        try:
            rel = str(full.relative_to(root_path))
        except ValueError:
            rel = full.name
        files.append({
            "rel": rel,
            "path": full_s,
            "mtime": st.st_mtime,
            "size": st.st_size,
            "status": status_map.get(rel, ""),
        })

    matched = len(files)
    files.sort(key=lambda x: x["mtime"], reverse=True)
    return {
        "root": str(root_path),
        "query": q,
        "files": files[:limit],
        "matched": matched,
        "truncated": matched > limit,
    }


def open_file_in_system(file_path: str, root: str, reveal: bool = False) -> tuple[bool, str]:
    """Open a file with macOS `open`, requiring file_path to stay inside root.

    With reveal=True, use `open -R` to highlight the file in Finder rather than opening it
    with the default app. Return (ok, error_msg).
    """
    try:
        root_real = Path(root).resolve()
        file_real = Path(file_path).resolve()
    except OSError as e:
        return False, f"resolve failed: {e}"

    if not file_real.exists():
        return False, "file not found"
    try:
        file_real.relative_to(root_real)
    except ValueError:
        return False, "path escapes root"

    cmd = ["open", "-R", str(file_real)] if reveal else ["open", str(file_real)]
    try:
        subprocess.Popen(cmd)
        return True, ""
    except (FileNotFoundError, OSError) as e:
        return False, f"open failed: {e}"


# ---------- PWA static assets (inline constants, keeping the single-file style) ----------
PWA_MANIFEST = json.dumps({
    "name": "Session Logbook",
    "short_name": "Logbook",
    "description": "A minimal, local dashboard for browsing your Claude Code, Codex, and Antigravity agent sessions.",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#f7f7f5",
    "theme_color": "#1a1a1a",
    "icons": [
        {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
        {"src": "/icon-maskable.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "maskable"},
    ],
}, ensure_ascii=False)

# Regular icon: reuse the favicon visual language of stacked cards plus an orange notification dot on a 512x512 canvas
PWA_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <rect width="512" height="512" rx="96" fill="#f7f7f5"/>
  <g transform="translate(72,72)">
    <rect x="0" y="120" width="288" height="240" rx="36" fill="#1a1a1a" opacity="0.25"/>
    <rect x="32" y="72" width="288" height="240" rx="36" fill="#1a1a1a" opacity="0.55"/>
    <rect x="64" y="24" width="288" height="240" rx="36" fill="#1a1a1a"/>
    <rect x="112" y="88" width="160" height="22" rx="11" fill="#ffffff" opacity="0.92"/>
    <rect x="112" y="136" width="200" height="22" rx="11" fill="#ffffff" opacity="0.55"/>
    <rect x="112" y="184" width="110" height="22" rx="11" fill="#ffffff" opacity="0.35"/>
  </g>
  <circle cx="408" cy="104" r="44" fill="#d97706"/>
</svg>"""

# Maskable icon: keep content inside the central ~62% safe area for Android adaptive masks.
# Remove the orange notification dot so it is not clipped by masks, and use a dark background
# to distinguish it from the regular icon.
PWA_ICON_MASKABLE_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <rect width="512" height="512" fill="#1a1a1a"/>
  <g transform="translate(128,128)">
    <rect x="0" y="80" width="240" height="192" rx="32" fill="#ffffff" opacity="0.20"/>
    <rect x="16" y="48" width="240" height="192" rx="32" fill="#ffffff" opacity="0.38"/>
    <rect x="32" y="16" width="240" height="192" rx="32" fill="#ffffff"/>
    <rect x="64" y="64" width="140" height="20" rx="10" fill="#1a1a1a" opacity="0.85"/>
    <rect x="64" y="104" width="176" height="20" rx="10" fill="#1a1a1a" opacity="0.55"/>
    <rect x="64" y="144" width="96" height="20" rx="10" fill="#1a1a1a" opacity="0.35"/>
  </g>
</svg>"""

# Minimal service worker: only satisfies Chrome install heuristics; no caching because session lists must stay live
PWA_SW_JS = """// Do not cache anything; fetch does not call respondWith, so the browser uses normal network behavior
self.addEventListener('install', () => { self.skipWaiting(); });
self.addEventListener('activate', (e) => { e.waitUntil(self.clients.claim()); });
self.addEventListener('fetch', () => {});
"""


def _vendor_content_type(path: Path) -> str:
    if path.suffix == ".map":
        return "application/json; charset=utf-8"
    if path.suffix == ".js":
        return "application/javascript; charset=utf-8"
    if path.suffix == ".woff2":
        return "font/woff2"
    return "text/plain; charset=utf-8"


def _port_arg(value: str) -> int:
    try:
        port = int(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError("port must be an integer") from e
    if not (1 <= port <= 65535):
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Run the local Session Logbook dashboard."
    )
    parser.add_argument(
        "--port",
        type=_port_arg,
        default=PORT,
        help=f"local port to listen on (default: {PORT})",
    )
    return parser.parse_args(argv)


# ---------- HTTP Handler ----------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # quiet logs; enable only when debugging

    def _safe_write(self, body):
        # When the client aborts an old request (debounce / AbortController / closed tab),
        # wfile.write raises BrokenPipe / ConnectionReset. This is harmless; swallow it to
        # avoid noisy logs.
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return
        self._safe_write(body)

    def _send_bytes(self, code, body, ctype, extra_headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if extra_headers:
                for k, v in extra_headers.items():
                    self.send_header(k, v)
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return
        self._safe_write(body)

    def _read_json(self):
        content_type = self.headers.get("Content-Type", "").partition(";")[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("Content-Type must be application/json")
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        if length > MAX_JSON_BODY:
            raise ValueError("request body too large")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _request_is_trusted(self):
        return _is_trusted_http_request(
            self.headers.get("Host", ""),
            self.headers.get("Origin", ""),
            self.headers.get("Sec-Fetch-Site", ""),
        )

    def do_GET(self):
        if not self._request_is_trusted():
            return self._send_json(403, {"error": "cross-site request rejected"})
        # Ensure _state has been loaded from disk. Idempotent: only the first call reads.
        # Protects startup paths such as `import server; ThreadingHTTPServer(..., server.Handler)`
        # that bypass main().
        load_state()
        url = urllib.parse.urlparse(self.path)
        path = url.path
        qs = urllib.parse.parse_qs(url.query)

        if path == "/":
            if not INDEX_HTML.exists():
                return self._send_bytes(404, "index.html not found", "text/plain; charset=utf-8")
            return self._send_bytes(200, INDEX_HTML.read_bytes(), "text/html; charset=utf-8")

        # PWA resources: manifest / icon / service worker
        if path == "/manifest.webmanifest":
            return self._send_bytes(200, PWA_MANIFEST, "application/manifest+json; charset=utf-8")
        if path == "/icon.svg":
            return self._send_bytes(200, PWA_ICON_SVG, "image/svg+xml")
        if path == "/icon-maskable.svg":
            return self._send_bytes(200, PWA_ICON_MASKABLE_SVG, "image/svg+xml")
        if path == "/sw.js":
            # The service worker must control /; root registration needs no Service-Worker-Allowed header
            return self._send_bytes(
                200, PWA_SW_JS, "application/javascript; charset=utf-8",
                extra_headers={"Cache-Control": "no-cache"},
            )

        if path.startswith("/vendor/"):
            rel = urllib.parse.unquote(path[len("/vendor/"):])
            vendor_path = (VENDOR_DIR / rel).resolve()
            try:
                vendor_path.relative_to(VENDOR_DIR.resolve())
            except ValueError:
                return self._send_bytes(404, "Not found", "text/plain; charset=utf-8")
            if not vendor_path.is_file():
                return self._send_bytes(404, "Not found", "text/plain; charset=utf-8")
            return self._send_bytes(
                200,
                vendor_path.read_bytes(),
                _vendor_content_type(vendor_path),
                extra_headers={"Cache-Control": "no-cache"},
            )

        if path == "/api/sessions":
            return self._send_json(200, enriched_sessions())

        if path == "/api/search":
            q = (qs.get("q") or [""])[0].strip()
            if not q:
                return self._send_json(200, [])
            return self._send_json(200, search_sessions(q))

        if path == "/api/stats":
            import time as _time
            items = scan_sessions()
            now = _time.time()
            counts = {"starred": 0, "recent": 0, "dusty": 0, "archived": 0}
            for m in items:
                sc = compute_scope(m, _state.get(m["id"], {}), now=now)
                counts[sc] = counts.get(sc, 0) + 1
            counts["total"] = len(items)
            return self._send_json(200, counts)

        cm = re.match(r"^/api/sessions/([^/]+)/conversation$", path)
        if cm:
            sid = cm.group(1)
            jsonl = _find_jsonl(sid)
            if not jsonl:
                return self._send_json(404, {"error": "Session not found"})
            try:
                if codex_source.is_codex_path(jsonl):
                    conv = codex_source.extract_conversation(jsonl)
                elif str(jsonl).startswith(str(ag_source.AG_BRAIN)):
                    conv = ag_source.extract_conversation(jsonl)
                else:
                    conv = extract_conversation(jsonl)
                return self._send_json(200, conv)
            except Exception as e:
                return self._send_json(500, {"error": str(e)})

        tm = re.match(r"^/api/sessions/([^/]+)/transcript$", path)
        if tm:
            sid = tm.group(1)
            jsonl = _find_jsonl(sid)
            if not jsonl:
                return self._send_bytes(404, "Session not found", "text/plain; charset=utf-8")
            try:
                if codex_source.is_codex_path(jsonl):
                    text = codex_source.extract_transcript(jsonl)
                elif str(jsonl).startswith(str(ag_source.AG_BRAIN)):
                    text = ag_source.extract_transcript(jsonl)
                else:
                    text = extract_transcript(jsonl)
                brief_param = (qs.get('brief') or ['0'])[0].lower()
                headers = {}
                if brief_param in ('1', 'true', 'yes'):
                    brief, status, when = get_or_generate_brief(sid, jsonl, text)
                    text = (
                        "=== HANDOFF BRIEFING (generated by claude -p) ===\n"
                        f"{brief}\n"
                        "=== /BRIEFING ===\n\n"
                        "=== RAW TRANSCRIPT (compact) ===\n"
                        f"{text}"
                    )
                    headers['X-Brief-Status'] = status
                    headers['X-Brief-Generated-At'] = when
                return self._send_bytes(
                    200, text, "text/plain; charset=utf-8",
                    extra_headers=headers,
                )
            except Exception as e:
                return self._send_bytes(500, f"Error: {e}", "text/plain; charset=utf-8")

        # Downloadable compact version: a navigable transcript with original line anchors,
        # meant for agents to read and expand back into the source as needed. It serves a
        # different consumer from /transcript, which is human-readable export markdown; see
        # docs/philosophy.md on context reduction for whom. Both sources share
        # sources/anchored_transcript with the offline pipeline.
        am = re.match(r"^/api/sessions/([^/]+)/anchored$", path)
        if am:
            sid = am.group(1)
            jsonl = _find_jsonl(sid)
            if not jsonl:
                return self._send_bytes(404, "Session not found", "text/plain; charset=utf-8")
            try:
                # Self-describing header so a cold recipient, such as another agent without
                # repository context, can understand anchor semantics and source positions from
                # this .txt alone. Only prepend it to the download artifact, not render body,
                # preserving byte-level parity with the offline pipeline.
                if codex_source.is_codex_path(jsonl):
                    src = "codex"
                    body = anchored_transcript.render_codex(jsonl)
                else:
                    src = "claude"
                    body = anchored_transcript.render_claude(jsonl)
                text = anchored_transcript.digest_header(jsonl, src) + "\n\n" + body
                fname = f"{sid}.anchored.txt"
                return self._send_bytes(
                    200, text, "text/plain; charset=utf-8",
                    extra_headers={
                        "Content-Disposition": f'attachment; filename="{fname}"',
                        "Cache-Control": "no-cache",
                    },
                )
            except Exception as e:
                return self._send_bytes(500, f"Error: {e}", "text/plain; charset=utf-8")

        if path == "/api/recent-files":
            root = (qs.get("root") or [""])[0]
            if not root:
                return self._send_json(400, {"error": "missing root"})
            root_path = _resolve_known_project_root(root)
            if root_path is None:
                return self._send_json(403, {"error": "unknown project root"})
            try:
                limit = int((qs.get("limit") or ["50"])[0])
            except ValueError:
                limit = 50
            limit = max(1, min(limit, 500))
            inc_raw = (qs.get("include_ignored") or ["0"])[0].lower()
            include_ignored = inc_raw in ("1", "true", "yes")
            try:
                return self._send_json(
                    200, list_recent_files(str(root_path), limit, include_ignored=include_ignored)
                )
            except Exception as e:
                return self._send_json(500, {"error": str(e)})

        if path == "/api/find-files":
            root = (qs.get("root") or [""])[0]
            if not root:
                return self._send_json(400, {"error": "missing root"})
            root_path = _resolve_known_project_root(root)
            if root_path is None:
                return self._send_json(403, {"error": "unknown project root"})
            query = (qs.get("q") or [""])[0]
            try:
                limit = int((qs.get("limit") or ["300"])[0])
            except ValueError:
                limit = 300
            limit = max(1, min(limit, 1000))
            inc_raw = (qs.get("include_ignored") or ["0"])[0].lower()
            include_ignored = inc_raw in ("1", "true", "yes")
            try:
                return self._send_json(
                    200, find_files_by_name(
                        str(root_path), query, limit, include_ignored=include_ignored
                    )
                )
            except Exception as e:
                return self._send_json(500, {"error": str(e)})

        return self._send_bytes(404, "Not found", "text/plain; charset=utf-8")

    def do_POST(self):
        if not self._request_is_trusted():
            return self._send_json(403, {"error": "cross-site request rejected"})
        # Ensure _state has been loaded from disk; same rationale as do_GET.
        load_state()
        # POST is a write path. If load failed, return 503 immediately; never let the handler
        # write dirty _state and then save it to disk. GET may degrade to an empty list, but
        # allowing POST would risk overwriting disk state.
        if not _state_loaded:
            return self._send_json(503, {
                "error": "state not loaded",
                "detail": "Server failed to load state file. Restart required or check stderr.",
            })
        url = urllib.parse.urlparse(self.path)
        path = url.path

        m = re.match(r"^/api/sessions/([^/]+)/(archive|note|star)$", path)
        if m:
            sid, action = m.group(1), m.group(2)
            try:
                body = self._read_json()
            except Exception:
                return self._send_json(400, {"error": "Invalid JSON"})

            entry = dict(_state.get(sid, {}))
            if action == "archive":
                archived = bool(body.get("archived", True))
                entry["archived"] = archived
                if archived:
                    entry["archived_at"] = datetime.now(timezone.utc).isoformat()
                else:
                    entry.pop("archived_at", None)
                if "note" in body:
                    entry["note"] = body.get("note", "")
            elif action == "note":
                entry["note"] = body.get("note", "")
            elif action == "star":
                starred = bool(body.get("starred", True))
                entry["starred"] = starred
                if starred:
                    entry["starred_at"] = datetime.now(timezone.utc).isoformat()
                else:
                    entry.pop("starred_at", None)

            _state[sid] = entry
            save_state()
            return self._send_json(200, {"id": sid, **entry})

        if path == "/api/open-file":
            try:
                body = self._read_json()
            except Exception:
                return self._send_json(400, {"error": "Invalid JSON"})
            file_path = (body.get("path") or "").strip()
            root = (body.get("root") or "").strip()
            reveal = bool(body.get("reveal"))
            if not file_path or not root:
                return self._send_json(400, {"error": "missing path or root"})
            root_path = _resolve_known_project_root(root)
            if root_path is None:
                return self._send_json(403, {"error": "unknown project root"})
            ok, err = open_file_in_system(file_path, str(root_path), reveal=reveal)
            if not ok:
                return self._send_json(400, {"error": err})
            return self._send_json(200, {"ok": True})

        return self._send_bytes(404, "Not found", "text/plain; charset=utf-8")


# ---------- Main ----------
def main(argv=None):
    global PORT
    args = parse_args(argv)
    PORT = args.port
    print(f"[info] state file: {STATE_FILE}")
    rg_executable = _find_ripgrep()
    if rg_executable:
        print(f"[info] full-text search: ripgrep at {rg_executable}")
    else:
        _warn_search_fallback("ripgrep executable not found")
    load_state()
    print(f"[info] scanning {PROJECTS_DIR} ...")
    t0 = datetime.now()
    cache_loaded = load_scan_cache()
    items = scan_sessions(force=not cache_loaded)
    save_scan_cache()
    elapsed = (datetime.now() - t0).total_seconds()
    mode = "hot cache + incremental scan" if cache_loaded else "full scan"
    print(f"[info] {len(items)} sessions loaded in {elapsed:.1f}s ({mode})")
    print(f"[info] listening on http://{HOST}:{PORT}")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[info] bye.")


if __name__ == "__main__":
    main()
