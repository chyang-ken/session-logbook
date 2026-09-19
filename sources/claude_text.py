"""Shared cleanup for Claude's leading environment reminders."""
import re


_LEADING_REMINDERS = re.compile(r"^(?:\s*<system-reminder>.*?</system-reminder>\s*)+", re.DOTALL)


def strip_leading_reminders(text):
    """Remove complete leading wrappers only; preserve ordinary and unclosed text."""
    return _LEADING_REMINDERS.sub("", text)


def anchored_user_text(row):
    """Text used to count/render a Claude user anchor, excluding tool results."""
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
