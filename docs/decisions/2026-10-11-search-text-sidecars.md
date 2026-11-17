# Search reads a per-session text sidecar instead of the whole library

## Decision

Full-text search keeps, for every Claude, Codex, Antigravity and Kimi session file, a
plain-text **sidecar** holding the lowercased text the search matchers are able to match
in that file, one piece per line (`sources/search_text.py`). The file prefilter runs
ripgrep over the sidecars instead of over the raw session files; only files whose
sidecar holds every term go on to the existing per-session matchers, unchanged.

- **Superset, never less.** A sidecar holds message text the matchers would reject (a
  row an Antigravity rewind abandoned, a queued notice, an unfinished final record), but
  nothing they can match is left out. `message_text()` is the one function both the
  Claude/Antigravity/Kimi matcher and the sidecar writer call, so the two cannot read
  different fields. Titles and session ids are not in it; the caller admits those
  exactly as it did before.
- **Append-only for a growing file.** The sidecar's header records the source's inode,
  size and mtime, the source bytes it covers (complete lines only) and hashes of the
  first and last covered kilobyte. A source that only grew is appended to, reading only
  the new bytes; anything else is rebuilt. Nothing happens per write: a sidecar is
  brought up to date when a search needs it.
- **Freshness is checked per search, cheaply.** One `stat` per source; an unchanged
  source is answered from memory without opening its sidecar.
- **Missing or stale costs time, never a result.** A search refreshes stale sidecars
  inline up to `SEARCH_TEXT_INLINE_BYTES` (32 MB of source); every file it could not
  refresh goes through the old raw-file prefilter for that search. The server process
  builds missing sidecars on one background thread, newest first, starting at launch.
- **One pass per page.** A paged search runs the sidecar prefilter once over every
  record of the page, and once per term for the files Codex continuations inherit,
  instead of once per slice.
- **Where.** Next to the state file (`~/.session-logbook/search-text/`), so a launcher
  that rebinds `STATE_FILE` gets its own; `SESSION_LOGBOOK_SEARCH_TEXT` overrides it
  (`off` disables), mirroring `SESSION_LOGBOOK_HISTORY_INDEX`. A process whose source
  roots are not the real ones (a test fixture) writes none unless the variable is set.
- **The header can never match.** It is written in capital letters only; search terms
  and sidecar bodies are lowercased and ripgrep searches them without case folding.
- Pi (14 MB here) and Devin (a SQLite database) keep their existing paths.

`FORMAT` in `sources/search_text.py` must be bumped whenever a matcher starts matching
text it did not before, so sidecars from an older build are rebuilt instead of trusted.

## Rationale

The 2026-10-11 latency work left one floor: every search page read the whole 11.6 GB
library with ripgrep, though only about 4% of it is message text. Moving the floor needs
a smaller thing to read. The simplest such thing is the text itself, in plain files that
ripgrep already knows how to read, kept beside the state the dashboard already owns. It
adds no process, no dependency and no index format; deleting the directory loses only
speed.

## Results

All measured on the maintainer's library (about 8,000 records, 11.6 GB of session files)
at load average 30-145. Single runs of one query varied by 5x, so only medians of
interleaved rounds are comparable.

**Results did not change.** The library was cloned (APFS copy-on-write) so both builds
read identical bytes, and 24 queries (common words, multi-word, a path, an id prefix,
`"type"`, `c:\path`, one- and two-character CJK, six rare two-word phrases, no-match)
ran through the old and the new build, unpaged and paged: 24 of 24 identical, both ways.
The raw-file fallback answered for 0-28 of about 8,000 files per query.

**Rare phrase, first result through HTTP** (old and new servers side by side on copies
of the same state, another client refreshing each list every 3 s, 3 rounds, median):

