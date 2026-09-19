# Claude copied history preserves identity and branch evidence

## Decision

Expose source overlap and compaction as observed facts, while keeping each Claude
Session ID and physical file separately addressable. The selected transcript is the
history rendered in context, the web reader, and exports. Identical UUID/content
copies within that transcript render once. Related transcripts are links for
on-demand inspection, not concatenated history and not automatic supervision targets.

An explicit caller-selected target change can translate a physical cursor through a
shared UUID with equal rendered content. Changed usage accounting is ignored. A
missing, conflicting, or ambiguous UUID stops delivery for reconciliation. An old
file's unshared tail cannot be silently skipped. Cross-ID observation starts a new
Hook/native/turn namespace independently of the translated conversation cursor.

## Evidence and limits

A read-only local investigation found copied record UUIDs, a shared manual compaction
boundary with logical-parent and preserved-message fields, and a new user input
parented to the final shared record. The original file also had a later unshared
branch. All shared message content agreed; one copy changed only usage accounting.
These establish overlap. They do not establish why the client changed IDs or whether
arbitrary overlapping files represent continuation versus fork. The specific user's
predecessor confirmation is valid caller context, not a general inference rule.
Private source IDs, paths, and transcripts are deliberately excluded from this repo.

Synthetic regressions in `tests/test_claude_history.py` cover overlap, same-ID copies,
independent sessions, compaction, divergent tails, accounting-only updates, conflicting
UUIDs, partial writes, append/rewrite cache validation, and independent observation
cursors. `tests/test_claude_history_web.py` checks source UI contracts and export.

## Alternatives rejected

- Concatenating whole files duplicates inherited content and imports abandoned work.
- Picking the newest title, timestamp, or shared prefix can turn a fork into an
  unauthorized continuation and attach old completion events to a new task.
- Reconstructing pre-compaction raw text as if the client inherited it would erase
  the distinction between preserved context and historical evidence.
- Storing a second canonical transcript or a supervisor registry would introduce
  unnecessary state ownership. The index is rebuildable and holds no message bodies.

## Consumer contract and affected surfaces

`locate/status/observe` expose source references. `context/follow` and HTTP anchored
export retain original physical line coordinates. A source-qualified follow reads
only the indexed suffix, after verifying the selected source and any cursor mapping.
The web source panel provides real session IDs and paths. Sharing still addresses the
selected ID. Search and discovery retain distinct IDs, so old branch-only text stays
findable. No source file, watchlist, Bridge, or external runtime is modified.

Same-ID copies are reported as such when both native IDs are known; an exact path
selects a physical source without guessing which copy is authoritative. Existing
ID-only discovery remains unchanged. Ambiguous/missing native identity prevents
cross-file cursor mapping. Invalid records and conflicting UUIDs are reported.
There is no claimed native marker distinguishing fork from continuation in this
sample. Shared history remains `undetermined` even when the caller separately knows
what it intended. This is a read-only observation contract for any supervisor.

## Validation and release boundary

Tests use synthetic files and isolated indexes. Real-sample checks use a temporary
index and read-only source access. This task is authorized through a commit on its
feature branch only; deployment, staging promotion, and main promotion are separate.

## Commit

See the implementing commit referencing this decision document.
