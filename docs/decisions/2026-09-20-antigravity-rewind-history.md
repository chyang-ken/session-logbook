# Antigravity in-file rewinds: a step slot with a second occupant

## Decision

An Antigravity rewind stays inside one transcript. The client re-opens an earlier
`step_index` and keeps appending, so the abandoned branch and the branch that replaced it
sit in the same file with no lineage record of any kind. Session Logbook now selects the
live history before presenting anything:

> A drop in `step_index` to step *k* is a rewind **only when step *k* is already held by a
> row that is still live**. That rewind abandons every still-live row whose step slot is at
> or above *k*. A drop to a step slot that nothing holds is a write-order artifact and
> abandons nothing.

Card previews, first user message, turn count, the conversation reader, the Markdown
export and full-text search all read the live history only. Rows are never reordered and
never renumbered: the file's own physical line numbers are preserved, so raw evidence for
an abandoned row still resolves at its original line. The conversation payload carries
`rewind_abandoned_rows` and a `rewinds` list (`{line, step, abandoned_rows}`), and the
Markdown export gains an `Abandoned by rewind: N raw lines` header line when N is non-zero,
so nothing is hidden without being counted.

The project path is still inferred from every row, including abandoned ones. A rewind
changes the conversation, not the IDE workspace it ran in. This matches Pi, which reads the
session title from all rows and the model and stop reason from the selected branch only.

## Rationale

`sources/antigravity.py` read the transcript linearly, so a rewound conversation was
rendered as the abandoned branch and the live branch concatenated into one history, with
duplicated questions and answers that were never in the same conversation.

The step slot is the only signal on disk. The guard is what makes it usable, and it is a
direct statement of what a rewind *is*: the same step number gets a second occupant.

**Corroboration.** The client keeps its own count of how many steps a conversation
currently has. Across the local corpus of 73 transcripts, six `step_index` drops occur in
five files. The guarded rule reproduces the client's own count for all five
(3 / 36 / 38 / 98 / 35 steps) and leaves the other 68 untouched. Of those 68, 63 also equal
their own `max(step_index) + 1`; the 5 that do not are three-row conversations whose client
count is one higher than any step the transcript ever recorded, which is unrelated to
rewinds and predates this change.

## Alternatives rejected

- **Treating every `step_index` drop as a rewind.** This is the failure the guard exists to
  prevent. Three of the six observed drops are write-order artifacts: a context-compaction
  `CHECKPOINT` row persisted two or four rows late, and a planner row persisted after the
  results of the parallel tool calls it issued. Applied unguarded, the rule silently deletes
  9 rows of live conversation across those three files, and the client's own step count
  disagrees with the result in all three.
- **Guarding on `created_at` going backwards.** Equally accurate on this corpus — the three
  artifacts are exactly the three drops whose timestamp moves backwards, while the three
  rewinds have a multi-minute forward pause — but it rests on clock behaviour rather than on
  the sequence itself. Kept as a cross-check, not as the test.
- **Guarding on the drop row being a user message.** Accurate here (all three rewinds are
  `USER_INPUT`) but a proxy. Nothing on disk guarantees it, and a client that rewinds to a
  non-user step would break it.
- **Comparing against the previous row's step instead of the furthest step still live.**
  The two agree on every one of the 73 transcripts. The furthest-live comparison was chosen
  because a row persisted out of order temporarily lowers the previous row's step, which
  would hide a rewind that arrives right after an artifact. The guard, not the comparison,
  is what discriminates.
- **Hiding the abandoned rows without saying so.** Rejected: the conversation payload and
  the export both state how many rows a rewind removed, and the raw line numbers still point
  into the source.
- **Offering an "include abandoned rows" reading mode.** Not built. Claude's in-file rewind
  has one because a UI entry point already exists for it; Antigravity has none, and adding a
  reading mode nothing can reach is not worth the extra surface. The raw transcript remains
  the evidence path, at unchanged line numbers.

## Affected surfaces

`extract_metadata`, `extract_conversation`, `extract_transcript` in
`sources/antigravity.py`, and Antigravity's branch of `_search_session` in `server.py`,
which now skips the abandoned physical lines on both the ripgrep fast path and the
whole-file path. `CACHE_SCHEMA_VERSION` is bumped, because the cached metadata shape
changes for affected files.

Antigravity is not a source of the anchored transcript renderer or of
`session_logbook_cli.py`, so there is no third place presenting this content today.
`tests/test_antigravity_rewind.py` asserts that boundary, so the rule gets applied if either
surface gains an Antigravity reader.

Following a running conversation is a full redraw here: the standalone reader polls
`/conversation?fingerprint=…` and replaces the payload whenever the file changes. That is
the right shape, because a rewind retroactively removes rows a follower was already shown;
the redraw carries the new `rewind_abandoned_rows`, so the follower is told the earlier
steps are gone instead of silently losing them.

## Known edges, stated honestly

- **A rewind to a step slot that was never written.** Gaps in the step sequence are ordinary
  (35 across the corpus). If a rewind targets a slot that holds no row, the guard sees no
  second occupant and classifies the rewind as an artifact, keeping the abandoned branch.
  Not observed. The failure is over-inclusion, which is the safe direction.
- **Re-submitting the newest row.** That does not lower `step_index`, so the rule cannot see
  it and keeps both occupants. Over-inclusion again.
- **Two rewinds to the same step.** The rule handles it by construction — each rewind
  abandons only what is live at that moment — but no transcript in the corpus exercises it,
  so that path is covered by synthetic tests only. One real file does stack two rewinds to
  *different* steps and matches the client's count.
- **Sample size.** Three rewinds in 73 transcripts, from one machine. The agreement with the
  client's own step count is strong evidence that the mechanism is understood; it is still
  three data points.

## Evidence

Synthetic regressions: `tests/test_antigravity_rewind.py` (classifier, three artifact
shapes, stacked rewinds, rewind to zero, gaps, rows with no step slot, ordering and line
anchors, and every reader) and two rows in `tests/test_search_contract.py` that run the
rewound fixture through both search paths.

The corpus measurements above come from a read-only local analysis of real transcripts and
of a read-only copy of the client's summary database. Real conversation IDs, paths and
transcript content are deliberately excluded from this repository; only counts appear here.

## Commit

The commit that adds this record also implements it ("Read Antigravity in-file rewinds as
live history only"). Resolve its full identity with
`git log --diff-filter=A --format=%H -- docs/decisions/2026-09-20-antigravity-rewind-history.md`.
