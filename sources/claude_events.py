"""Claude records that carry a fact but not a sentence anybody said.

Three record shapes reach a Claude transcript without being human speech or model
output. The reader used to drop all three, which quietly cost the reader evidence
about why a session stalled, and - for the queue - risked the opposite mistake:
showing text the person typed as if the agent had received it.

1. ``queue-operation`` / ``operation="enqueue"`` holds text typed while the agent was
   busy. When the agent picks the entry up, the same string is written again as a
   delivered record: an ordinary ``user`` record, or a ``queued_command`` attachment
   when it lands mid-turn. When no delivered copy exists in the file, the queue entry
   is the only evidence there is and **delivery was never confirmed**. The client also
   queues background-task notices through the same mechanism, so the queue carries both
   human text and machine text and the two must be told apart before either is shown.

2. A background-task completion notice arrives either as a ``queued_command``
   attachment whose ``commandMode`` is ``task-notification``, or, once delivered, as a
   ``user`` record whose string content opens with ``<task-notification>``. The second
   form has long been rendered as a system event; the first was dropped. Both are the
   harness reporting on itself.

3. ``system`` / ``subtype="api_error"`` records a connection or API failure. A retry
   repeats the record with ``retryAttempt`` incrementing towards ``maxRetries``, so a
   single outage writes a run of near-identical adjacent records.

What is deliberately *not* here: a rule that guesses humanity from language, tone or
script. The 2026-09-20 format audit found Chinese prose in machine records and English
prose in human ones. Every predicate below keys on an explicit structural marker the
client wrote - a record type, an ``origin.kind``, a ``commandMode``, an XML wrapper.
"""
import re

from sources.claude_text import (SYSTEM_USER_PREFIXES_EVENT, SYSTEM_USER_PREFIXES_SKIP,
                                 is_system_user_string)

__all__ = [
    'enqueued_text', 'delivered_text', 'notification_prompt', 'api_error_label',
    'parse_task_notification', 'parse_system_user_event', 'extract_inner_xml',
    'ApiErrorRun', 'QUEUED_INPUT_ROLE',
]

#: Search snippets for text that was queued but never confirmed delivered use this role.
#: Deliberately not ``user`` / ``you``: a role filter asking for what the person said
#: must not return something the agent may never have read.
QUEUED_INPUT_ROLE = 'queued'

_TASK_NOTIFICATION = '<task-notification>'


def _inner(text, tag):
    found = re.search(r'<%s>(.*?)</%s>' % (tag, tag), text or '', re.DOTALL)
    return found.group(1).strip() if found else ''


def enqueued_text(row):
    """The string a ``queue-operation`` enqueue record parked, else ``None``.

    ``dequeue`` and ``remove`` records are bookkeeping about an entry already seen at
    its ``enqueue``; reporting them too would double-count one queued item.
    """
    if not isinstance(row, dict) or row.get('type') != 'queue-operation':
        return None
    if row.get('operation') != 'enqueue':
        return None
    content = row.get('content')
    if not isinstance(content, str) or not content.strip():
        return None
    return content


def is_task_notification(text):
    """Whether a queued or delivered string is the harness reporting a finished task."""
    return isinstance(text, str) and text.lstrip().startswith(_TASK_NOTIFICATION)


def is_queued_human_text(text):
    """Whether queued text is something a person typed rather than an injected block.

    Everything the harness injects under the user role is prefixed with its own XML
    wrapper; anything else in the queue was typed.
    """
    return (isinstance(text, str) and bool(text.strip())
            and not is_system_user_string(text.lstrip()))


def delivered_text(row):
    """The exact string this record handed to the model, else ``None``.

    Compared verbatim against :func:`enqueued_text`, because the client copies the
    queued string across unchanged. Matching on the raw string rather than on a
    cleaned-up form is what keeps a delivered entry from being announced twice.
    """
    if not isinstance(row, dict) or row.get('isMeta'):
        return None
    if row.get('type') == 'user':
        content = (row.get('message') or {}).get('content')
        if isinstance(content, str) and content.strip():
            return content
    attachment = row.get('attachment')
    if isinstance(attachment, dict) and attachment.get('type') == 'queued_command':
        prompt = attachment.get('prompt')
        if isinstance(prompt, str) and prompt.strip():
            return prompt
    return None


def confirmed_queue_entries(parked_by_text, delivered):
    """Which parked queue entries a later delivered record already accounts for.

    ``parked_by_text`` maps an exact enqueued string to whatever the caller parked for it,
    in queue order; ``delivered`` counts how often each string was handed over. The
    confirmed entries come back flat, for the caller to remove from its own output. The
    queue is first-in-first-out, so when the same text was typed twice and handed over
    once, the earlier entry is the delivered one and the later one is still unaccounted for.
    """
    return [entry for text, parked in parked_by_text.items()
            for entry in parked[:delivered.get(text, 0)]]


def notification_prompt(row):
    """The task-notification body carried by a ``queued_command`` attachment, else ``None``.

    ``sources.claude_text.normalize_record`` promotes the human ``commandMode="prompt"``
    form to a real user message and leaves every other mode alone, so what reaches here
    is the machine form only.
    """
    if not isinstance(row, dict) or row.get('isMeta'):
        return None
    if row.get('type') != 'attachment':
        return None
    attachment = row.get('attachment')
    if not isinstance(attachment, dict) or attachment.get('type') != 'queued_command':
        return None
    if attachment.get('commandMode') != 'task-notification':
        return None
    prompt = attachment.get('prompt')
    return prompt if isinstance(prompt, str) and prompt.strip() else None


