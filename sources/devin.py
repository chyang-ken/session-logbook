"""Read-only Devin Local database adapter (not legacy Windsurf Cascade).

Source references are virtual paths: <sessions.db>/<percent-encoded session ID>.
They identify database records, never generated transcript files. N anchors refer
to message_nodes.row_id; follow returns a full snapshot because branches can change.
"""
import json
import hashlib
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote

DEVIN_ROOT = Path(os.environ.get('DEVIN_DATA_DIR') or
                  str(Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share') / 'devin'))


def database_path():
    return DEVIN_ROOT / 'cli' / 'sessions.db'


def fingerprint():
    stamps = []
    for path in (database_path(), Path(str(database_path()) + '-wal')):
        try:
            st = path.stat()
            stamps.append([st.st_mtime_ns, st.st_size])
        except FileNotFoundError:
            stamps.append(None)
    return stamps


def is_devin_path(path):
    return Path(path).parent.name == 'sessions.db'


def reference(session_id):
    return database_path() / quote(session_id, safe='')


def _connect(path):
    # mode=ro includes committed WAL records; immutable=1 would miss active writes.
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    return conn


def scan_sessions():
    if not database_path().is_file():
        return []
    with closing(_connect(database_path())) as conn:
        return [reference(row[0]) for row in conn.execute(
            'SELECT id FROM sessions WHERE COALESCE(hidden,0) = 0')]


def find_session(session_id):
    raw_id = session_id.removeprefix('devin:')
    return next((p for p in scan_sessions() if unquote(p.name) == raw_id), None)


def read_session(path):
    """Read one coherent snapshot and follow only the selected parent chain.

    A broken chain is an error, not permission to merge alternate histories.
    """
    path = Path(path)
    if not is_devin_path(path):
        raise ValueError('not a Devin session reference')
    with closing(_connect(path.parent)) as conn:
        conn.execute('BEGIN')
        row = conn.execute('SELECT * FROM sessions WHERE id=? AND COALESCE(hidden,0)=0',
                           (unquote(path.name),)).fetchone()
        if row is None:
            raise ValueError('Devin session not found or hidden')
        meta = dict(row)
        records = [dict(r) for r in conn.execute(
            'SELECT * FROM message_nodes WHERE session_id=? ORDER BY row_id', (meta['id'],))]
    nodes = {r['node_id']: r for r in records}
    leaf = meta.get('main_chain_id')
    if leaf is None:
        if records:
            raise ValueError('Devin session has messages but no selected main chain')
        chain = []
    else:
        chain, visited = [], set()
        while leaf is not None:
            if leaf in visited or leaf not in nodes:
                raise ValueError('Devin main chain is cyclic or incomplete')
            visited.add(leaf)
            node = nodes[leaf]
            chain.append(node)
            leaf = node['parent_node_id']
        chain.reverse()
    for node in chain:
        msg = json.loads(node['chat_message'])
        if not isinstance(msg, dict):
            raise ValueError('invalid Devin message object')
        node['message'] = msg
    return meta, chain


def text_content(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return '\n'.join(text_content(v) for v in value)
    if isinstance(value, dict):
        if isinstance(value.get('text'), str):
            return value['text']
        return '[non-text content]'
    return ''


def message_text(node):
    msg = node['message']
    role = msg.get('role')
    if role not in ('user', 'assistant'):
        return None, ''
    if role == 'user' and (msg.get('metadata') or {}).get('is_user_input') is False:
        return None, ''
    return role, text_content(msg.get('content'))


def _iso(value):
    try:
        return datetime.fromtimestamp(float(value or 0), timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return ''


def _metadata(path, meta, chain):
    messages = [(i, *message_text(n)) for i, n in enumerate(chain)]
    users = [m for m in messages if m[1] == 'user' and m[2]]
    assistants = [m for m in messages if m[1] == 'assistant' and m[2]]
    recent = sorted(users[-3:] + assistants[-3:])
    mtime = meta.get('last_activity_at') or meta.get('created_at') or 0
    return {'id': 'devin:' + meta['id'], 'source': 'devin',
            'project_path': meta.get('working_directory') or '',
            'jsonl_path': str(path), 'source_kind': 'sqlite',
            'mtime': mtime, 'mtime_iso': _iso(mtime),
            'size': sum(len(n['chat_message'].encode()) for n in chain),
            'model': meta.get('model'), 'custom_title': meta.get('title') or '',
            'user_turn_count': len(users), 'last_stop_reason': None,
            'first_user_msg': users[0][2][:500] if users else '',
            'recent_msgs': [{'role': role, 'text': text[:500]} for _, role, text in recent]}


def extract_metadata(path):
    return _metadata(path, *read_session(path))


def _turns(chain):
    results = {n['message'].get('tool_call_id'): n for n in chain
               if n['message'].get('role') == 'tool'}
    turns = []
    paired = set()
    for n in chain:
        msg = n['message']
        role, text = message_text(n)
        base = {'ts': (msg.get('metadata') or {}).get('created_at') or _iso(n['created_at']),
                'node_id': n['row_id']}
        if role and text:
            turns.append(dict(base, type=role, text=text))
        elif msg.get('role') == 'system':
            turns.append(dict(base, type='system', text='[System context; expand database evidence]'))
        for call in msg.get('tool_calls') or []:
            if not isinstance(call, dict):
                continue
            function = call.get('function') or call
            result_node = results.get(call.get('id'))
            result_msg = result_node['message'] if result_node else {}
            result = text_content(result_msg.get('content'))
            ext = (result_msg.get('metadata') or {}).get('extensions') or {}
            success = (ext.get('chisel/tool_result_meta') or {}).get('success')
            paired.add(call.get('id'))
            turns.append(dict(base, type='tool', name=function.get('name') or 'tool',
                              summary=json.dumps(function.get('arguments') or {}, ensure_ascii=False)[:400],
                              result=result[:1500], is_error=success is False,
                              result_node_id=result_node['row_id'] if result_node else None,
                              result_size=len(result), result_present=result_node is not None))
        if msg.get('role') == 'tool' and msg.get('tool_call_id') not in paired:
            turns.append(dict(base, type='tool', name='tool result', summary='',
                              result=text_content(msg.get('content'))[:1500]))
    return turns


def extract_conversation(path):
    meta, chain = read_session(path)
    info = _metadata(path, meta, chain)
    identity = [meta.get('title'), meta.get('working_directory'), meta.get('model'),
                [(n['row_id'], n['chat_message']) for n in chain]]
    fingerprint = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
    return {**info, 'total_lines': len(chain), 'record_unit': 'database nodes',
            'fingerprint': fingerprint, 'turns': _turns(chain)}


def digest_header(path):
    return '\n'.join([
        '# COMPACT SESSION DIGEST: Devin Local',
        f'# SOURCE_DATABASE: {Path(path).parent}',
        f'# SESSION_REFERENCE: {path}',
        '# [N<n>] is message_nodes.row_id, not a file line or array offset.',
        '# User/Assistant text is complete; system context and tool results are reduced.',
        '# Expand with: session_logbook_cli.py evidence <session-reference> --line <n>',
        '# Evidence is restricted to this session. No database writes are performed.',
        '# Follow returns the full selected chain: edits and compaction can replace earlier nodes.',
    ])


def render_context(path, after_line=0):
    meta, chain = read_session(path)
    turns = _turns(chain)
    out = [digest_header(path), '# SESSION_ID: devin:' + meta['id'],
           f'# INPUT_CURSOR: N{after_line}',
           f'# NEXT_CURSOR: N{chain[-1]["row_id"] if chain else 0}',
           '# FOLLOW_MODE: full-snapshot', '# EXPLICIT_TERMINAL: unknown',
           '# A quiet database does not prove completion or liveness.', '']
    u = 0
    for t in turns:
        anchor = f'[N{t["node_id"]}]'
        if t['type'] == 'user':
            u += 1
            out.append(f'## [U{u}] {anchor} USER\n{t["text"]}')
        elif t['type'] == 'assistant':
            out.append(f'## {anchor} ASSISTANT\n{t["text"]}')
        elif t['type'] == 'tool':
            status = 'error' if t.get('is_error') else ('recorded' if t.get('result_present') else 'pending')
            detail = t.get('result', '')[:300] if t.get('is_error') else ''
            result_anchor = f'[N{t["result_node_id"]}]' if t.get('result_node_id') else ''
            out.append(f'{anchor} TOOL {t["name"]} {t["summary"]}\n{result_anchor} result: {status}, {t.get("result_size", 0)} chars {detail}')
        else:
            out.append(f'{anchor} {t.get("text", "")}')
    return '\n\n'.join(out)


def extract_transcript(path):
    return render_context(path)


def read_evidence(path, line, context=1, max_chars=12000):
    # Read the exact row even if an edit has removed it from the selected chain.
    with closing(_connect(Path(path).parent)) as conn:
        rows = list(conn.execute('SELECT * FROM message_nodes WHERE session_id=? ORDER BY row_id',
                                (unquote(Path(path).name),)))
    index = next((i for i, r in enumerate(rows) if r['row_id'] == line), None)
    if index is None:
        raise ValueError(f'Devin node N{line} does not exist in this session')
    out = []
    for row in rows[max(0, index-context):index+context+1]:
        raw = json.dumps(dict(row), ensure_ascii=False)
        if max_chars and len(raw) > max_chars:
            raw = raw[:max_chars] + ' [truncated]'
        out.append(f'[N{row["row_id"]}] {raw}')
    return '\n'.join(out)


def search(path, terms, role='any'):
    meta, chain = read_session(path)
    found, snippets = set(), []
    for node in chain:
        message_role, text = message_text(node)
        if not message_role or (role != 'any' and role != message_role):
            continue
        hits = [t for t in terms if t in text.lower()]
        found.update(hits)
        if hits and len(snippets) < 3:
            start = max(0, min(text.lower().find(t) for t in hits) - 60)
            snippets.append({'role': message_role, 'line': node['row_id'], 'anchor_kind': 'N',
                             'text': text[start:start+320], 'term': hits[0]})
    return found, snippets
