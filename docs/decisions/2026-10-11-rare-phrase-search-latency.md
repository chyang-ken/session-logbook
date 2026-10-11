# A rare phrase reaches its first result sooner

## Decision

Four changes to paged search, none of which changes which sessions match:

1. **Search no longer waits behind a scan.** `_search_cache_items` reads the session cache
   when no scan is running and otherwise uses the snapshot the last finished scan left
   (`_search_snapshot`); conversation facts follow the same rule (`_search_conversations`).
   Only a search that finds no snapshot at all waits.
2. **Slices stop doubling at `SEARCH_PAGE_MAX_SLICE` (800 records).** The page clock is read
   only between slices.
3. **Long Codex lines are not streamed.** For Codex files the line search passes
   `--max-columns SEARCH_LONG_LINE_BYTES` (8 KiB) to ripgrep; an omitted line is read back
   from its byte offset only when its first bytes do not rule out a `response_item` message
   (`_codex_long_line`). Lines whose start cannot be read as a known Codex record are read in
   full, so an unfamiliar format costs time, never a result.
4. **Later query words search only files that held the earlier ones** in the file
   prefilter, instead of the whole library once per word.

## Rationale

Measured on the maintainer's library (about 7,700 records, 11.6 GB), on a machine at load
average 100-270 with swap nearly full:

- Raw ripgrep over the whole library for a word that is nowhere: 3-5 s. The resident
  service took 64 s for the same query (six pages). Search held `_SCAN_LOCK` to copy the
  cache, so every page waited for any list refresh in progress; one refresh took up to 20 s.
- A two-word rare phrase streamed 1.28 GB of matching lines through the pipe, almost all of
  it Codex compaction archives (842 MB in 1,171 lines), world-state snapshots and
  item-completed events, each parsed as JSON only to be rejected. With change 3 the same
  query streams 30 MB (0.88 GB to 20 MB for another).
- A two-word query ran the file prefilter over all 11.6 GB twice.

## Results

Old and new builds side by side against the same frozen state copy, each with another
client refreshing the session list every 3 s, three rounds per query, interleaved. Time is
from the first request to the first page holding a hit (median, seconds):

| Query | First match at record | Old | New |
|---|---|---|---|
| two words, 2 matches | 4,843 of 7,707 | 29.3 | 23.3 |
| two words, 2 matches | 6,136 | 33.2 | 25.9 |
| two words, 1 match | 7,429 | 32.2 | 21.4 |
| one word, no match (time to "no results") | - | 11.0 | 12.3 |

The machine's load varied from 120 to 270 during the runs, so single runs ranged from 6 s
to 109 s for the same query; only the medians of interleaved rounds are comparable. A
no-match query was not faster: it was already bound by reading the library.

## What still costs time

- Reading the library itself (ripgrep, about 3-5 s here) is the floor for a match near the
  oldest record. Removing it needs a persistent text index, rejected again for now.
- Every Devin session is read in full on every search (about 1.4 s of a 4.3 s full pass in
  process); it has no file prefilter.
- A concurrent `/api/sessions` request competes for the same Python process.

## Alternatives

- **Regex that skips non-message Codex lines inside ripgrep** (an anchored negative
  lookahead): cut streamed bytes the same way but hit PCRE2's match limit on a real
  archive line, which makes ripgrep exit with an error.
- **`--max-columns` for every source**: Claude long lines are mostly real records the
  matchers need, so nothing would be saved.

## Evidence

Measurement scripts and raw output are in the maintainer's `_private/search-latency-2026-10-11/`
(they contain real session text). Tests: `tests/test_search_contract.py`
(`test_search_does_not_wait_behind_a_running_scan`, `test_slices_stop_growing_at_the_cap`,
`test_long_codex_lines_are_read_only_when_they_can_be_messages`,
`test_later_terms_only_search_files_with_the_earlier_ones`); each fails when its change is
reverted.

## Commit

Filled in on merge.