def parse_task_notification(text):
    """One line describing a finished background task: ``[status] summary``.

    The body also carries a task id, a tool-use id and an output-file path. None of
    those help a person reading the conversation and the path names a temporary
    directory, so the summary line is all that is rendered; the raw record stays one
    ``[L#]`` jump away.
    """
    status = _inner(text, 'status') or '?'
    summary = _inner(text, 'summary') or '(no summary)'
    return '[%s] %s' % (status, summary)


def extract_inner_xml(text, tag):
    """The first body inside ``<tag>...</tag>``, or ``''`` when the pair is absent."""
    open_tag, close_tag = '<%s>' % tag, '</%s>' % tag
    start = text.find(open_tag)
    if start < 0:
        return ''
    end = text.find(close_tag, start + len(open_tag))
    return text[start + len(open_tag):end] if end >= 0 else ''


def parse_system_user_event(stripped):
    """A user-role pseudo message as a system event, or ``None`` when it is not one.

    ``stripped`` is lstrip output. ``None`` means the caller's ordinary handling applies.
    """
    if is_task_notification(stripped):
        return {'type': 'system_notification', 'text': parse_task_notification(stripped)}
    if stripped.startswith('<bash-stdout>') or stripped.startswith('<bash-stderr>'):
        out = extract_inner_xml(stripped, 'bash-stdout')
        err = extract_inner_xml(stripped, 'bash-stderr')
        parts = []
        if out and out != '(Bash completed with no output)':
            parts.append(out)
        if err:
            parts.append('[stderr] %s' % err)
        return {'type': 'bash_output', 'text': '\n'.join(parts) if parts else '(no output)'}
    if stripped.startswith('<teammate-message'):
        # Attributes may include teammate_id / summary; the body follows the closing >
        head_end = stripped.find('>')
        head = stripped[:head_end] if head_end > 0 else ''

        def _attr(name):
            key = '%s="' % name
            start = head.find(key)
            if start < 0:
                return ''
            start += len(key)
            end = head.find('"', start)
            return head[start:end] if end > start else ''

        teammate = _attr('teammate_id') or 'teammate'
        body_text = stripped[head_end + 1:].rstrip()
        close_tag = '</teammate-message>'
        if body_text.endswith(close_tag):
            body_text = body_text[:-len(close_tag)].rstrip()
        display = _attr('summary') or body_text or '(empty)'
        return {'type': 'teammate_message', 'text': '[%s] %s' % (teammate, display)}
    return None


def api_error_label(row):
    """A short, display-safe label for an ``api_error`` record, else ``None``.

    Only ``error.formatted`` is used. ``error.message`` carries the raw upstream body -
    for an HTTP 529 that is a JSON error payload - which belongs in the source file, not
    on a conversation line.
    """
    if not isinstance(row, dict) or row.get('type') != 'system':
        return None
    if row.get('subtype') != 'api_error':
        return None
    error = row.get('error')
    if not isinstance(error, dict):
        return 'API error'
    formatted = error.get('formatted')
    if isinstance(formatted, str) and formatted.strip():
        return formatted.strip()
    connection = error.get('connection')
    if isinstance(connection, dict) and isinstance(connection.get('code'), str):
        return 'Connection error (%s)' % connection['code']
    return 'API error'


def _seconds_between(first, last):
    """Whole seconds between two ISO timestamps, or ``None`` when either is unusable."""
    if not isinstance(first, str) or not isinstance(last, str):
        return None
    try:
        from datetime import datetime
        start = datetime.fromisoformat(first.replace('Z', '+00:00'))
        end = datetime.fromisoformat(last.replace('Z', '+00:00'))
    except ValueError:
        return None
    seconds = (end - start).total_seconds()
    return int(seconds) if seconds >= 0 else None


def format_span(seconds):
    """A compact duration for a retry run, or ``''`` when it is not worth saying."""
    if not seconds:
        return ''
    if seconds < 60:
        return '%ds' % seconds
    if seconds < 3600:
        return '%dm%02ds' % divmod(seconds, 60)
    return '%dh%02dm' % (seconds // 3600, (seconds % 3600) // 60)


class ApiErrorRun:
    """Fold a run of consecutive identical ``api_error`` records into one event.

    An outage writes one record per retry attempt. Ten rows saying the same thing is
    noise; one row saying it happened ten times over four minutes is the fact. A run
    ends when any other content is rendered between two errors, or when the error label
    changes - so a different failure never hides inside another one's count.

    The caller reserves a slot at the run's first record and calls :meth:`summary` to
    fill it, because a run's total is only known once the run is over.
    """

    def __init__(self):
        self.label = None
        self.count = 0
        self.first_ts = None
        self.last_ts = None
        self.slot = None
        self.prefix = ''

    def open(self, label, ts, slot, prefix=''):
        """Reserve the run's place. ``slot`` is whatever the caller needs to find it again
        (a turn dict, a list index); ``prefix`` is text to keep in front of the summary."""
        self.label, self.count, self.first_ts, self.last_ts = label, 1, ts, ts
        self.slot, self.prefix = slot, prefix

    def extend(self, ts):
        self.count += 1
        if ts:
            self.last_ts = ts

    def matches(self, label):
        return self.count > 0 and self.label == label

    def clear(self):
        self.label, self.count, self.first_ts, self.last_ts = None, 0, None, None
        self.slot, self.prefix = None, ''

    def summary(self):
        """The finished run's one-line text, or ``''`` when nothing is open."""
        if not self.count:
            return ''
        if self.count == 1:
            return self.label
        span = format_span(_seconds_between(self.first_ts, self.last_ts))
        retried = '%s — retried %d times' % (self.label, self.count)
        return retried + (' over %s' % span if span else '')
