#!/usr/bin/env python3
"""Do the reader, the card, the anchored transcript and search count the same human turns?

Read-only audit over a Claude library (default: ~/.claude/projects). For every record it
asks four consumers whether that record is a human turn:

    R  the reader         server.extract_conversation      -> a turn of type `user`
    A  the anchored text  anchored_transcript.render_claude -> a [U#] banner
    C  the card count     server._selection_user_turn
    S  the message stream session_logbook_cli._message_from_row -> role `user` with words,
                          which is what search matches and attributes to the person

and reports every record where the four do not agree, grouped by the record's shape. A
turn that is only an image has no words for S to carry; that is expected, it is counted
apart, and it is not a disagreement. Records the client marks as a compaction summary are
counted too, with how many of them any consumer still calls a human turn.
Agreement is the contract in CLAUDE.md section 2; this is how it is checked against real
data, which unit fixtures cannot stand in for.

Safe to run beside a resident dashboard: the state file, the scan cache, the history index
and the runtime-events journal are all rebound to a temporary directory before anything is
imported, and the library itself is only ever opened for reading.

The output is counts and structural shapes only - record type flags, block kinds, and a
label from a closed list of client-written markers. It never prints message text, a path
or a session id, so it can be pasted into an issue. Anything more specific belongs in the
git-ignored `_private/` directory.

    python3 scripts/audit_claude_human_turns.py [--root DIR] [--workers N] [--files-from LIST]

A library that is being written to is a moving target. To compare two builds, freeze the
file list once (one path per line, kept outside the repository) and pass it to both runs
with --files-from.
"""
import argparse
import collections
import os
import re
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix='logbook-audit-'))
os.environ['SESSION_LOGBOOK_HISTORY_INDEX'] = 'off'
os.environ['SESSION_LOGBOOK_EVENTS'] = str(_TMP / 'runtime-events.sqlite3')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402  (the environment above has to be set first)

server.STATE_FILE = _TMP / 'state.json'
server.SCAN_CACHE_FILE = _TMP / 'scan-cache.json'
server.SCAN_CACHE_BACKUP_DIR = _TMP / 'cache-backups'

import session_logbook_cli as cli  # noqa: E402
from sources import anchored_transcript, claude_history  # noqa: E402
from sources.claude_text import normalize_record, strip_leading_reminders  # noqa: E402

BANNER = re.compile(r'^━+ \[U(\d+)\] \[L(\d+)\] USER', re.MULTILINE)
_REAL_RECORDS = claude_history.records

# Client-written openings, so a shape can be named without printing what follows it.
_MARKERS = (
    ('[Request interrupted by user', 'interrupt marker'),
    ('[Image: source:', 'image source note'),
    ('[Image: original', 'image size note'),
    ('Base directory for this skill:', 'skill body'),
    ('Continue from where you left off.', 'resume prompt'),
    ('Stop hook feedback:', 'hook feedback'),
    ('<system-reminder>', 'reminder-wrapped'),
    ('<channel ', 'channel message'),
    ('<command-', 'slash command'),
    ('<local-command-', 'local command output'),
    ('<task-notification>', 'task notification'),
    ('This session is being continued', 'compaction summary'),
)


def _stamped(path, *args, **kwargs):
    """The reader's turns carry a timestamp and no line number; lend them one."""
    for line, record in _REAL_RECORDS(path, *args, **kwargs):
        yield line, dict(record, timestamp='L%d' % line)


def shape(record):
    record = normalize_record(record)
    message = record.get('message')
    content = message.get('content') if isinstance(message, dict) else None
    flags = ''.join(flag for flag, key in (('meta ', 'isMeta'), ('compact ', 'isCompactSummary'),
                                           ('subagent ', 'isSidechain')) if record.get(key))
    if isinstance(content, str):
        first, kinds = content, 'string'
    elif isinstance(content, list):
        blocks = [b for b in content if isinstance(b, dict)]
        first = next((b.get('text') or '' for b in blocks if b.get('type') == 'text'), '')
        kinds = 'blocks[%s]' % ','.join(sorted({str(b.get('type')) for b in blocks}))
    else:
        return '%s%s record, no message content' % (flags, record.get('type'))
    label = next((name for opening, name in _MARKERS if first.lstrip().startswith(opening)),
                 'other text' if first.strip() else 'no text')
    return '%s%s: %s' % (flags, kinds, label)


def _has_typed_words(record):
    """Whether the record holds any text of its own, read here without the rule under audit."""
    message = normalize_record(record).get('message')
    content = message.get('content') if isinstance(message, dict) else None
    if isinstance(content, str):
        return bool(strip_leading_reminders(content).strip())
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get('type') == 'text'
        and strip_leading_reminders(block.get('text') or '').strip() for block in content)


