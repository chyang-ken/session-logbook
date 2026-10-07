# A Codex sub-agent rollout is a complete session, not a session with a missing prefix

**Date:** 2026-09-20
**Status:** accepted

## Decision

A Codex rollout that its own `session_meta` marks as a spawned sub-agent thread is reported
`complete`, and the thread that spawned it is reported as lineage (`spawned_from`) rather than
as a missing history segment.

Two things are deliberately left alone:

- A rollout whose records start at an **ordinal other than 0** still reports
  `missing_history_base`. That is the one native signal that earlier records existed in this
  ordinal space, and it holds for a sub-agent exactly as for any other rollout.
- A **user fork** without a `history_base` keeps reporting `missing_history_base`. Its
  `forked_from_id` is a real branch marker with no recoverable boundary.

## Rationale

`missing_history_base` fired whenever a rollout carried `forked_from_id` and no `history_base`.
That reads `forked_from_id` as "an inherited prefix lives in another file". On a sub-agent
rollout it does not mean that.

Measured over the whole local corpus of 2,395 rollout files (2,376 distinct session ids),
reading only file heads plus Codex's own read-only thread database:

| Question | Answer |
|---|---|
| Sub-agent rollouts carrying a `history_base` | **0 of 1,795** |
| Sub-agent rollouts carrying `forked_from_ordinal_exclusive` (a split coordinate) | **0 of 290** |
| Sub-agent rollouts whose first record ordinal is 0 | **1,783 of 1,795** (the other 12 are `history_mode: legacy` files with no ordinals at all) |
| Sub-agent `forked_from_id` that merely repeats `parent_thread_id` or `session_id` | **276 of 290**; the remaining 14 are the oldest clients (0.133–0.136), where `forked_from_id` is the *only* parent pointer written |
| Sub-agent rollouts whose spawn parent exists on disk | **290 of 290** |

So a spawned thread has no field that points at a byte range in another file, and no field
claiming one. Where the spawn did hand the thread inherited context, that context is **copied
into the thread's own file**: of 145 sampled flagged rollouts, 47 open with the parent thread's
own `session_meta` followed by the parent's records, 48 open with a compaction snapshot that
embeds the history, and 50 open directly with the sub-agent's own prompt. Nothing is inherited
by reference, so nothing can be missing.

Codex's own `state_5.sqlite` agrees independently: 277 of the 290 appear in `thread_spawn_edges`
as a child (the rest are guardian threads, which that table never covers), the edge's parent
matches the rollout's own parent pointer in 277 of 277, and `threads.thread_source` is
`subagent` for all 290.

**What separated the flagged rollouts from the unflagged ones was nothing about their content.**
1,505 sub-agents carry no `forked_from_id` and were never flagged; 290 carry one and were all
flagged. Both groups start at ordinal 0, both lack a `history_base`, both open with the same
mix of shapes, and 16 CLI versions appear in both groups. The flag tracked whether the writing
client happened to also emit a redundant pointer.

The cost of that was not cosmetic: opening such a session in the reader raised
"Conversation history is incomplete — some earlier context is unavailable" over a transcript
that was in fact whole, and the CLI refused `observe` on it with `context_incomplete`.

## Alternatives rejected

- **Follow `forked_from_id` into the parent file and prepend a prefix.** There is no boundary
  to follow: no `forked_from_ordinal_exclusive` and no `history_base` on any sub-agent rollout.
  Choosing a cut point would be a guess, and it would duplicate context the file already holds.
- **Keep `complete: False` and only reword the issue.** That would keep telling the reader
  something is missing when nothing is. The honest report is "complete, and here is who
  spawned it".
- **Suppress the issue for every rollout without a `history_base`.** That would also silence
  user forks, where the pre-fork turns genuinely are unreachable.
- **Use `subagent_history_start_ordinal` as the boundary.** Measured, not trusted: in 312 of
  337 sampled sub-agents its value equals the file's own total record count, and it never lines
  up with anything meaningful in the parent. Its meaning is not established, so no behaviour
  rests on it.
- **Widen `codex._is_subagent` and reuse it here.** That predicate decides which cards the
  dashboard lists and includes a content-sniffing fallback; changing it would change the
  visible session list. The history layer needs a narrower, metadata-only question, and
  `fork_lineage` already contained exactly that test inline — it was factored out into
  `spawn_lineage` so there is one implementation, not two.

## Evidence

Read-only sweep of `codex_history.resolve()` over the real local corpus, before and after,
with the history index disabled. The change can only affect rollouts that carry
`forked_from_id` and no `history_base`; all 306 such files were probed, not a sample.

| | before | after |
|---|---|---|
| Deterministic 80-rollout sample, incomplete | 12 (all sub-agents) | 0 |
| All 306 changeable rollouts, incomplete | 306 | 16 — exactly the user forks |
| Non-sub-agent sessions whose `complete` or `issues` changed | — | 0 |
| Sessions whose effective record count changed | — | 0 |

The transcripts themselves are byte-identical; only the verdict about them changed. The raw
sweep output contains real session ids and paths and is therefore kept out of this repository.

## Commit

`Decision: docs/decisions/2026-09-20-codex-subagent-completeness.md`

## Open, not settled here

- **A user fork without a `history_base` may also be self-contained.** All 16 in the corpus
  have a first user message identical to their parent's, and 12 of them exceed 50 MB, which is
  consistent with the whole parent prefix being copied in. That was not verified beyond the
  opening message, and it is a different population from the one this decision covers, so their
  behaviour is unchanged.
- **`session_identity._relationship` cannot name the parent of the 14 oldest guardian
  rollouts**, because it reads only `parent_thread_id` and `source.subagent.thread_spawn`.
  `spawned_from` now answers for those files where `parent_session_id` still reports nothing.
