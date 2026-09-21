#!/usr/bin/env python3
"""Render a session jsonl into a navigable transcript with original-line anchors (pure, read-only).

See docs/philosophy.md ("context reduction — for whom"): this is the standard reduced form
**for agents to read** — not a replacement for the raw jsonl, but a navigation layer carrying
original coordinates. Every line is tagged with `[U#]` (the Nth human turn) / `[L#]` (the
original line number). User and Assistant message text is preserved in full. Tool actions retain
their target path, search scope, or command prefix. Successful tool-result bodies collapse to
status and size; leading error text remains visible. Thinking, injected context, and binary
content are reduced. An agent can use `[L#]` to recover any hidden detail from the raw jsonl.

Sources:
- `render_claude(path)` — Claude Code session jsonl (`~/.claude/projects/...`)
- `render_codex(path)`  — Codex rollout jsonl (`~/.codex/sessions/...`)
- `render_kimi(path)`   — Kimi Code CLI wire jsonl (`~/.kimi-code/sessions/.../agents/main/wire.jsonl`)
- `render_pi(path)`     — Pi session jsonl (selected branch only)
- `render_antigravity(path)` — Antigravity transcript jsonl
  (`~/.gemini/antigravity/brain/<id>/.system_generated/logs/transcript.jsonl`), live history only

All produce the **same anchored-transcript format**. The module has zero heavy dependencies
(only json/re) and is the single source of truth behind the server's `/anchored` download
endpoint.

⚠ Note when changing this: any downstream that reads the anchored transcript depends on its
**verbatim output** (truncation constants, anchor format, injection-filtering rules). Verify
byte-level diffs before and after a change.
"""
from __future__ import annotations

from pathlib import Path
import json
import re
from datetime import datetime, timezone
from sources import claude_events
from sources.claude_text import strip_leading_reminders, anchored_user_text, user_record_text


def trunc(s, n):
    if s is None:
        return ""
    s = str(s)
    return s if len(s) <= n else s[:n] + f" …[+{len(s)-n} chars truncated]"


def line_ranges(lines) -> str:
    """Render physical line numbers as compact anchors: `L5-L9, L14`; empty gives `none`.

    One spelling for "these source lines", shared by the digest header and the Agent CLI, so
    a reader parses a single format wherever anchors are reported in bulk.
    """
    spans, start, prev = [], None, None
    for line in sorted(set(lines)):
        if start is None:
            start = prev = line
        elif line == prev + 1:
            prev = line
        else:
            spans.append((start, prev))
            start = prev = line
    if start is not None:
        spans.append((start, prev))
    return ", ".join(f"L{a}" if a == b else f"L{a}-L{b}" for a, b in spans) or "none"


# ───────────────────────── Turn-level addressing ─────────────────────────

# The one spelling of a rendered user-turn header, in every shape the renderers below
# produce: the ━━ banner (Claude / Codex / Kimi / Antigravity), the bare line Pi writes,
# and the `##` heading Devin writes. `[L#]` is a physical source line, `[N#]` a Devin
# database node. This transcript already decides what a human turn is; a caller that wants
# only some of those turns must be able to say so in the same `[U#]` the renderer printed,
# rather than translating it back into line numbers the renderer never asked it to know.
USER_TURN_RE = re.compile(r"^(?:━+\s*|##\s*)?\[U(\d+)\]\s+\[[LN]\d+\]\s+USER\b")


def turn_anchors(body):
    """[(turn number, index of its header line)] over a rendered transcript body."""
    found = []
    for index, line in enumerate(body.splitlines()):
        match = USER_TURN_RE.match(line)
        if match:
            found.append((int(match.group(1)), index))
    return found


def slice_from_turn(body, first_turn=None, last_turns=None):
    """Cut a rendered body down to the turns asked for; return `(body, notes)`.

    `notes` are header lines the caller must emit. A reader handed a cut transcript has to
    be told it is one and what is missing: silently returning less than the whole history is
    how a fragment gets mistaken for the record. An out-of-range or unanswerable request
    raises instead of quietly returning everything -- a parameter that is accepted and then
    ignored teaches a caller to trust a bound that was never applied.
    """
    if first_turn is None and last_turns is None:
        return body, []
    anchors = turn_anchors(body)
    if not anchors:
        raise ValueError(
            "this transcript renders no [U#] human turn, so it cannot be sliced by turn; "
            "read it whole, or bound it by line with the cursor options"
        )
    lowest, total = anchors[0][0], anchors[-1][0]
    if last_turns is not None:
        if last_turns < 1:
            raise ValueError("--last-turns must be 1 or more")
        wanted = max(lowest, total - last_turns + 1)
    else:
        if first_turn < 1:
            raise ValueError("--from-turn must be U1 or later")
        if first_turn > total:
            raise ValueError(
                f"turn U{first_turn} is beyond this transcript's last turn U{total}"
            )
        wanted = max(lowest, first_turn)
    start = next(index for number, index in anchors if number >= wanted)
    kept = [number for number, _ in anchors if number >= wanted]
    notes = [
        f"# TURN_SLICE: U{kept[0]}-U{total} ({len(kept)} of {len(anchors)} rendered turns)",
        *([f"# OMITTED_BEFORE_SLICE: U{lowest}-U{kept[0] - 1}"] if kept[0] > lowest else
          ["# OMITTED_BEFORE_SLICE: none; the request covered every rendered turn"]),
        "# A [U#] numbers this transcript only. A rewind mints a new record that renumbers "
        "from U1, so a turn number addresses a position inside this snapshot and is not a "
        "durable cursor; follow new work with NEXT_CURSOR.",
    ]
    return "\n".join(body.splitlines()[start:]).lstrip("\n"), notes


