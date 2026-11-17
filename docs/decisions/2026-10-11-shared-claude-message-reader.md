# One semantic reader for Claude conversation surfaces

## Decision and authorization

The maintainer requested that browser and CLI parsing be unified, with only presentation
remaining different, after a native session-message body needed fixes in several readers.
Implement that request in `sources/claude_messages.py`. This change covers Claude conversation
reading; it does not replace other source adapters or change the read-only product boundary.
Local implementation and validation do not imply deployment authorization.

`claude_history.records` continues to own normalization, branch selection, duplicate removal,
physical lines and human-turn positions. `claude_messages.read` consumes that selected history
and owns message/body classification, full authored text, queue confirmation, tool-call/receipt
association, QA and Skill-body recognition. It uses the existing `claude_text` and
`claude_events` rules, including the single human-turn predicate.

The browser (`extract_conversation`), Markdown export (`extract_transcript`) and anchored
renderer (`render_claude`) consume this model. The CLI passes its line cursor to the anchored
renderer; the packaged Skill still calls the CLI. Source-specific message-name or payload
handling belongs in the shared reader or its helpers, never in those renderers.

## Why this boundary

Sharing only low-level text helpers still left three independent loops deciding what a
native record meant and which receipt belonged to which call. A new transport could be fixed
in one loop while the others kept showing only its short tool summary. Sharing the browser's
rendered turns instead would apply its truncation to the CLI and lose source anchors and
thinking/tool evidence. The common model therefore retains complete text and original lines;
renderers retain their existing layout, omission and truncation policies.

A missing or out-of-slice receipt remains unconfirmed. A result-only delta retains the native
call ID without manufacturing an earlier call. Source transcripts and personal state are
never rewritten. No cache schema bump is needed: metadata, search text and the human-turn rule
are unchanged.

## Presentation and compatibility

- Browser turns keep their existing ordering, folding, result limits and omission of thinking.
- Markdown keeps authored bodies in full and retains its shorter tool receipts. Its former
  classification of proactive Skill bodies as generic system notes is removed: it now uses
  the browser's Skill classification and keeps that injected body in full.
- Anchored output keeps physical `[L#]`, human `[U#]`, action summaries and binary redaction.
  It still omits injected metadata/Skill bodies. Legacy generic channel tools retain their
  compact action display; recognized Project/session-message bodies remain complete.
- API responses, CLI flags and the Skill entry point do not change.

## Evidence

- `tests/test_claude_project_reading.py` exercises full bodies, successful/failed/missing
  receipts, receipt-only deltas, and the CLI/Skill routes using synthetic records. It also
  changes only the shared message classifier to prove that all reading surfaces receive a
  newly recognized message, and checks common proactive Skill classification.
- `tests/test_history_entry_point.py` guards the shared entry point against direct native
  parsing in the three renderers.
- A frozen maintainer sample of 10,817 source lines produced exactly the same 1,819 browser
  turns and byte-identical anchored output before and after this change. Markdown differs
  only where two formerly shortened system notes become complete Skill bodies, apart from
  the isolated temporary source-path header. Private sample contents are not committed.
- Validation commands: `python3 -m unittest discover -s tests` and
  `python3 scripts/check_no_cjk.py`.

Commit: the commit introducing this decision records its implementation and validation.
