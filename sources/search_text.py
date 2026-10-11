"""Search-text sidecars: per session file, the text full-text search is able to match.

A session file is mostly tool output, snapshots and compaction archives; on the
maintainer's library about 4% of the bytes are message text. The file prefilter used to
run ripgrep over every byte of every file on every search. A sidecar holds, lowercased,
every piece of text a search matcher can match in one source file, one piece per line, so
the prefilter reads only that.

Contract: a sidecar is a **superset** of what the matchers can match in its file. It
holds message text the matchers would reject (an abandoned Antigravity row, a queued
notice, an unfinished final record) and never less, so using it as a prefilter cannot
drop a result. Titles and session ids are not in it; the caller admits those separately,
exactly as it did with the raw-file prefilter.

The file starts with a fixed-width header written in capital letters only. Search terms
are lowercased, and the body is lowercased, so ripgrep searching without case folding can
never match the header. The header records the source's inode, size and mtime when the
sidecar was last brought up to date, how many source bytes it covers (complete lines
only), and hashes of the first and last covered bytes. A source that only grew is
appended to; anything else is rebuilt. A source whose sidecar is missing, stale and too
large to refresh within the caller's budget is reported back as unindexed, and the caller
prefilters it the old way.

**Bump FORMAT whenever a matcher starts matching text it did not before**, so sidecars
written by an older build are rebuilt instead of trusted.
"""
import contextlib
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from sources import antigravity as ag_source
from sources import claude_events
from sources import codex as codex_source
from sources import kimi as kimi_source
from sources.claude_text import (assistant_search_words, human_turn_words, normalize_record,
                                 project_message_ids, project_wake_words)

FORMAT = 1
_MAGIC = "LOGBOOKSEARCHTEXT"
_TO_CAPS = str.maketrans("0123456789abcdef", "ABCDEFGHIJKLMNOP")
_FROM_CAPS = str.maketrans("ABCDEFGHIJKLMNOP", "0123456789abcdef")
_EDGE_BYTES = 1024
# MAGIC, format, inode, size, mtime_ns, covered bytes, head hash, tail hash
HEADER_BYTES = len(_MAGIC) + 1 + (2 + 1) + 5 * (16 + 1) + 16 + 1

# Set by the server process: stale sidecars too large to refresh inline are then built
# by one background thread. Tests and scripts leave it off and build explicitly.
BACKGROUND = False

# Running totals of sources prefilter() read through a sidecar and sources it left to the
# raw-file prefilter, for tests and diagnostics: a silent fallback must be visible.
# refresh_seconds / refresh_bytes: time and source bytes searches spent bringing sidecars
# up to date inline.
stats = {"indexed": 0, "unindexed": 0, "refresh_seconds": 0.0, "refresh_bytes": 0}


# ---------- what a matcher can match ----------

def message_text(d):
    """The message text the Claude / Antigravity / Kimi matcher reads from one record.

    `d` is a record already passed through normalize_record. Queue entries are handled
    by the caller before this is asked. server._search_session uses this same function,
    so the matcher and the sidecar cannot disagree about which field holds the text.
    """
    t = d.get("type")
    if t == "user":
        return human_turn_words(d)
    if t == "assistant":
        return assistant_search_words(d.get("message", {}).get("content"))
    if t == "response_item":
        # Codex row: extract message.role=user/assistant text and search it too
        p = d.get("payload") or {}
        if p.get("type") == "message" and p.get("role") in ("user", "assistant"):
            return codex_source._extract_text_from_message_content(p.get("content"))
        return ""
    if t in ("USER_INPUT", "PLANNER_RESPONSE"):
        # Antigravity row: extract user/assistant text and search it too
        return ag_source.search_text_from_line(d)
    if t in ("context.append_message", "context.append_loop_event"):
        # Kimi row: appended user message / assistant text part
        return kimi_source.search_text_from_line(d)
    return ""


def generic_line_texts(line):
    """Every string the generic matcher can match in one raw line (str), unlowered."""
    try:
        d = normalize_record(json.loads(line))
    except Exception:
        return []
    if not isinstance(d, dict):
        return []
    queued = claude_events.enqueued_text(d)
    if queued is not None:
        # Kept whether or not it reads as human text: a superset is all this needs.
        return [project_wake_words(queued).strip()]
    texts = []
    if d.get("type") == "user":
        texts.extend(project_message_ids(d))
    text = message_text(d)
    if text:
        texts.append(text)
    return texts


def codex_message(raw):
    """Return (role, text) when a raw Codex record is a user/assistant message, else None.

    Mirrors codex_history.read_segment's record validation and iter_messages' selection.
    """
    try:
        row = json.loads(raw)
    except (ValueError, UnicodeError):
        return None
    if not isinstance(row, dict) or row.get("type") != "response_item":
        return None
    payload = row.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "message":
        return None
    role = payload.get("role")
    if role not in {"user", "assistant"}:
        return None
    text = codex_source._extract_text_from_message_content(payload.get("content"))
    return (role, text) if text else None