def parse_turn_ref(value):
    """`U13` or `13` -> 13. One spelling in, whichever the caller copied out of a transcript."""
    text = str(value).strip()
    digits = text[1:] if text[:1] in ("U", "u") else text
    if not digits.isdigit():
        raise ValueError(f"not a turn reference: {value!r}; use U13 or 13")
    return int(digits)


# ───────────────────────── Self-describing header (download artifact only) ─────────────────────────

def digest_header(jsonl_path, source="claude", source_files=None) -> str:
    """Add a self-describing header to the download artifact: so a cold recipient with no context on
    this repo (another agent) can understand it from this .txt alone —— how to read the anchors,
    where the original file is, and where to recover the truncated detail.

    ⚠ The header is **prepended only to the /anchored download endpoint's artifact**, never into the
    render_claude/render_codex body: the offline session-review pipeline relies on its prompt template
    knowing the anchor semantics, and its "byte-for-byte identical" contract must not be broken.
    See docs/handoffs/2026-06-11-anchored-render-to-service-layer.md.
    """
    label = {"codex": "Codex", "kimi": "Kimi Code", "pi": "Pi",
             "antigravity": "Antigravity"}.get(source, "Claude Code")
    lines = [
        "# ┌─ COMPACT SESSION DIGEST ─────────────────────────────────────────",
        f"# │ Navigable transcript of a {label} session. User and Assistant messages",
        "# │ are complete. Tool targets and command prefixes stay visible; successful",
        "# │ result bodies collapse to status/size and remain expandable by [L#].",
        "# │",
        f"# │ SOURCE (full, authoritative): {jsonl_path}",
        "# │ To expand any point, open that file at the cited [L#] line.",
        "# │",
        "# │ ANCHORS",
        "# │   [L<n>]                 line <n> in the SOURCE jsonl above — go there for full content",
        "# │   [U<n>]                 the <n>-th real user turn",
        "# │   …[+N chars truncated]  N more chars exist at that [L<n>] in the source",
        "# │ MARKERS",
        "# │   ━━ [U#] … ━━  user turn   🔧 tool action   ⮑ result status   💭 model thinking",
        "# │   [CONTEXT injected: …]   filtered boilerplate (AGENTS.md / env / permissions)",
        "# │",
        "# │ Generated by session-logbook · sources/anchored_transcript.py",
        "# └──────────────────────────────────────────────────────────────────",
    ]
    if source == 'codex':
        from sources import codex, codex_history
        files = codex_history.references(jsonl_path) if source_files is None else source_files
        lines = [line.replace('SOURCE (full, authoritative)', 'ENTRY SOURCE (one physical segment)')
                 .replace('line <n> in the SOURCE jsonl above', 'line <n> in the accompanying SOURCE file')
                 .replace('open that file at the cited [L#] line.', 'open the accompanying SOURCE file at [L#].') for line in lines]
        lines += ['# EFFECTIVE HISTORY SOURCES:']
        lines += [f"# {f['relation']}: {f['path']} L{f['first_line']}-L{f['last_line']} (session {f['session_id']})" for f in files]
        lines += ['# A source file path is not the stable Session ID; inherited sources belong to their own session.']
        meta = codex._read_session_meta(jsonl_path)
        forked = codex_history.fork_lineage(meta)
        if forked:
            at = forked['ordinal_exclusive']
            lines += ['# FORKED FROM SESSION: ' + forked['session_id'] +
                      (' at ordinal ' + str(at) if at is not None else ' (fork point not recorded)'),
                      '# This session branched off that one. Records before the fork point belong to it.']
        spawned = codex_history.spawn_lineage(meta)
        if spawned:
            parent = spawned['parent_session_id']
            lines += ['# SPAWNED BY SESSION: ' + (parent or 'unrecorded'),
                      '# This is a sub-agent thread Codex started for that session. It is a session of'
                      ' its own and this file is its whole record; nothing is inherited from elsewhere.']
    if source == 'antigravity' and Path(jsonl_path).is_file():
        from sources import antigravity
        abandoned = sorted(antigravity.abandoned_line_numbers(jsonl_path))
        lines += ['# HISTORY: live rows only. An Antigravity rewind never leaves this file; the rows',
                  '# it abandoned are not rendered below and are not part of the conversation.']
        lines += [f"# Abandoned by rewind: {len(abandoned)} raw lines"
                  + (f" at {line_ranges(abandoned)}" if abandoned else "")]
        if abandoned:
            lines += ['# Those lines are still in the SOURCE file; open them at their [L#] as evidence.']
        lines += ['# Antigravity may store a row with fields it already shortened itself '
                  '(truncated_fields);',
                  '# [L#] gives the full stored record, which can still be shorter than what ran.']
    if source == 'claude' and Path(jsonl_path).is_file():
        from sources import claude_history
        info = claude_history.describe(jsonl_path)
        lines += ['# HISTORY: selected file only; copied records appear once.']
        lines += ['# ' + info['history_note']]
        lines += ['# HISTORY_ISSUE: ' + json.dumps(issue, sort_keys=True) for issue in info['history_issues']]
        lines += [f"# RELATED SOURCE: {f['path']} L{f['first_line']}-L{f['last_line']} "
                  f"(session {f['session_id']}; {f.get('shared_records', 0)} shared records)"
                  for f in info['source_files'] if f['relation'] != 'selected']
        lines += [f"# COMPACTION: L{c['line']} trigger={c['trigger']}; earlier raw history is not reconstructed."
                  for c in info['compactions']]
    return "\n".join(lines)


