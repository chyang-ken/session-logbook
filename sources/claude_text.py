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

# The record the client writes when the person presses Esc. It is filed under the user role
# and the model does receive it, but nobody typed it: it reports that something happened.
# The whole text has to be the marker - a bracketed literal on one line - so a person who
# quotes it inside a longer message still gets their turn. The two wordings in use are
# "[Request interrupted by user]" and "[Request interrupted by user for tool use]"; the
# pattern leaves room for a third without a code change.
_INTERRUPT_MARKER = re.compile(r"\[Request interrupted by user[^\]\n]*\]")


def interrupt_marker_text(stripped):
    """The marker's own wording without its brackets, or '' when the text is not one."""
    found = _INTERRUPT_MARKER.fullmatch((stripped or "").strip())
    return found.group(0)[1:-1] if found else ""


def is_system_user_string(stripped):
    """Whether user-role string content is a system-side injection rather than real user input. stripped should be lstrip output."""
    return (stripped.startswith(SYSTEM_USER_PREFIXES_SKIP)
            or stripped.startswith(SYSTEM_USER_PREFIXES_EVENT)
            or bool(interrupt_marker_text(stripped)))


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


def is_compact_summary(row):
    """Whether the client wrote this record to stand in for the turns it compacted away.

    The flag is the evidence, not the opening sentence: a person can type "This session is
    being continued", and a future client can reword its summary.
    """
    return isinstance(row, dict) and bool(row.get('isCompactSummary'))


def user_record_text(row, joiner=' ', image_placeholder='[image]'):
    """Readable text of a user-role record: leading reminders stripped, images as [image].

    ``tool_result`` blocks are skipped rather than disqualifying the record. A record can
    carry both a tool result and something the person typed, and the typed part is still
    theirs; callers that care about the tool result read the blocks themselves.
    """
    content = (row.get('message') or {}).get('content')
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get('type') == 'text':
                parts.append(strip_leading_reminders(block.get('text') or ''))
            elif block.get('type') == 'image':
                parts.append(image_placeholder)
        return joiner.join(part for part in parts if part).strip()
    return strip_leading_reminders(content) if isinstance(content, str) else ''


def anchored_user_text(row):
    """The text of a human turn, or '' when this record is not one.

    This is the one rule for "did a person say this, and did the model receive it". The
    reader's `user` turns, the card's `user_turn_count`, the history index's `user_turn`
    and the anchored transcript's [U#] all ask it, so they cannot drift apart again.

    Not a human turn, and '' here:
    - anything the client marks ``isMeta`` - it wrote that record itself: a skill body, the
      "[Image: source: ...]" note that follows a pasted image, hook feedback, a message
      relayed from another session;
    - the summary the client marks ``isCompactSummary`` - it wrote that too, to replace the
      turns it compacted away; the model receives it, and nobody said it;
    - the user-role pseudo-messages in ``is_system_user_string``: a background-task notice,
      bash-mode output, a teammate report, a slash-command injection, an interrupt marker;
    - a record holding nothing but tool results.

    A human turn, even though it has no typed words: a message that is only an image.
    """
    row = normalize_record(row)
    if not isinstance(row, dict) or row.get('type') != 'user':
        return ''
    if row.get('isMeta') or is_compact_summary(row):
        return ''
    text = user_record_text(row)
    if not text.strip() or is_system_user_string(text.lstrip()):
        return ''
    return text


def human_turn_words(row):
    """Only the words the person typed in a human turn, or '' - for matching a search.

    The same verdict as :func:`anchored_user_text`, without the ``[image]`` placeholder:
    that word is ours, and a search for "image" must not find it in every pasted picture.
    """
    if not anchored_user_text(row):
        return ''
    return user_record_text(normalize_record(row), image_placeholder='')
