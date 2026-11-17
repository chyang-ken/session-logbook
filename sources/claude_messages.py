"""One semantic reader for Claude conversation surfaces.

History selection belongs to claude_history. This module decodes the selected records,
classifies message bodies, removes confirmed queue copies, and associates tool receipts.
It never truncates authored text. Browser, Markdown, and anchored renderers consume this
model; they do not inspect native message/content/input fields themselves.
"""
from sources import claude_events, claude_history
from sources.claude_text import (anchored_user_text, project_reply_text, project_wake_words,
                                 session_message_text, strip_leading_reminders)


def _message_call(block):
    """Decode every supported authored-message tool in one place."""
    text = session_message_text(block)
    inputs = block.get('input')
    if text is not None:
        return {'kind': 'SESSION_MESSAGE', 'text': text, 'audience': 'session',
                'context': {k: v for k, v in inputs.items() if k != 'message'}}
    text = project_reply_text(block)
    if text is not None:
        return {'kind': ('PROJECT_EDIT' if block['name'] == 'mcp__hearthbot__update_message'
                         else 'PROJECT_REPLY'), 'text': text, 'audience': 'human',
                'message_id': inputs.get('message_id')}
    # Preserve the reader's older generic channel-tool support. This is a distinct
    # semantic kind so its legacy display policy does not become a second parser.
    name = block.get('name')
    if (isinstance(name, str) and isinstance(inputs, dict)
            and name.endswith(('__reply', '__post_message'))):
        text = inputs.get('text')
        if isinstance(text, str) and text.strip():
            return {'kind': 'CHANNEL_REPLY', 'text': text, 'audience': 'human'}
    return None


def _result_parts(content):
    """Decode result blocks once, retaining non-text markers for presentation policies."""
    if not isinstance(content, list):
        return [{'kind': 'text', 'text': str(content)}]
    parts = []
    for block in content:
        if not isinstance(block, dict):
            parts.append({'kind': 'value', 'text': str(block), 'string': isinstance(block, str)})
        elif block.get('type') == 'text':
            parts.append({'kind': 'text', 'text': block.get('text', '')})
        elif block.get('type') == 'image':
            parts.append({'kind': 'image', 'text': '[image]'})
        else:
            parts.append({'kind': 'other', 'text': f"[{block.get('type')}]"})
    return parts


def result_text(parts):
    """The reader's plain result body; binary redaction is an anchored-display policy."""
    return '\n'.join(part['text'] for part in parts
                     if part['kind'] in ('text', 'image') or part.get('string'))


def _tool(block, entry):
    name = block.get('name', '?')
    inputs = block.get('input', {})
    call = {'kind': 'tool', 'id': block.get('id', ''), 'name': name, 'input': inputs,
            'line': entry['line'], 'timestamp': entry['timestamp'],
            'message': _message_call(block), 'receipt': None}
    if name == 'Agent':
        inp = inputs if isinstance(inputs, dict) else {}
        call['spawn'] = {'name': inp.get('subagent_type') or 'general-purpose',
                         'description': inp.get('description') or (inp.get('prompt') or '')[:120]}
    call['is_question'] = name == 'AskUserQuestion'
    call['is_skill'] = name == 'Skill'
    return call


def read(path, *, first=0, include_rewound=False, records=None):
    """Return the complete semantic model for the requested selected-history slice.

    A receipt outside the slice is not inferred. Its native call ID survives in a
    result-only delta so a consumer can join it to the call it read earlier.
    `records` is an optional selected-record feed for callers with a frozen snapshot.
    """
    source = (claude_history.records(path, first, include_rewound=include_rewound)
              if records is None else records)
    return parse(source)