# ───────────────────────── Claude Code ─────────────────────────

def _looks_b64(s):
    return isinstance(s, str) and len(s) > 2000 and re.fullmatch(r'[A-Za-z0-9+/=\s]+', s[:200] or '') is not None


def _tool_input_summary(name, inp):
    if not isinstance(inp, dict):
        return trunc(inp, 200)
    if name == 'Bash':
        return trunc(inp.get('command', ''), 300)
    if name in ('Read', 'Write', 'NotebookEdit'):
        return inp.get('file_path') or inp.get('notebook_path') or ''
    if name == 'Edit':
        return f"{inp.get('file_path','')}  old:{trunc(inp.get('old_string',''),60)!r} new:{trunc(inp.get('new_string',''),60)!r}"
    if name == 'Grep':
        return f"pattern={inp.get('pattern','')!r} path={inp.get('path','')} glob={inp.get('glob','')}"
    if name == 'Glob':
        return f"pattern={inp.get('pattern','')!r}"
    if name in ('Task', 'Agent'):
        return f"[{inp.get('subagent_type','')}] {trunc(inp.get('description',''),80)} :: {trunc(inp.get('prompt',''),200)}"
    if name == 'TodoWrite':
        todos = inp.get('todos', [])
        return "; ".join(trunc(t.get('content', ''), 50) for t in todos[:8]) if isinstance(todos, list) else trunc(inp, 150)
    if name == 'WebFetch':
        return f"{inp.get('url','')} :: {trunc(inp.get('prompt',''),100)}"
    if name == 'WebSearch':
        return trunc(inp.get('query', ''), 150)
    keys = list(inp.keys())
    return trunc("{" + ", ".join(f"{k}={trunc(inp[k],80)!r}" for k in keys[:5]) + "}", 250)


def _result_text(content):
    """tool_result content -> readable, strip images/b64."""
    if isinstance(content, str):
        if _looks_b64(content):
            return "[binary/base64 stripped]"
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if not isinstance(b, dict):
                parts.append(str(b))
                continue
            t = b.get('type')
            if t == 'text':
                tx = b.get('text', '')
                parts.append("[binary/base64 stripped]" if _looks_b64(tx) else tx)
            elif t == 'image':
                parts.append("[image]")
            else:
                parts.append(f"[{t}]")
        return "\n".join(parts)
    return str(content)


def _result_index(text, is_error=False):
    """Keep actionable result state without carrying successful payload bodies."""
    text = "" if text is None else str(text)
    if is_error:
        return trunc(text, 300)
    if text == "[binary/base64 stripped]":
        return text
    if not text:
        return "[empty]"
    return f"[{text.count(chr(10)) + 1} lines, {len(text)} chars hidden; use evidence]"


def _codex_output_is_error(output):
    """Recognize explicit structured failures without guessing from prose."""
    value = output
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return False
    if not isinstance(value, dict):
        return False
    if value.get("isError") is True or value.get("is_error") is True:
        return True
    exit_code = value.get("exit_code")
    return isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0


