"""Shared cleanup for Claude's leading environment reminders."""
import re


_LEADING_REMINDERS = re.compile(r"^(?:\s*<system-reminder>.*?</system-reminder>\s*)+", re.DOTALL)

# System-injection prefixes for user-role records that are not user input. The harness
# writes these events into JSONL as user messages, but semantically they belong to the
# system/agent side. Once recognized, classify them separately so they do not count as
# user input.
#   <command-*> / <local-command-*>   slash command injection + hook stdout
#   <system-reminder>                 system prompt
#   <task-notification>               background-task completion notice
#   <bash-stdout> / <bash-stderr>     bash-mode output from `!command`; unlike <bash-input>
#   <teammate-message ...>            teammate agent report with variable attributes
# These live here rather than in server.py because the anchored transcript, the history
# index and the HTTP layer all have to agree on what counts as a human turn; two copies
# of this list is how [U#] came to disagree with user_turn_count.
SYSTEM_USER_PREFIXES_SKIP = ("<local-command-", "<command-", "<system-reminder>")
SYSTEM_USER_PREFIXES_EVENT = ("<task-notification>", "<bash-stdout>", "<bash-stderr>", "<teammate-message")


def is_system_user_string(stripped):
    """Whether user-role string content is a system-side injection rather than real user input. stripped should be lstrip output."""
    return stripped.startswith(SYSTEM_USER_PREFIXES_SKIP) or stripped.startswith(SYSTEM_USER_PREFIXES_EVENT)


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
    """Text used to count/render a Claude user anchor, excluding tool results.

    System-side pseudo-messages the harness files under the user role - a background-task
    notification, bash-mode output, a teammate report, a slash-command injection - are not
    human turns and return '' here. Callers render them as events instead, so [U#] counts
    the same turns the dashboard's user_turn_count does.
    """
    row = normalize_record(row)
    if row.get('type') != 'user' or row.get('isMeta'):
        return ''
    content = (row.get('message') or {}).get('content')
    if isinstance(content, list):
        if any(isinstance(block, dict) and block.get('type') == 'tool_result' for block in content):
            return ''
        text = ' '.join(
            strip_leading_reminders(block.get('text', ''))
            if isinstance(block, dict) and block.get('type') == 'text' else
            '[image]' if isinstance(block, dict) and block.get('type') == 'image' else ''
            for block in content).strip()
    else:
        text = strip_leading_reminders(content or '')
    return '' if is_system_user_string(text.lstrip()) else text
