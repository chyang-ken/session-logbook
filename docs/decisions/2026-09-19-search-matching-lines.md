# Search reads only lines that contain a term

## Decision

Full-text search streams, from ripgrep, only the lines that contain a query term
(`_rg_matching_lines`) and feeds them to the existing per-session matchers, instead of
reading every candidate file in Python. Occurrences inside JSON keys are excluded with a
PCRE2 lookahead when ripgrep supports it. Codex histories use the same lines, bounded by
the indexed history coordinates. Any ripgrep failure, missing index, incomplete history,
or query containing JSON-escaped characters falls back to reading whole files.

## Rationale

Profiling on the maintainer's real history (about 3,000 sessions, about 10 GB) showed
that search time was dominated by reading and lower-casing whole candidate files, most
of whose bytes are tool output. A path-like query with one true hit opened 87 files
(2.2 GB) and took 16.6 s. The matchers already skip every line without a term, so
supplying only those lines cannot change results. JSON keys such as `timestamp` and
`sessionId` occur on nearly every line; in a 5 MB sample, excluding key occurrences cut
matching lines for `time` from 1,615 to 45 and for `session` from 1,862 to 23. Inside a
JSON string a quote is always escaped, so a term followed by identifier characters and
`":` can only be part of a key.

A second, redundant raw-line lower-case pass on lines ripgrep had already selected cost
about a third of the remaining time and was removed.

## Alternatives

- A persistent full-text index would remove the roughly 1-1.5 s ripgrep scan floor, but
  adds a resident data store and invalidation; not justified by current needs.
- Narrowing multi-term queries by their rarest term first was considered; common
  short terms still dominate, and the added phase was not needed for the target use
  (locating a session by a long, nearly unique string).

## Evidence

26 real queries (unique strings, paths, IDs, common words, multi-term, quotes and
backslashes), old deployed build versus this change, isolated state/cache/index:

- Result sets and snippets identical for all 26. Order differed for six queries only
  where the moved sessions were being written between the two runs.
- Queries with up to about 150 hits (unique strings, paths, IDs): 1.2-4.6 s
  (previously 1.4-43 s).
- Common words with hundreds of hits: 10-42 s (previously 52-124 s).

Raw outputs contain real session identifiers and stay outside Git in
`_private/search-line-bench-2026-09-19/` on the maintainer's machine. Synthetic
regression tests: `tests/test_codex_history.py` (`test_line_search_*`), which also
assert the fast path really ran.

## Commit

The commit that introduces this record.