def render_pi(path) -> str:
    """Render Pi's selected branch with physical-file anchors, not synthetic rows."""
    from sources import pi
    output, user = [], 0
    for turn in pi.collect_turns(pi.read_session(path)[2]):
        line = turn["line"]
        kind = turn["type"]
        if kind == "user":
            user += 1
            output.append(f"\n[U{user}] [L{line}] USER {turn['ts']}\n{turn['text']}")
        elif kind == "assistant":
            output.append(f"[L{line}] ASSISTANT: {turn['text']}")
        elif kind == "tool":
            output.append(f"[L{line}] TOOL {turn['name']}: {turn['summary']}")
            if "result_line" in turn:
                status = "ERROR" if turn["is_error"] else "OK"
                result = _result_index(turn["result"], turn["is_error"])
                output.append(f"[L{turn['result_line']}] TOOL_RESULT {status}: {result}")
        else:
            output.append(f"[L{line}] [SYSTEM]: {trunc(turn['text'], 300)}")
    return "\n".join(output)


def render_antigravity(path) -> str:
    """Antigravity transcript jsonl → anchored-transcript string, same format as the others.

    Antigravity differences: a rewind stays inside the file, re-opening an earlier step and
    appending over it, so which rows exist at all is a decision — `sources.antigravity`
    makes it and this renderer only formats the rows it returns. Abandoned rows are not
    rendered; `[L#]` stays the file's own physical line, so an agent can still read one with
    `evidence`, and the digest header says how many were left out. Tool calls ride on a
    planner row and their results land on later rows, so each is anchored where it was
    written and `[L#]` stays ascending for a cursor-based reader.
    """
    from sources import antigravity
    uturn = 0
    o_lines = []
    for turn in antigravity.collect_turns(path):
        ln, kind = turn['line'], turn['type']
        if kind == 'user':
            uturn += 1
            o_lines.append("")
            o_lines.append(f"━━━━━━━━━━ [U{uturn}] [L{ln}] USER {turn['ts']} ━━━━━━━━━━")
            o_lines.append(turn['text'])
        elif kind == 'assistant':
            o_lines.append(f"[L{ln}] ASSISTANT: {turn['text']}")
        elif kind == 'think':
            o_lines.append(f"[L{ln}]   💭 THINK: {trunc(turn['text'], 1400)}")
        elif kind == 'tool':
            o_lines.append(f"[L{ln}]   🔧 {turn['name']}: {trunc(turn['summary'], 300)}")
        elif kind == 'result':
            status = 'ERROR' if turn['is_error'] else 'OK'
            o_lines.append(f"[L{ln}]   ⮑ RESULT {status} {turn['name']}: "
                           f"{_result_index(turn['text'], turn['is_error'])}")
        elif kind == 'compacted':
            o_lines.append(f"[L{ln}] [CONTEXT COMPACTED]")
    return "\n".join(o_lines)