| Query | Old first | New first | Old total | New total |
|---|---|---|---|---|
| rare phrase A (2 matches) | 10.0 | 3.4 | 16.4 | 4.9 |
| rare phrase B (2 matches) | 8.7 | 3.9 | 9.9 | 4.4 |
| rare phrase C (1 match) | 13.2 | 3.6 | 13.9 | 3.6 |
| rare phrase D (3 matches) | 5.6 | 1.5 | 11.6 | 3.0 |
| rare phrase E (1 match) | 10.3 | 4.3 | 12.1 | 4.4 |
| rare phrase F (1 match) | 9.6 | 4.0 | 9.6 | 4.0 |
| no match (time to "no results") | - | - | 7.8 | 4.3 |

Seconds; load average 35-101 during the run. List refreshes on the same servers in the
same run: median 1.91 s old, 1.73 s new.

**Cost.**

- Sidecars: 541 MB in 8,011 files, about 4.7% of the session files.
- First build: 51-61 s on one thread with the page cache warm; about 5 minutes in the
  server, where the builder rests as long as each file took so requests keep at least
  half of the interpreter. While it runs, a list refresh took a median 3.6 s against
  2.9 s for the old build (before the rest was added: 2.2 s against 1.2 s). It happens
  once, and again only when `FORMAT` changes.
- Steady state, 10 minutes of real activity (dozens of agents writing; the library grew
  by 78 MB of session text in that time), both servers refreshing their list every 3 s
  and searching a rare phrase every 30 s: server-side `scan_sessions` median 1.13 s in
  both builds (p90 3.6 s old, 4.1 s new); list refresh as the client saw it, median
  2.26 s old and 2.38 s new. Bringing sidecars up to date cost 19.6 s of wall time over
  the 14 searches the new build ran, about 1.4 s each including time spent waiting for
  the interpreter, and reading 78 MB of appended source; the scan itself does no sidecar
  work. In the same window a search took a median 18.4 s old and 7.0 s new (first result
  15.6 s and 6.8 s).

## What still costs time

Under this load a rare phrase still takes several seconds, not one or two. What is left
is fixed work every search page repeats, none of it reading the library: building the
conversation views for the hits (0.3-1 s), walking the whole Codex tree to plan
continuation histories (`history_index.catalog`, 0.3-1 s per page), reading every Devin
session in full (0.3-0.8 s), and competing with list refreshes for the same Python
process. A page ends after `SEARCH_PAGE_MAX_S`, so on a busy machine a rare phrase needs
two to five pages and pays these costs each time. In one process with no list refresh
competing (the frozen comparison above), a whole-library search for the six rare phrases
took 1.9-3.5 s new against 5.5-12.3 s old.

Sidecars of session files that were deleted or moved (Codex archiving moves files) are
not removed; they cost disk space only, since nothing points a search at them.

## Alternatives

- **A persistent inverted index or SQLite FTS.** Answers any query in milliseconds, but
  needs its own tokenizer, which has to agree with substring matching for CJK and for
  terms like `c:\path`; a mismatch silently drops results. Rejected again: the sidecars
  keep substring semantics for free because the same ripgrep literal match runs on them.
- **Building sidecars during the list scan.** The scan holds the lock every list refresh
  waits for, and dozens of agents append to the library all the time, so the scan would
  inherit their write rate. Refreshing at search time touches only what a search reads.
- **Sidecar metadata in a separate manifest.** Two writers (the request thread and the
  builder thread, or two processes) could leave a manifest that names one state and a
  sidecar that holds another. A header inside the sidecar is written with the body, so
  a rebuild is atomic (temporary file and rename) and an interrupted append can only
  leave duplicate text, which a superset tolerates.

## Evidence

Measurement scripts, raw output and the mutation run are in the maintainer's
`_private/search-sidecar-2026-10-11/` (they contain real session text). Tests:
`tests/test_search_contract.py` runs every contract query through the sidecars and
asserts they really were the prefilter, that the result equals both the whole-file path
and the raw-file prefilter, and covers appends, rewrites, an unfinished final record,
queued text, Project thread ids, one- and two-character CJK queries, the header, the
budget fallback, a sidecar removed behind the memo, the background builder and the
directory guard. Each safeguard was broken on purpose under `python3 -B`; every break
failed at least one test.

## Commit

Filled in on merge.