def _could_carry_a_banner(record):
    if record.get('type') != 'user' or record.get('isMeta'):
        return False
    message = record.get('message')
    content = message.get('content') if isinstance(message, dict) else None
    if isinstance(content, str):
        return True
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get('type') in ('text', 'image') for block in content)


def audit(path):
    path = Path(path)
    claude_history.records = _stamped
    try:
        turns = server.extract_conversation(path)['turns']
    finally:
        claude_history.records = _REAL_RECORDS
    reader = {int(t['ts'][1:]) for t in turns
              if t['type'] == 'user' and str(t.get('ts', '')).startswith('L')}
    rows = dict(_REAL_RECORDS(path))
    banners = BANNER.findall(anchored_transcript.render_claude(path))
    # render_claude only ever writes a banner for a non-meta user record that has words or
    # an image in it. A banner-shaped line that points anywhere else is text somebody
    # quoted - a pasted transcript, a tool result that printed one - and is counted apart
    # rather than as a disagreement.
    anchored, quoted = set(), 0
    for _, line in banners:
        if _could_carry_a_banner(rows.get(int(line), {})):
            anchored.add(int(line))
        else:
            quoted += 1
    card = {line for line, record in rows.items() if server._selection_user_turn(record)}
    stream = set()
    for line, record in rows.items():
        role, words = cli._message_from_row(record, 'claude')
        if role == 'user' and words:
            stream.add(line)
    turns = reader | anchored | card | stream
    wordless = {line for line in turns - stream if not _has_typed_words(rows.get(line, {}))}
    shapes = collections.Counter()
    for line in turns:
        key = ''.join(letter if line in group else '-'
                      for letter, group in (('R', reader), ('A', anchored), ('C', card),
                                            ('S', stream | wordless)))
        if key != 'RACS':
            shapes[(key, shape(rows.get(line, {})))] += 1
    summaries = {line for line, record in rows.items() if record.get('isCompactSummary')}
    return {'shapes': shapes, 'quoted': quoted, 'turns': len(turns), 'wordless': len(wordless),
            'summaries': len(summaries), 'summary_turns': len(summaries & turns),
            'disagrees': bool(shapes)}


def _work(path):
    try:
        return audit(path)
    except Exception as error:  # a file being written right now, a truncated record
        return {'error': type(error).__name__}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--root', default=str(Path.home() / '.claude/projects'))
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--files-from', help='audit exactly the paths listed in this file')
    args = parser.parse_args()
    if args.files_from:
        files = [line for line in Path(args.files_from).read_text().splitlines() if line.strip()]
    else:
        files = sorted(str(p) for p in Path(args.root).expanduser().glob('*/*.jsonl'))
    shapes, sessions = collections.Counter(), collections.Counter()
    errors, disagreeing, quoted, turns = collections.Counter(), 0, 0, 0
    wordless, summaries, summary_turns = 0, 0, 0
    from multiprocessing import Pool
    with Pool(args.workers) as pool:
        for result in pool.imap_unordered(_work, files, chunksize=8):
            if 'error' in result:
                errors[result['error']] += 1
                continue
            shapes.update(result['shapes'])
            sessions.update(result['shapes'].keys())
            disagreeing += result['disagrees']
            quoted += result['quoted']
            turns += result['turns']
            wordless += result['wordless']
            summaries += result['summaries']
            summary_turns += result['summary_turns']
    print('sessions read: %d   unreadable: %s' % (len(files) - sum(errors.values()), dict(errors) or 0))
    print('records any consumer calls a human turn: %d' % turns)
    print('sessions where the four consumers disagree: %d' % disagreeing)
    print('human turns with no typed words (image only), so nothing for S to carry: %d' % wordless)
    print('compaction summaries: %d   of which a consumer calls a human turn: %d'
          % (summaries, summary_turns))
    print('banner-shaped lines that are quoted text, not banners: %d' % quoted)
    if shapes:
        print('\nR = reader user turn, A = anchored [U#], C = card count, S = message stream / search'
              '\nrecords  sessions  who    shape')
        for (key, name), count in sorted(shapes.items(), key=lambda item: -item[1]):
            print('%7d  %8d  %s   %s' % (count, sessions[(key, name)], key, name))
    leaked = sorted(p.name for p in _TMP.iterdir())
    print('\nwritable paths were rebound to a temporary directory; it now holds: %s' % (leaked or 'nothing'))


if __name__ == '__main__':
    main()