def render_claude(path, records=None) -> str:
    """Claude Code session jsonl → anchored-transcript string.

    Records that are neither human speech nor model output - a queue entry, a background
    task notice, a connection failure - are emitted as ⚠ EVENT marker lines carrying their
    own [L#]. They never take a [U#]: an agent reading this transcript must be able to
    trust that every [U#] is something the person actually said and the model actually saw.
    """
    uturn = 0
    o_lines = []
    queued_lines = {}
    delivered = {}
    errors = claude_events.ApiErrorRun()

    def close_error_run():
        if errors.count:
            o_lines[errors.slot] = errors.prefix + errors.summary()
            errors.clear()

    from sources import claude_history
    for ln, o in (claude_history.records(path) if records is None else records):
        t = o.get('type')
        ts = (o.get('timestamp') or '')[:19]
        side = ' (subagent)' if o.get('isSidechain') else ''
        if o.get('isMeta'):
            continue

        handed_over = claude_events.delivered_text(o)
        if handed_over is not None:
            delivered[handed_over] = delivered.get(handed_over, 0) + 1

        error_label = claude_events.api_error_label(o)
        if error_label is not None:
            prefix = f"[L{ln}]   ⚠ EVENT{side} API_ERROR: "
            if errors.matches(error_label) and errors.slot == len(o_lines) - 1:
                errors.extend(o.get('timestamp') or '')
            else:
                close_error_run()
                o_lines.append(prefix + error_label)
                errors.open(error_label, o.get('timestamp') or '', len(o_lines) - 1, prefix)
            continue

        queued = claude_events.enqueued_text(o)
        if queued is not None:
            if claude_events.is_task_notification(queued):
                text = ('TASK_NOTIFICATION (queued, delivery not confirmed): '
                        + claude_events.parse_task_notification(queued))
            elif claude_events.is_queued_human_text(queued):
                text = 'QUEUED_INPUT (delivery not confirmed): ' + trunc(queued.strip(), 2000)
            else:
                continue
            o_lines.append(f"[L{ln}]   ⚠ EVENT{side} {text}")
            queued_lines.setdefault(queued, []).append(len(o_lines) - 1)
            continue

        notification = claude_events.notification_prompt(o)
        if notification is not None:
            o_lines.append(f"[L{ln}]   ⚠ EVENT{side} TASK_NOTIFICATION: "
                           + claude_events.parse_task_notification(notification))
            continue

        summary = claude_events.compact_summary_text(o)
        if summary is not None:
            # The client's own account of the turns it compacted away. Kept whole, because
            # a compaction can start a new file and then this is the only record in it of
            # what came before; but it takes no [U#], because nobody said it.
            o_lines.append(f"[L{ln}]   ⚠ EVENT{side} COMPACTION_SUMMARY "
                           f"(written by the client, {len(summary)} chars):")
            o_lines.append(summary)
            continue

        if t == 'user':
            msg = o.get('message') or {}
            content = msg.get('content')
            for b in (content if isinstance(content, list) else ()):
                if isinstance(b, dict) and b.get('type') == 'tool_result':
                    is_error = bool(b.get('is_error'))
                    status = 'ERROR' if is_error else 'OK'
                    txt = _result_index(_result_text(b.get('content')), is_error)
                    o_lines.append(f"[L{ln}]   ⮑ TOOL_RESULT {status}{side}: {txt}")
            # One rule decides a human turn, for the reader, the card count and this banner
            # alike. A record may hold a tool result and typed text; the text keeps its [U#].
            txt = anchored_user_text(o)
            if not txt:
                # Pseudo-messages the harness files under the user role keep their
                # content but lose their [U#]: a delivered task notice or an interrupt is
                # still worth reading, and still is not something the person said.
                raw = user_record_text(o)
                event = claude_events.parse_system_user_event(raw.lstrip()) if raw else None
                if event is not None:
                    kind = ('INTERRUPTED' if event.get('kind') == 'interrupt' else
                            {'system_notification': 'TASK_NOTIFICATION',
                             'bash_output': 'BASH_OUTPUT'}.get(event['type'], 'TEAMMATE_MESSAGE'))
                    o_lines.append(f"[L{ln}]   ⚠ EVENT{side} {kind}: {trunc(event['text'], 300)}")
                continue
            uturn = o.get('_logbook_user_turn', uturn + 1)
            o_lines.append("")
            o_lines.append(f"━━━━━━━━━━ [U{uturn}] [L{ln}] USER {ts}{side} ━━━━━━━━━━")
            o_lines.append(str(txt))
        elif t == 'assistant':
            msg = o.get('message') or {}
            for b in (msg.get('content') or []):
                if not isinstance(b, dict):
                    continue
                bt = b.get('type')
                if bt == 'text':
                    tx = (b.get('text') or '').strip()
                    if tx:
                        o_lines.append(f"[L{ln}] ASSISTANT{side}: {tx}")
                elif bt == 'thinking':
                    th = (b.get('thinking') or '').strip()
                    if th:
                        o_lines.append(f"[L{ln}]   💭 THINK{side}: {trunc(th, 1400)}")
                elif bt == 'tool_use':
                    nm = b.get('name', '?')
                    o_lines.append(f"[L{ln}]   🔧 {nm}{side}: {_tool_input_summary(nm, b.get('input'))}")
        elif t == 'system':
            tx = (o.get('content') or o.get('text') or '')
            if isinstance(tx, str) and tx.strip():
                o_lines.append(f"[L{ln}] [SYSTEM]: {trunc(tx.strip(), 300)}")
    close_error_run()
    dropped = set(claude_events.confirmed_queue_entries(queued_lines, delivered))
    if dropped:
        o_lines = [line for i, line in enumerate(o_lines) if i not in dropped]
    return "\n".join(o_lines)


# ───────────────────────── Codex ─────────────────────────

_INJECT_PREFIXES = ('<user_instructions', '<environment_context', '<permissions',
                    '# agents.md', '<user_shell', '<editor', '## my environment')


def _is_injected(text):
    t = (text or '').strip().lower()
    return any(t.startswith(p) for p in _INJECT_PREFIXES)


def _msg_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if not isinstance(b, dict):
                continue
            ty = b.get('type')
            if ty in ('input_text', 'output_text', 'text', 'summary_text'):
                out.append(b.get('text', ''))
            elif ty in ('input_image', 'image'):
                out.append('[image]')
        return "\n".join(out)
    return str(content)


