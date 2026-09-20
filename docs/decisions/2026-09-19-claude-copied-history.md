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
The initial transcript-only investigation did not establish a native cause.
The client-log follow-up below supersedes that uncertainty for this sample. Shared history remains `undetermined` even when the caller separately knows
what it intended. This is a read-only observation contract for any supervisor.

## Validation and release boundary

Tests use synthetic files and isolated indexes. Real-sample checks use a temporary
index and read-only source access. This task is authorized through a commit on its
feature branch only; deployment, staging promotion, and main promotion are separate.

## Commit

See the implementing commit referencing this decision document.


## Follow-up: client logs establish rewind-driven forks

The user requested a causal investigation after the initial implementation; no
publication or runtime changes were authorized. Reading the desktop client's own
main log resolved the sampled ID changes without operating the original session.

The observed sequence was a rewind request, resume at a specific existing assistant
record with `forkSession`, and a logged CLI-session-ID change. This occurred twice,
through an intermediate transcript. The desktop internal session ID stayed the same
across all three CLI IDs. The resume anchor exactly matched the last shared assistant
record in the three transcript files. Therefore the sample demonstrates **rewind-driven
forking inside one desktop conversation**, not length-triggered automatic file rollover.
The log does not identify who invoked the UI action or why; do not invent that detail.

This separates two identities that the original investigation conflated: the desktop
conversation can remain the same while its selected underlying CLI history becomes a
new branch. Neither shared UUIDs alone nor the user's uninterrupted desktop experience
can distinguish that event. The local evidence references and narrowly selected log
lines are in git-ignored `_private/claude-desktop-rewind-investigation.json` in the
investigation worktree; real identifiers and log excerpts are not public fixtures.

Official [desktop documentation](https://code.claude.com/docs/en/desktop) describes
summarizing and continuing when context fills. The CLI
[session documentation](https://code.claude.com/docs/en/sessions) separately describes
branching as copying conversation history while preserving the original. These docs
support the distinction but do not prove the behavior of every desktop version.
The runtime log identifies the sampled engine as version 2.1.275.

No parser or automatic supervisor-selection rule was changed by this follow-up.
The existing feature remains unpublished. Before claiming desktop conversation
continuity as a product capability, its native desktop-to-CLI identity mapping and
rewind semantics would need explicit support; the current shared-history reader
alone does not provide that capability. No long-context reproduction or paid model
call is needed to explain the observed sample now that its causal log is available.

## Approved product behavior: current conversation first

The user approved showing one current conversation by default, retaining earlier
records behind an explicit reader entry, and keeping independently forked sessions
visible. This implementation remains on the feature branch for personal acceptance;
it does not replace the resident service or publish a release.

Desktop relationships now come from read-only native session descriptors. An explicit
`rewindEdges` chain must end at `cliSessionId`, with all source files available in the
same project. The UI folds proven ancestors and provides links back to the current
conversation. `priorCliSessionIds`, copied content, names, and modification times do
not justify folding. Conflicting ownership, including an ancestor still selected by
another desktop conversation, leaves the entire chain visible. Descriptors without
explicit edges remain unchanged; absence of evidence is not evidence of no rewind.

CLI history uses a different rule: an explicit `last-prompt.leafUuid` plus a complete,
consistent parent graph can identify the saved selected ancestry inside one file.
Native samples confirmed a leaf after the assistant reply, not merely the last user
prompt. Local inspection did not find a `rewound` marker. Message reparenting also
creates branches, so the UI calls these earlier saved records without attributing a
user action. Missing links, compaction, uncertain roots, partial writes, and stale
pointers preserve all records. This conservative coverage is intentional.

The default reader and exports use verified selected ancestry. The historical reader
opts into all saved records, labels that view explicitly, and keeps it separate from
the current conversation. Original physical anchors and source files remain intact.
Claude follow returns a full selected-branch snapshot when selection excludes records;
consumers must reconcile removed entries rather than append blindly. Desktop grouping
is presentation metadata, not permission to migrate an Agent's supervision target.

Synthetic tests cover native mapping conflicts, independent forks, unavailable files,
CLI graph uncertainty, current versus historical rendering, previews, exports, and
follow reconciliation. The local acceptance preview isolates its state and caches
from the resident dashboard and uses existing source records read-only.

## Follow-up: working-directory drift and reader parity

A subsequent user report showed a missing rewind entry in the overlay reader.
Fresh overlay and standalone reads both displayed it, so the original screenshot's
precise cause was not recovered. A separate, reproducible defect was confirmed:
changing a card's display project from the Desktop root to a working subdirectory
removed its explicit rewind relationship. Native parent/child evidence had not changed.
The project guard now accepts directories within the Desktop root, using path-component
boundaries; unrelated roots, conflicting ownership, and invalid native edges still
fail open. This does not infer a relationship from directory containment alone.

The existing refresh test replaced the renderer, so it could not catch features
accidentally restricted to one view. It now also invokes the actual shipped renderer
for both overlay and standalone entry modes, checking previous-record navigation,
return-to-current links, and same-file historical views. This verifies generated
content, not browser layout; browser checks remain necessary for visual behavior.
No automatic page reload or new update prompt is introduced by this repair.