def parse(records):
    """Decode normalized, selected (physical line, record) pairs without source writes."""
    entries, calls, queued, delivered = [], {}, {}, {}
    project_path = None
    custom_titles = []
    next_is_skill = pending_skill_body = False
    user_turn = 0

    for line, row in records:
        entry = {'line': line, 'timestamp': row.get('timestamp') or '',
                 'is_meta': bool(row.get('isMeta')), 'sidechain': bool(row.get('isSidechain')),
                 'kind': 'ignored'}
        entries.append(entry)
        if project_path is None and row.get('cwd'):
            project_path = row['cwd']
        if row.get('type') == 'custom-title':
            custom_titles.append(row.get('customTitle'))
        handed_over = claude_events.delivered_text(row)
        if handed_over is not None:
            delivered[handed_over] = delivered.get(handed_over, 0) + 1
        relay = claude_events.project_relay_event(row)
        if relay is not None:
            entry.update(kind='relay', event=relay)
            continue
        error = claude_events.api_error_label(row)
        if error is not None:
            entry.update(kind='api_error', text=error)
            continue
        enqueued = claude_events.enqueued_text(row)
        if enqueued is not None:
            if claude_events.is_task_notification(enqueued):
                entry.update(kind='notification', delivered=False,
                             text=claude_events.parse_task_notification(enqueued))
            elif claude_events.is_queued_human_text(enqueued):
                entry.update(kind='queued_input', text=enqueued.strip())
            else:
                continue
            queued.setdefault(enqueued, []).append(entry)
            continue
        notification = claude_events.notification_prompt(row)
        if notification is not None:
            entry.update(kind='notification', delivered=True,
                         text=claude_events.parse_task_notification(notification))
            continue
        summary = claude_events.compact_summary_text(row)
        if summary is not None:
            entry.update(kind='compact_summary', text=summary)
            next_is_skill = pending_skill_body = False
            continue

        content = (row.get('message') or {}).get('content')
        if row.get('type') == 'assistant' and isinstance(content, list):
            entry.update(kind='assistant', blocks=[])
            for block in content:
                if not isinstance(block, dict):
                    continue
                kind = block.get('type')
                if kind in ('text', 'thinking'):
                    value = block.get('text' if kind == 'text' else 'thinking') or ''
                    if value:
                        entry['blocks'].append({'kind': kind, 'text': value})
                elif kind == 'tool_use':
                    call = _tool(block, entry)
                    entry['blocks'].append(call)
                    if isinstance(call['id'], str) and call['id']:
                        calls[call['id']] = call
                    if call['is_skill']:
                        pending_skill_body = True
            continue

        if row.get('type') == 'user':
            entry.update(kind='user_record', results=[], body=None)
            parts = []
            is_list = isinstance(content, list)
            if isinstance(content, str):
                parts = [strip_leading_reminders(content)]
            elif is_list:
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    kind = block.get('type')
                    if kind == 'tool_result':
                        call_id = block.get('tool_use_id')
                        call = calls.pop(call_id, None) if isinstance(call_id, str) else None
                        chunks = _result_parts(block.get('content', ''))
                        receipt = {'line': line, 'timestamp': entry['timestamp'],
                                   'is_meta': entry['is_meta'], 'id': call_id, 'parts': chunks, 'text': result_text(chunks),
                                   'is_error': block.get('is_error', False), 'qa': None}
                        if call is not None:
                            call['receipt'] = receipt
                            if call['is_question']:
                                receipt['qa'] = build_qa(call['input'], row.get('toolUseResult'))
                        entry['results'].append({'call': call, 'receipt': receipt})
                    elif kind == 'text':
                        text = strip_leading_reminders(block.get('text') or '')
                        if text and not text.lstrip().startswith('<system-reminder>'):
                            parts.append(text)
                    elif kind == 'image':
                        parts.append('[image]')

            full = '\n'.join(parts).strip()
            if not full:
                # A pure result row does not consume a pending Skill body.
                if not is_list:
                    next_is_skill = False
                continue
            if full.startswith('<command-'):
                next_is_skill = not is_list
                if is_list:
                    pending_skill_body = False
                continue
            if full.startswith(('<local-command-', '<system-reminder>')):
                next_is_skill = False
                if is_list:
                    pending_skill_body = False
                continue
            event = claude_events.parse_system_user_event(full)
            if event is not None:
                entry['body'] = {'kind': 'event', 'event': event}
            else:
                human = anchored_user_text(row)
                skill = (next_is_skill or (is_list and (pending_skill_body
                         or full.startswith('Base directory for this skill: '))))
                kind = 'user' if human else 'skill' if skill else 'harness_note'
                body = {'kind': kind, 'text': project_wake_words(full) if human else full}
                if human:
                    user_turn = row.get('_logbook_user_turn', user_turn + 1)
                    body.update(user_turn=user_turn, inline_text=project_wake_words(human))
                elif is_list and entry['is_meta'] and full == 'Continue from where you left off.':
                    body['resume_note'] = True
                entry['body'] = body
            next_is_skill = False
            if is_list:
                pending_skill_body = False
            continue

        if row.get('type') == 'system':
            text = row.get('content') or row.get('text') or ''
            if isinstance(text, str) and text.strip():
                entry.update(kind='system', text=text.strip())

    # Queue confirmation is a source fact, shared by every presentation.
    for entry in claude_events.confirmed_queue_entries(queued, delivered):
        entry['suppressed'] = True
    return {'records': entries, 'project_path': project_path or '', 'custom_titles': custom_titles}


def build_qa(tool_use_input, tool_use_result):
    """Build a structured QA unit from AskUserQuestion input plus the user's answer snapshot.

    Returns a dict with questions: [{question, header, multiSelect, options, answer, matched_option}].
    matched_option is the index in options; None means a free-form Other answer, often the
    most important user correction signal. Return None on upstream anomalies so the caller
    can fall back to the generic tool path.
    """
    questions_in = (tool_use_input or {}).get('questions') or []
    if not questions_in:
        return None
    answers_map = {}
    if isinstance(tool_use_result, dict):
        raw_ans = tool_use_result.get('answers')
        if isinstance(raw_ans, dict):
            answers_map = raw_ans

    out_q = []
    for q in questions_in:
        if not isinstance(q, dict):
            continue
        qtext = q.get('question', '') or ''
        options = []
        for opt in q.get('options') or []:
            if not isinstance(opt, dict):
                continue
            options.append({
                'label': opt.get('label', '') or '',
                'description': opt.get('description', '') or '',
                'preview': opt.get('preview') or None,
            })
        ans = answers_map.get(qtext, '') or ''
        matched = None
        for i, opt in enumerate(options):
            if opt['label'] and opt['label'] == ans:
                matched = i
                break
        out_q.append({
            'question': qtext,
            'header': q.get('header', '') or '',
            'multiSelect': bool(q.get('multiSelect')),
            'options': options,
            'answer': ans,
            'matched_option': matched,
        })

    if not out_q:
        return None
    return {'questions': out_q}