def _codex_user_parts(content):
    """Split one Codex user message into injected context and real human text.

    Codex may place environment/AGENTS blocks and the real prompt in separate input_text
    blocks inside the same response_item. Classifying the concatenated string by its prefix
    drops the real prompt. Keep block boundaries until injection filtering is complete.
    """
    if not isinstance(content, list):
        text = _msg_text(content)
        return ([text] if _is_injected(text) else []), ("" if _is_injected(text) else text)
    injected = []
    human = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get('type')
        if kind not in ('input_text', 'text'):
            continue
        text = block.get('text', '')
        if _is_injected(text):
            injected.append(text)
        elif text:
            human.append(text)
    return injected, "\n".join(human)


def _fc_summary(name, args_raw):
    try:
        a = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
    except Exception:
        return trunc(args_raw, 250)
    if not isinstance(a, dict):
        return trunc(args_raw, 250)
    if name == 'exec_command':
        return trunc(a.get('cmd', ''), 300)
    if name == 'write_stdin':
        return f"stdin: {trunc(a.get('input') or a.get('chars') or a,120)}"
    if name == 'update_plan':
        steps = a.get('plan') or []
        return "; ".join(f"[{s.get('status','')[:4]}]{trunc(s.get('step',''),40)}" for s in steps[:8]) if isinstance(steps, list) else trunc(a, 200)
    keys = list(a.keys())
    return trunc("{" + ", ".join(f"{k}={trunc(a[k],80)!r}" for k in keys[:5]) + "}", 250)


def _clean_output(out):
    if not isinstance(out, str):
        out = json.dumps(out, ensure_ascii=False)
    # Remove Codex's Chunk ID / Wall time metadata prefix lines
    lines = out.split("\n")
    lines = [l for l in lines if not l.startswith(("Chunk ID:", "Wall time:", "Total output tokens:"))]
    return "\n".join(lines).strip()


def render_codex(path, records=None) -> str:
    """Codex rollout jsonl → anchored-transcript string in the same format as Claude.

    Codex differences: reasoning is encrypted and unreadable; AGENTS.md/env/permissions are injected
    as messages and must be filtered; tools are exec_command/apply_patch/update_plan/write_stdin;
    rollback/abort are strong dissatisfaction signals.
    """
    uturn = 0
    o_lines = []
    from sources import codex_history
    history = codex_history.load(path) if records is None else None
    rows = history['records'] if history is not None else records
    multi = len({r['path'] for r in rows}) > 1
    # Records inherited from another session (a user fork's pre-fork prefix) are labelled on
    # every source line, so a reader never takes a parent thread's turns for this session's.
    owners = {segment['path']: segment for segment in history['segments']} if history is not None else {}
    if history is not None and not history['complete']:
        o_lines.append('# CONTEXT_INCOMPLETE: ' + json.dumps(history['issues']))
    for entry in rows:
        ln, o = entry['line'], entry['record']
        if multi:
            owner = owners.get(entry['path']) or {}
            label = (f" INHERITED from session {owner['session_id']}"
                     if owner.get('relation') == 'inherited' else '')
            o_lines.append(f"[L{ln}] [SOURCE {entry['path']}{label}]")
        t = o.get('type')
        p = o.get('payload') or {}
        ts = (o.get('timestamp') or '')[:19]
        if t == 'session_meta':
            cwd = (p.get('cwd') or '')
            model = (p.get('model') or p.get('model_provider') or '')
            o_lines.append(f"[L{ln}] [SESSION_META] cwd={cwd} {ts}")
        elif t == 'response_item':
            pt = p.get('type')
            if pt == 'message':
                role = p.get('role')
                if role == 'developer':
                    continue
                if role == 'user':
                    injected, txt = _codex_user_parts(p.get('content'))
                    for context in injected:
                        o_lines.append(f"[L{ln}]   [CONTEXT injected: {trunc(context,80)}]")
                    if txt.strip():
                        uturn += 1
                        o_lines.append("")
                        o_lines.append(f"━━━━━━━━━━ [U{uturn}] [L{ln}] USER {ts} ━━━━━━━━━━")
                        o_lines.append(txt)
                elif role == 'assistant':
                    txt = _msg_text(p.get('content'))
                    if txt.strip():
                        o_lines.append(f"[L{ln}] ASSISTANT: {txt}")
            elif pt == 'reasoning':
                summ = _msg_text(p.get('summary'))
                if summ.strip():
                    o_lines.append(f"[L{ln}]   💭 THINK: {trunc(summ,1400)}")
                # encrypted reasoning with no summary is skipped (no information)
            elif pt == 'function_call':
                nm = p.get('name', '?')
                o_lines.append(f"[L{ln}]   🔧 {nm}: {_fc_summary(nm, p.get('arguments'))}")
            elif pt == 'function_call_output':
                raw_output = p.get('output')
                is_error = _codex_output_is_error(raw_output)
                status = 'ERROR' if is_error else 'OK'
                o_lines.append(
                    f"[L{ln}]   ⮑ OUTPUT {status}: "
                    f"{_result_index(_clean_output(raw_output), is_error)}"
                )
            elif pt == 'custom_tool_call':
                nm = p.get('name', '?')
                inp = p.get('input') or p.get('arguments')
                o_lines.append(f"[L{ln}]   🔧 {nm}: {trunc(inp,400)}")
            elif pt == 'custom_tool_call_output':
                raw_output = p.get('output')
                is_error = _codex_output_is_error(raw_output)
                status = 'ERROR' if is_error else 'OK'
                o_lines.append(
                    f"[L{ln}]   ⮑ OUTPUT {status}: "
                    f"{_result_index(_clean_output(raw_output), is_error)}"
                )
            elif pt == 'web_search_call':
                q = ''
                act = p.get('action') or {}
                if isinstance(act, dict):
                    q = act.get('query', '')
                o_lines.append(f"[L{ln}]   🔧 web_search: {trunc(q,150)}")
        elif t == 'event_msg':
            et = p.get('type')
            if et == 'thread_rolled_back':
                o_lines.append(f"[L{ln}] ⏪⏪ USER ROLLED BACK THREAD (strong dissatisfaction signal) {ts}")
            elif et == 'turn_aborted':
                o_lines.append(f"[L{ln}] ⛔ TURN ABORTED by user (interruption signal) {ts}")
            elif et == 'context_compacted':
                o_lines.append(f"[L{ln}] [CONTEXT COMPACTED]")
            # the remaining event_msg (token_count/agent_message/user_message/task_*) are redundant with response_item, skip
        elif t == 'compacted':
            o_lines.append(f"[L{ln}] [COMPACTED SUMMARY]")
    return "\n".join(o_lines)