# The start of a Codex rollout line: optional timestamp and ordinal, then the record type
# and, for a response item, the payload type.
CODEX_LINE_HEAD = re.compile(
    rb'\{(?:"timestamp":"[^"\\]*",)?(?:"ordinal":\d+,)?"type":"([a-z_]+)"'
    rb'(?:,"payload":\{"type":"([a-z_]+)")?')


def codex_ruled_out(head):
    """Whether the first bytes of a Codex line already prove it is not a message."""
    m = CODEX_LINE_HEAD.match(head)
    return bool(m and (m.group(1) != b"response_item"
                       or (m.group(2) is not None and m.group(2) != b"message")))


def _line_texts(raw, kind):
    if kind == "codex":
        if codex_ruled_out(raw[:512]):
            return []
        message = codex_message(raw)
        return [message[1]] if message else []
    return generic_line_texts(raw.decode("utf-8", "replace"))


# ---------- the sidecar files ----------

def _caps(value, width):
    return format(value, "0%dx" % width).translate(_TO_CAPS)


def _header(stamp, done, edges):
    ino, size, mtime = stamp
    fields = [_MAGIC, _caps(FORMAT, 2), _caps(ino, 16), _caps(size, 16), _caps(mtime, 16),
              _caps(done, 16), edges[0], edges[1]]
    text = " ".join(fields) + "\n"
    assert len(text) == HEADER_BYTES
    return text.encode("ascii")


def _read_header(sidecar):
    """(inode, size, mtime_ns, covered bytes, head hash, tail hash) or None."""
    try:
        with open(sidecar, "rb") as f:
            raw = f.read(HEADER_BYTES)
    except OSError:
        return None
    parts = raw.decode("ascii", "replace").rstrip("\n").split(" ")
    if len(raw) != HEADER_BYTES or len(parts) != 8 or parts[0] != _MAGIC:
        return None
    try:
        if int(parts[1].translate(_FROM_CAPS), 16) != FORMAT:
            return None
        numbers = [int(p.translate(_FROM_CAPS), 16) for p in parts[2:6]]
    except ValueError:
        return None
    return (*numbers, parts[6], parts[7])


def _digest(data):
    return hashlib.sha1(data).hexdigest()[:16].translate(_TO_CAPS)


def _edges(f, done):
    """Hashes of the first and last covered bytes of an open source file."""
    f.seek(0)
    head = f.read(min(_EDGE_BYTES, done))
    start = max(0, done - _EDGE_BYTES)
    f.seek(start)
    tail = f.read(done - start)
    return _digest(head), _digest(tail)


_sidecar_paths = {}


def sidecar_path(store, source):
    """Where the sidecar of one source file lives; a symlinked source shares one.

    Remembered per (store, source): a search asks for every file of the library.
    """
    key = (str(store), source)
    found = _sidecar_paths.get(key)
    if found is None:
        if len(_sidecar_paths) > 100_000:
            _sidecar_paths.clear()
        real = os.fsencode(os.path.realpath(source))
        found = _sidecar_paths[key] = Path(store) / (hashlib.sha1(real).hexdigest() + ".txt")
    return found


_locks = {}
_locks_guard = threading.Lock()
_failed = {}  # sidecar -> source stamp whose texts could not be extracted
# sidecar -> source stamp it was last seen to cover, so an unchanged source costs one stat
# rather than opening its sidecar. Cleared when ripgrep cannot read a sidecar.
_fresh = {}


def _lock_for(sidecar):
    with _locks_guard:
        return _locks.setdefault(str(sidecar), threading.Lock())


def refresh(source, kind, store, budget):
    """Bring one sidecar up to date if that reads at most `budget` source bytes.

    Return (sidecar path or None, source bytes read). None means the caller must treat
    the source as unindexed for now. kind is "codex" or "generic".
    """
    sidecar = sidecar_path(store, source)
    try:
        st = os.stat(source)
    except OSError:
        return None, 0
    stamp = (st.st_ino, st.st_size, st.st_mtime_ns)
    key = str(sidecar)
    if _fresh.get(key) == stamp:
        return sidecar, 0
    held = _read_header(sidecar)
    if held is not None and tuple(held[:3]) == stamp:
        _fresh[key] = stamp
        return sidecar, 0
    if _failed.get(str(sidecar)) == stamp:
        return None, 0
    lock = _lock_for(sidecar)
    if not lock.acquire(blocking=False):
        return None, 0  # being built right now by another thread
    try:
        held = _read_header(sidecar)
        if held is not None and tuple(held[:3]) == stamp:
            return sidecar, 0
        try:
            result = _update(source, kind, sidecar, st, stamp, held, budget)
            if result[0] is not None:
                _fresh[key] = stamp
            return result
        except Exception:
            _failed[str(sidecar)] = stamp
            return None, 0
    finally:
        lock.release()


