"""Shared cleanup for Claude's leading environment reminders."""
import re


_LEADING_REMINDERS = re.compile(r"^(?:\s*<system-reminder>.*?</system-reminder>\s*)+", re.DOTALL)


def strip_leading_reminders(text):
    """Remove complete leading wrappers only; preserve ordinary and unclosed text."""
    return _LEADING_REMINDERS.sub("", text)


def normalize_record(row):
    """Expose delivered human interruptions as user messages, not queue events.

    Desktop persists an interruption as an attachment after delivery. Its rendered
    system-reminder is transport wrapping; the attachment prompt is the human text.
    Keep original identity and coordinates so raw evidence still points to the source.
    """
    if not isinstance(row, dict) or row.get('type') != 'attachment' or row.get('isMeta'):
        return row
    attachment = row.get('attachment')
    if not isinstance(attachment, dict):
        return row
    origin = attachment.get('origin')
    prompt = attachment.get('prompt')
    if (attachment.get('type') != 'queued_command'
            or attachment.get('commandMode') != 'prompt'
            or not isinstance(origin, dict) or origin.get('kind') != 'human'
            or not isinstance(prompt, str) or not prompt.strip()):
        return row
    return dict(row, type='user', message={'role': 'user', 'content': prompt})


def anchored_user_text(row):
    """Text used to count/render a Claude user anchor, excluding tool results."""
    row = normalize_record(row)
    if row.get('type') != 'user' or row.get('isMeta'):
        return ''
    content = (row.get('message') or {}).get('content')
    if isinstance(content, list):
        if any(isinstance(block, dict) and block.get('type') == 'tool_result' for block in content):
            return ''
        return ' '.join(
            strip_leading_reminders(block.get('text', ''))
            if isinstance(block, dict) and block.get('type') == 'text' else
            '[image]' if isinstance(block, dict) and block.get('type') == 'image' else ''
            for block in content).strip()
    return strip_leading_reminders(content or '')