# ───────────────────────── Kimi Code ─────────────────────────

def _kimi_ts(o):
    """Kimi rows carry `time` as epoch milliseconds; render like the other sources' ISO prefix."""
    t = o.get('time')
    if not isinstance(t, (int, float)):
        return ''
    try:
        return datetime.fromtimestamp(t / 1000.0, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
    except (OverflowError, OSError, ValueError):
        return ''


def _kimi_content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get('type') == 'text':
                out.append(b.get('text', ''))
            elif b.get('type') in ('image', 'input_image'):
                out.append('[image]')
        return "\n".join(out)
    return '' if content is None else str(content)


def _kimi_tool_summary(ev):
    name = ev.get('name', '?')
    args = ev.get('args') if isinstance(ev.get('args'), dict) else {}
    display = ev.get('display') if isinstance(ev.get('display'), dict) else {}
    if display.get('command'):
        return trunc(display['command'], 300)
    if name == 'Bash':
        return trunc(args.get('command', ''), 300)
    if display.get('path'):
        return trunc(display['path'], 300)
    if name in ('Read', 'Write', 'Edit', 'ReadMediaFile'):
        p = args.get('path') or args.get('file_path') or ''
        if name == 'Edit':
            return f"{p}  old:{trunc(args.get('old_string') or args.get('old_text') or '', 60)!r} new:{trunc(args.get('new_string') or args.get('new_text') or '', 60)!r}"
        return trunc(p, 300)
    if name == 'Grep':
        return f"pattern={args.get('pattern', '')!r} path={args.get('path', '')}"
    if name == 'Agent':
        return f"[{args.get('agent_name') or args.get('subagent_type') or ''}] {trunc(args.get('description', ''), 80)} :: {trunc(args.get('prompt', ''), 200)}"
    if name == 'AskUserQuestion':
        qs = args.get('questions') or []
        return " | ".join(trunc((q or {}).get('question', ''), 120) for q in qs if isinstance(q, dict))
    keys = list(args.keys())
    return trunc("{" + ", ".join(f"{k}={trunc(args[k], 80)!r}" for k in keys[:5]) + "}", 250)


def _kimi_result_text(result):
    """tool.result.result is {output: str | [{type,text}], isError?, note?}; accept a bare string too."""
    if isinstance(result, dict):
        out = result.get('output')
        text = out if isinstance(out, str) else _kimi_content_text(out)
        if not text and result.get('error'):
            text = str(result['error'])
        return text or ''
    return _kimi_content_text(result)


def render_kimi(path) -> str:
    """Kimi Code CLI wire jsonl → anchored-transcript string in the same format as Claude/Codex.

    Kimi differences: the human prompt is `turn.prompt` (origin.kind == user); the same text is
    re-appended as `context.append_message`, which is skipped as a duplicate unless its origin is
    an injection (then marked as filtered context). Assistant reasoning arrives as `content.part`
    with part.type == think; visible reply as part.type == text. Tool calls/results are loop
    events paired by toolCallId. AskUserQuestion shows as the question plus the resolved answer.
    """
    uturn = 0
    o_lines = []
    ln = 0
    asked = set()  # toolCallIds already shown as a 🔧 AskUserQuestion line
    with open(path, 'r', errors='replace') as f:
        for raw in f:
            ln += 1
            raw = raw.strip()
            if not raw:
                continue
            try:
                o = json.loads(raw)
            except Exception:
                continue
            t = o.get('type')
            ts = _kimi_ts(o)
            if t == 'metadata':
                o_lines.append(f"[L{ln}] [SESSION_META] protocol={o.get('protocol_version', '')} {ts}")
            elif t in ('profile.bind', 'config.update'):
                cwd = ((o.get('environmentDisclosure') or {}).get('cwd') if t == 'profile.bind' else o.get('cwd')) or ''
                model = o.get('modelAlias') or ''
                if cwd or model:
                    o_lines.append(f"[L{ln}] [SESSION_META] cwd={cwd} model={model} {ts}")
            elif t == 'turn.prompt':
                kind = (o.get('origin') or {}).get('kind')
                txt = _kimi_content_text(o.get('input'))
                if kind == 'user':
                    uturn += 1
                    o_lines.append("")
                    o_lines.append(f"━━━━━━━━━━ [U{uturn}] [L{ln}] USER {ts} ━━━━━━━━━━")
                    o_lines.append(txt)
                # non-user prompts (task / system_trigger) are re-appended as context.append_message
                # with the same origin and are rendered there, once, as injected context
            elif t == 'context.append_message':
                msg = o.get('message') or {}
                kind = (msg.get('origin') or {}).get('kind')
                # origin user duplicates the turn.prompt above; other origins are harness injections
                if msg.get('role') == 'user' and kind != 'user':
                    txt = _kimi_content_text(msg.get('content'))
                    if txt.strip():
                        o_lines.append(f"[L{ln}]   [CONTEXT injected ({kind or 'system'}): {trunc(txt, 80)}]")
            elif t == 'context.append_loop_event':
                ev = o.get('event') or {}
                et = ev.get('type')
                if et == 'content.part':
                    part = ev.get('part') or {}
                    if part.get('type') == 'think':
                        th = (part.get('think') or '').strip()
                        if th:
                            o_lines.append(f"[L{ln}]   💭 THINK: {trunc(th, 1400)}")
                    elif part.get('type') == 'text':
                        tx = (part.get('text') or '').strip()
                        if tx:
                            o_lines.append(f"[L{ln}] ASSISTANT: {tx}")
                elif et == 'tool.call':
                    nm = ev.get('name', '?')
                    if nm == 'AskUserQuestion':
                        asked.add(ev.get('toolCallId'))
                    o_lines.append(f"[L{ln}]   🔧 {nm}: {_kimi_tool_summary(ev)}")
                elif et == 'tool.result':
                    res = ev.get('result')
                    is_error = isinstance(res, dict) and res.get('isError') is True
                    status = 'ERROR' if is_error else 'OK'
                    o_lines.append(f"[L{ln}]   ⮑ RESULT {status}: {_result_index(_kimi_result_text(res), is_error)}")
            elif t == 'interaction.request':
                # The question text is already on the 🔧 AskUserQuestion line when the tool.call was logged
                tcid = o.get('toolCallId') or (o.get('request') or {}).get('toolCallId')
                if tcid in asked:
                    continue
                qs = ((o.get('request') or {}).get('questions')) or []
                qtext = " | ".join(trunc((q or {}).get('question', ''), 200) for q in qs if isinstance(q, dict))
                if qtext:
                    o_lines.append(f"[L{ln}]   ❓ ASK USER: {qtext}")
            elif t == 'interaction.resolved':
                answers = ((o.get('response') or {}).get('answers')) or {}
                if isinstance(answers, dict) and answers:
                    atext = " | ".join(f"{trunc(q, 80)} → {trunc(a, 200)}" for q, a in answers.items())
                    o_lines.append(f"[L{ln}]   ⮑ USER ANSWERED: {atext}")
            elif t == 'turn.cancel':
                o_lines.append(f"[L{ln}] ⛔ TURN CANCELLED by user (interruption signal) {ts}")
            elif t == 'turn.ended':
                o_lines.append(f"[L{ln}] [TURN ENDED: {o.get('reason', '')}] {ts}")
            elif t == 'context.apply_compaction':
                o_lines.append(f"[L{ln}] [CONTEXT COMPACTED]")
    return "\n".join(o_lines)