def _update(source, kind, sidecar, st, stamp, held, budget):
    with open(source, "rb") as f:
        start = 0
        if held is not None and held[0] == st.st_ino and held[3] <= st.st_size:
            if _edges(f, held[3]) == (held[4], held[5]):
                start = held[3]
        if st.st_size - start > budget:
            return None, 0
        Path(sidecar).parent.mkdir(parents=True, exist_ok=True)
        if start:
            out = open(sidecar, "r+b")
            out.seek(0, os.SEEK_END)
            tmp = None
        else:
            tmp = Path(str(sidecar) + ".%d.%d.tmp" % (os.getpid(), threading.get_ident()))
            out = open(tmp, "wb")
            out.write(b" " * HEADER_BYTES)  # placeholder until the body is complete
        try:
            f.seek(start)
            done = start
            while True:
                raw = f.readline()
                if not raw:
                    break
                complete = raw.endswith(b"\n")
                for text in _line_texts(raw, kind):
                    out.write(text.lower().encode("utf-8", "surrogatepass") + b"\n")
                if not complete:
                    # An unfinished final record: its text is kept (a superset), but it is
                    # read again on the next update, once it is finished.
                    break
                done += len(raw)
            edges = _edges(f, done)
            out.flush()
            out.seek(0)
            out.write(_header(stamp, done, edges))
        except BaseException:
            out.close()
            if tmp is not None:
                with contextlib.suppress(OSError):
                    tmp.unlink()
            raise
        out.close()
        if tmp is not None:
            os.replace(tmp, sidecar)
        return sidecar, done - start


# ---------- the prefilter ----------

def _rg_files(rg_executable, term, files, chunk_bytes):
    """Files (as given) whose bytes contain `term` exactly; None when ripgrep fails."""
    fixed = [rg_executable, "-l", "-F", "-a", "--no-messages", "-e", term, "--"]
    fixed_bytes = sum(len(os.fsencode(arg)) + 1 for arg in fixed)
    hits, batch, batch_bytes = set(), [], fixed_bytes
    batches = []
    for path in files:
        size = len(os.fsencode(path)) + 1
        if batch and batch_bytes + size > chunk_bytes:
            batches.append(batch)
            batch, batch_bytes = [], fixed_bytes
        batch.append(path)
        batch_bytes += size
    if batch:
        batches.append(batch)
    for files_batch in batches:
        try:
            r = subprocess.run([*fixed, *files_batch], capture_output=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return None
        # 0 = matches, 1 = none; 2 also covers a sidecar that vanished, which must not
        # read as "no match".
        if r.returncode not in (0, 1):
            return None
        hits.update(line for line in r.stdout.decode("utf-8", "surrogateescape").splitlines()
                    if line)
    return hits


def prefilter(terms, items, store, rg_executable, budget, chunk_bytes):
    """Prefilter (source, kind) items through their sidecars.

    Return (sources whose sidecar holds every term, unindexed sources), or None when the
    sidecars cannot be used for this query at all. `budget` caps the source bytes read to
    bring stale sidecars up to date; the rest are unindexed for this call and, with
    BACKGROUND on, handed to the builder thread.
    """
    try:
        for term in terms:
            term.encode("utf-8")
    except UnicodeError:
        return None
    fresh, unindexed, stale = {}, [], []
    began = time.monotonic()
    read = 0
    for source, kind in items:
        sidecar, spent = refresh(source, kind, store, budget)
        if sidecar is None:
            unindexed.append(source)
            stale.append((source, kind))
            continue
        budget -= spent
        read += spent
        # Two names for one file (a symlink) share a sidecar; answer for both.
        fresh.setdefault(str(sidecar), []).append(source)
    stats["refresh_seconds"] += time.monotonic() - began
    stats["refresh_bytes"] += read
    if stale:
        kick(stale, store)
    stats["indexed"] += len(fresh)
    stats["unindexed"] += len(unindexed)
    candidate = list(fresh)
    for term in terms:
        if not candidate:
            break
        hits = _rg_files(rg_executable, term, candidate, chunk_bytes)
        if hits is None:
            _fresh.clear()  # a sidecar may have been removed under us; look again next time
            return None
        candidate = [path for path in candidate if path in hits]
    return {source for path in candidate for source in fresh[path]}, unindexed


# ---------- background builder ----------

_pending = {}
_pending_guard = threading.Lock()
_worker = None


def kick(items, store):
    """Queue (source, kind) items for the builder thread; newest first is the caller's order."""
    global _worker
    if not BACKGROUND:
        return
    with _pending_guard:
        for source, kind in items:
            _pending.setdefault(source, (kind, store))
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_build_pending, name="search-text", daemon=True)
            _worker.start()


def _build_pending():
    while True:
        with _pending_guard:
            if not _pending:
                return
            source = next(iter(_pending))
            kind, store = _pending[source]
        began = time.monotonic()
        try:
            refresh(source, kind, store, float("inf"))
        finally:
            with _pending_guard:
                _pending.pop(source, None)
        # Rest as long as the file took (at most half a second), so a first build after a
        # deploy leaves the request threads at least half of the interpreter.
        time.sleep(min(time.monotonic() - began, 0.5))


def build(items, store):
    """Bring every sidecar up to date now, on this thread; return how many are fresh."""
    return sum(1 for source, kind in items
               if refresh(source, kind, store, float("inf"))[0] is not None)
