"""Kimi Code CLI data source ($KIMI_CODE_HOME, default ~/.kimi-code).

The interface mirrors the Codex / Antigravity adapters:
- scan_sessions(): yields the main agent's wire.jsonl per session, skipping sub-agents
- extract_metadata(jsonl_path): returns a meta dict aligned with Claude/Codex
- extract_conversation(jsonl_path): returns a list of turns
- extract_transcript(jsonl_path): token-optimized markdown export

Storage layout (official docs: data-locations + sessions guides):
  <home>/session_index.jsonl                       one {sessionId, sessionDir, workDir} per line
  <home>/sessions/<workDirKey>/<sessionId>/
      state.json                                   {createdAt, updatedAt, title, isCustomTitle, workDir, ...}
      agents/main/wire.jsonl                       main agent event stream  <- the session's jsonl_path
      agents/<other>/wire.jsonl                    sub-agents: skipped in scan, like Codex subagents
      agents/main/{tasks,blobs,plans}/, logs/      ignored

wire.jsonl is an append-only event log; every line is {"type", "time": <epoch ms>, ...}.
Rows this adapter reads (protocol_version 1.4 and 1.5 both observed):
  metadata                    first line, carries protocol_version
  config.update / profile.bind   modelAlias; cwd (v1.4 config.update / v1.5 profile.bind.environmentDisclosure)
  llm.request                 modelAlias / model actually used for a step
  turn.prompt                 {input:[{type:text,text}], origin:{kind}}; only kind=="user" is a human turn
  context.append_message      {message:{role, content, origin:{kind}}}; the user text as appended to context
  context.append_loop_event   {event:{type: step.begin|content.part|tool.call|tool.result|step.end}}
                              content.part.part.type=="think" is reasoning (dropped like Claude thinking);
                              =="text" is the assistant's visible reply
  interaction.request/.resolved  AskUserQuestion prompt + the user's answers -> a `qa` turn
  turn.ended                  {reason}; the last one is last_stop_reason
Everything else (usage.record, llm.tools_snapshot, permission.*, task.*, tools.*) is skipped.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional


def _home_from_env(env=None) -> Path:
    """Honour $KIMI_CODE_HOME like the CLI itself does; fall back to the documented default."""
    env = os.environ if env is None else env
    return Path(env.get("KIMI_CODE_HOME") or (Path.home() / ".kimi-code")).expanduser()


KIMI_HOME = _home_from_env()
KIMI_SESSIONS_ROOT = KIMI_HOME / "sessions"
SESSION_INDEX_PATH = KIMI_HOME / "session_index.jsonl"

MAIN_AGENT = "main"

# Aligned with the top-of-file constants in server.py
RECENT_USER_N = 3
RECENT_ASSISTANT_N = 3
CONV_USER_MAX = 5000
CONV_ASSISTANT_MAX = 10000
CONV_TOOL_RESULT_MAX = 1500
CONV_TOOL_INPUT_MAX = 300
TRANSCRIPT_TOOL_RESULT_MAX = 200

_INDEX_CACHE = {"mtime": 0.0, "data": {}}


# ---------- Path helpers ----------
def _under_root(path, root) -> bool:
    """Directory-boundary-safe prefix check (same idea as codex._under_root)."""
    s = str(path)
    rs = str(root)
    return s == rs or s.startswith(rs + os.sep)


def is_kimi_path(path) -> bool:
    """Whether path belongs to the Kimi data source (anything under <home>/sessions)."""
    return _under_root(path, KIMI_SESSIONS_ROOT)


def _session_dir(jsonl_path: Path) -> Path:
    """<session>/agents/<agent>/wire.jsonl -> <session>."""
    return Path(jsonl_path).parents[2]


def agent_id_for_path(jsonl_path: Path) -> str:
    """<session>/agents/<agent>/wire.jsonl -> <agent> ("main" for the primary stream)."""
    return Path(jsonl_path).parent.name


def is_subagent_path(jsonl_path: Path) -> bool:
    """Sub-agent streams live at agents/<id>/wire.jsonl with id != "main"."""
    return agent_id_for_path(jsonl_path) != MAIN_AGENT


def session_id_for_path(jsonl_path: Path) -> str:
    """Session id is the session directory name (session_<uuid>)."""
    try:
        return _session_dir(jsonl_path).name
    except IndexError:
        return Path(jsonl_path).stem


def _read_state(session_dir: Path) -> dict:
    try:
        with open(session_dir / "state.json", "r", encoding="utf-8", errors="replace") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _load_session_index() -> dict:
    """session_index.jsonl -> {sessionId: {sessionDir, workDir}}. Cached by mtime."""
    try:
        st = SESSION_INDEX_PATH.stat()
    except OSError:
        return _INDEX_CACHE["data"]
    if st.st_mtime == _INDEX_CACHE["mtime"]:
        return _INDEX_CACHE["data"]
    data = {}
    try:
        with open(SESSION_INDEX_PATH, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                sid = d.get("sessionId")
                if sid:
                    data[sid] = {"sessionDir": d.get("sessionDir"), "workDir": d.get("workDir")}
    except OSError:
        return _INDEX_CACHE["data"]
    _INDEX_CACHE["mtime"] = st.st_mtime
    _INDEX_CACHE["data"] = data
    return data


def _normalize_project_path(cwd) -> str:
    """Same rule as codex._normalize_project_path: strip a trailing slash, empty -> '~'."""
    if not isinstance(cwd, str) or not cwd:
        return "~"
    cwd = cwd.rstrip("/")
    return cwd or "~"


# ---------- Row helpers ----------
def _read_lines(jsonl_path: Path):
    """Full read -> (parsed rows, physical line count). Observed wire.jsonl files are small
    (<2 MB); the only long lines are llm.tools_snapshot rows, which are parsed once and ignored."""
    out = []
    total = 0
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                total += 1
                if not line.strip():
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return out, total


def _ts(d) -> str:
    """Epoch-ms `time` -> ISO-8601 UTC string (empty when missing/invalid)."""
    t = d.get("time") if isinstance(d, dict) else None
    if not isinstance(t, (int, float)):
        return ""
    try:
        return datetime.fromtimestamp(t / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return ""


def _text_from_content(content) -> str:
    """Message content is a string, or a list of {type:"text", text} parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                t = p.get("text")
                if isinstance(t, str) and t:
                    parts.append(t)
            elif isinstance(p, dict) and p.get("type") in ("image", "input_image"):
                parts.append("[image]")
        return "\n".join(parts)
    return ""


def _origin_kind(obj) -> str:
    o = (obj or {}).get("origin") if isinstance(obj, dict) else None
    return (o or {}).get("kind") or ""


def _is_user_prompt(d: dict) -> bool:
    return d.get("type") == "turn.prompt" and _origin_kind(d) == "user"


def _user_message_text(d: dict) -> str:
    """context.append_message with role user and a human origin -> text, else ''."""
    m = d.get("message") or {}
    if not isinstance(m, dict) or m.get("role") != "user" or _origin_kind(m) != "user":
        return ""
    return _text_from_content(m.get("content")).strip()


def _loop_event(d: dict) -> dict:
    ev = d.get("event") if d.get("type") == "context.append_loop_event" else None
    return ev if isinstance(ev, dict) else {}


def _assistant_part_text(d: dict) -> str:
    """content.part with part.type == text -> the visible assistant text, else ''."""
    ev = _loop_event(d)
    if ev.get("type") != "content.part":
        return ""
    part = ev.get("part") or {}
    if part.get("type") != "text":
        return ""
    t = part.get("text")
    return t if isinstance(t, str) else ""


def _result_text(result) -> str:
    """tool.result.result is {output: str | [{type,text}], isError?, note?} (also accept a bare string)."""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        output = result.get("output")
        out = output if isinstance(output, str) else _text_from_content(output)
        if not out and result.get("error"):
            out = str(result["error"])
        return out or ""
    if isinstance(result, list):
        return _text_from_content(result)
    return str(result)


def _result_is_error(result) -> bool:
    return isinstance(result, dict) and result.get("isError") is True


def _compact_args(args) -> str:
    if isinstance(args, dict):
        return ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in args.items())
    if args is None:
        return ""
    return json.dumps(args, ensure_ascii=False) if not isinstance(args, str) else args


def _tool_summary(ev: dict) -> str:
    """One-line human summary of a tool.call: prefer the display block, then well-known args, then all args."""
    name = ev.get("name") or "?"
    args = ev.get("args") if isinstance(ev.get("args"), dict) else {}
    display = ev.get("display") if isinstance(ev.get("display"), dict) else {}
    if display.get("command"):
        return str(display["command"])
    if name == "Bash" and args.get("command"):
        return str(args["command"])
    if name == "Grep" and args.get("pattern"):
        return f"{args['pattern']} in {args.get('path') or display.get('path') or '.'}"
    if display.get("path"):
        return str(display["path"])
    for k in ("pattern", "query", "path", "file_path", "prompt", "description"):
        if args.get(k):
            return str(args[k])
    if ev.get("description"):
        return str(ev["description"])
    return _compact_args(args) or name


def _truncate(text: str, n: int) -> str:
    if not text:
        return ""
    if len(text) <= n:
        return text
    return text[:n] + f"\n…(+{len(text)-n} chars)"


# ---------- QA (AskUserQuestion) ----------
def _build_qa_unit(questions_in, answers_map) -> Optional[dict]:
    """Same shape as server._build_qa_unit so index.html renderQaQuestion renders it unchanged:
    {questions: [{question, header, multiSelect, options:[{label, description, preview}], answer, matched_option}]}.
    """
    if not isinstance(questions_in, list) or not questions_in:
        return None
    if not isinstance(answers_map, dict):
        answers_map = {}
    out_q = []
    for q in questions_in:
        if not isinstance(q, dict):
            continue
        qtext = q.get("question", "") or ""
        options = []
        for opt in q.get("options") or []:
            if not isinstance(opt, dict):
                continue
            options.append({
                "label": opt.get("label", "") or "",
                "description": opt.get("description", "") or "",
                "preview": opt.get("preview") or None,
            })
        ans = answers_map.get(qtext, "")
        if isinstance(ans, list):
            ans = ", ".join(str(a) for a in ans)
        ans = ans or ""
        matched = None
        for i, opt in enumerate(options):
            if opt["label"] and opt["label"] == ans:
                matched = i
                break
        out_q.append({
            "question": qtext,
            "header": q.get("header", "") or "",
            "multiSelect": bool(q.get("multiSelect")),
            "options": options,
            "answer": ans,
            "matched_option": matched,
        })
    return {"questions": out_q} if out_q else None


def _format_qa_transcript(qa_unit) -> str:
    """Mirror of server._format_qa_transcript for the markdown export."""
    lines = []
    for q in qa_unit["questions"]:
        lines.append(f"Q: {q['question']}")
        is_other = q["matched_option"] is None and q["answer"]
        for i, opt in enumerate(q["options"]):
            letter = chr(ord("A") + i)
            lines.append(f"  [{letter}] {opt['label']}")
            if is_other and opt["description"]:
                lines.append(f"      {opt['description']}")
        if q["answer"]:
            if q["matched_option"] is not None:
                letter = chr(ord("A") + q["matched_option"])
                lines.append(f"→ {letter} (selected): {q['answer']}")
            else:
                lines.append(f"→ Other: {q['answer']}")
        else:
            lines.append("→ (no answer)")
        lines.append("")
    return "\n".join(lines).rstrip()


# ---------- Session-level facts ----------
def _session_facts(jsonl_path: Path, lines) -> dict:
    """cwd / model / title resolved with a fixed priority; shared by metadata + conversation + transcript."""
    sid = session_id_for_path(jsonl_path)
    state = _read_state(_session_dir(jsonl_path))

    wire_cwd = None
    model = None
    for d in lines:
        t = d.get("type")
        if t == "profile.bind":
            env = d.get("environmentDisclosure") or {}
            if isinstance(env, dict) and env.get("cwd") and not wire_cwd:
                wire_cwd = env["cwd"]
            if d.get("modelAlias"):
                model = d["modelAlias"]
        elif t == "config.update":
            if d.get("cwd") and not wire_cwd:
                wire_cwd = d["cwd"]
            if d.get("modelAlias"):
                model = d["modelAlias"]
        elif t == "llm.request":
            if d.get("modelAlias") or d.get("model"):
                model = d.get("modelAlias") or d.get("model")

    # Priority: state.json workDir (an older state.json shape stores it as `cwd`) > cwd recorded in the
    # wire (v1.5 profile.bind / v1.4 config.update) > the roster's workDir.
    cwd = state.get("workDir") or state.get("cwd") or wire_cwd
    if not cwd:
        cwd = (_load_session_index().get(sid) or {}).get("workDir")

    # Kimi auto-generates a title for every session and shows it in /sessions, so it is kept even
    # when isCustomTitle is false: it is the name the user sees in Kimi, not a placeholder.
    title = state.get("title")
    return {
        "id": sid,
        "project_path": _normalize_project_path(cwd),
        "model": model,
        "custom_title": title.strip() if isinstance(title, str) else "",
    }


# ---------- Turn collection (single walk shared by conversation + transcript) ----------
def _collect_turns(lines) -> list:
    """wire.jsonl rows -> untruncated turn dicts in file order.

    Turn shapes (aligned with Claude/Codex):
      {"type": "user", "text", "ts"}
      {"type": "assistant", "text", "ts"}       one per model step, text parts concatenated
      {"type": "tool", "name", "summary", "result", "is_error", "ts"}   paired by toolCallId
      {"type": "qa", "questions", "ts"}          AskUserQuestion + interaction.resolved answers
    """
    turns = []
    pending_tools = {}       # toolCallId -> tool turn dict (result filled in when tool.result arrives)
    ask_pending = {}         # toolCallId -> questions list (AskUserQuestion awaiting its answers)
    interaction_to_call = {}  # interaction id -> toolCallId
    answers_by_call = {}     # toolCallId -> {question: answer}
    step_text = []           # visible assistant text parts of the current step
    step_ts = ""

    def flush_assistant():
        nonlocal step_text
        text = "".join(step_text).strip()
        if text:
            turns.append({"type": "assistant", "text": text, "ts": step_ts})
        step_text = []

    def emit_qa(tcid, ts):
        questions = ask_pending.pop(tcid, None)
        unit = _build_qa_unit(questions, answers_by_call.pop(tcid, {}))
        if unit:
            turns.append({"type": "qa", "questions": unit["questions"], "ts": ts})
        elif questions is not None:
            # Unparseable question block: keep it visible as a plain tool turn rather than dropping it
            turns.append({"type": "tool", "name": "AskUserQuestion", "summary": "",
                          "result": "", "is_error": False, "ts": ts})

    for d in lines:
        t = d.get("type")
        ts = _ts(d)
        if t == "context.append_message":
            text = _user_message_text(d)
            if text:
                flush_assistant()
                turns.append({"type": "user", "text": text, "ts": ts})
        elif t == "context.append_loop_event":
            ev = _loop_event(d)
            et = ev.get("type")
            if et == "step.begin":
                flush_assistant()
                step_ts = ts
            elif et == "content.part":
                part_text = _assistant_part_text(d)
                if part_text:
                    if not step_text:
                        step_ts = ts
                    step_text.append(part_text)
            elif et == "tool.call":
                flush_assistant()
                tcid = ev.get("toolCallId")
                name = ev.get("name") or "?"
                if name == "AskUserQuestion":
                    args = ev.get("args") if isinstance(ev.get("args"), dict) else {}
                    ask_pending[tcid] = args.get("questions") or []
                    continue
                turn = {"type": "tool", "name": name, "summary": _tool_summary(ev),
                        "result": "", "is_error": False, "ts": ts}
                turns.append(turn)
                if tcid:
                    pending_tools[tcid] = turn
            elif et == "tool.result":
                tcid = ev.get("toolCallId")
                if tcid in ask_pending:
                    emit_qa(tcid, ts)
                    continue
                turn = pending_tools.pop(tcid, None)
                res = ev.get("result")
                if turn is not None:
                    turn["result"] = _result_text(res)
                    turn["is_error"] = _result_is_error(res)
                else:
                    turns.append({"type": "tool", "name": "?", "summary": "",
                                  "result": _result_text(res), "is_error": _result_is_error(res), "ts": ts})
            elif et == "step.end":
                flush_assistant()
        elif t == "interaction.request":
            tcid = d.get("toolCallId") or (d.get("request") or {}).get("toolCallId")
            if d.get("id") is not None:
                interaction_to_call[d["id"]] = tcid
            if tcid and tcid not in ask_pending:
                ask_pending[tcid] = ((d.get("request") or {}).get("questions")) or []
        elif t == "interaction.resolved":
            tcid = interaction_to_call.pop(d.get("id"), None)
            answers = (d.get("response") or {}).get("answers")
            if tcid and isinstance(answers, dict):
                answers_by_call[tcid] = answers
    flush_assistant()
    # AskUserQuestion blocks that never got a tool.result (session cut off mid-question) stay visible
    for tcid in list(ask_pending.keys()):
        emit_qa(tcid, "")
    return turns


# ---------- Public interface ----------
def search_text_from_line(d: dict) -> str:
    """Reused by server._search_session and the CLI: searchable user/assistant text from one row."""
    t = d.get("type")
    if t == "context.append_message":
        return _user_message_text(d)
    if t == "context.append_loop_event":
        return _assistant_part_text(d).strip()
    return ""


def message_role_from_line(d: dict):
    """(role, text) for the CLI search: user rows and assistant text parts only."""
    t = d.get("type")
    if t == "context.append_message":
        text = _user_message_text(d)
        return ("user", text) if text else (None, "")
    if t == "context.append_loop_event":
        text = _assistant_part_text(d).strip()
        return ("assistant", text) if text else (None, "")
    return None, ""


def scan_sessions(root: Path = None) -> Iterator[Path]:
    """Yield each session's main-agent wire.jsonl (sub-agent streams are skipped).

    Layout: <root>/<workDirKey>/<sessionId>/agents/main/wire.jsonl. `root` is for tests.
    """
    root = KIMI_SESSIONS_ROOT if root is None else root
    if not root.exists():
        return
    for wd_dir in root.iterdir():
        if not wd_dir.is_dir():
            continue
        for sess_dir in wd_dir.iterdir():
            if not sess_dir.is_dir():
                continue
            wire = sess_dir / "agents" / MAIN_AGENT / "wire.jsonl"
            if wire.is_file():
                yield wire


def find_wire_by_session_id(session_id: str, root: Path = None) -> Optional[Path]:
    """Locate a session's main wire.jsonl by id. Roster first (one stat), then a directory walk.

    Returns None when nothing exists; never fabricates a path.
    """
    if not session_id or "/" in session_id or session_id in (".", ".."):
        return None
    root = KIMI_SESSIONS_ROOT if root is None else root
    if root is KIMI_SESSIONS_ROOT:
        entry = _load_session_index().get(session_id) or {}
        sdir = entry.get("sessionDir")
        if sdir:
            candidate = Path(sdir) / "agents" / MAIN_AGENT / "wire.jsonl"
            if is_kimi_path(candidate) and candidate.is_file():
                return candidate
    if not root.exists():
        return None
    for wd_dir in root.iterdir():
        if not wd_dir.is_dir():
            continue
        candidate = wd_dir / session_id / "agents" / MAIN_AGENT / "wire.jsonl"
        if candidate.is_file():
            return candidate
    return None


def extract_metadata(jsonl_path: Path) -> Optional[dict]:
    """Kimi wire.jsonl -> meta dict (schema aligned with Claude/Codex/Antigravity)."""
    jsonl_path = Path(jsonl_path)
    try:
        stat = jsonl_path.stat()
    except FileNotFoundError:
        return None
    lines, _total = _read_lines(jsonl_path)
    facts = _session_facts(jsonl_path, lines)

    user_turns = 0
    last_stop = None
    users = []       # (ts, text)
    assistants = []  # (ts, text)
    step_text = []
    step_ts = ""

    def flush():
        nonlocal step_text
        text = "".join(step_text).strip()
        if text:
            assistants.append((step_ts, text))
        step_text = []

    for d in lines:
        t = d.get("type")
        if t == "turn.prompt":
            if _is_user_prompt(d):
                user_turns += 1
        elif t == "turn.ended":
            r = d.get("reason")
            if isinstance(r, str) and r:
                last_stop = r
        elif t == "context.append_message":
            text = _user_message_text(d)
            if text:
                users.append((_ts(d), text))
        elif t == "context.append_loop_event":
            ev = _loop_event(d)
            et = ev.get("type")
            if et in ("step.begin", "step.end", "tool.call"):
                flush()
                if et == "step.begin":
                    step_ts = _ts(d)
            elif et == "content.part":
                part_text = _assistant_part_text(d)
                if part_text:
                    if not step_text:
                        step_ts = _ts(d)
                    step_text.append(part_text)
    flush()

    raw = [(ts, "user", text) for ts, text in users[-RECENT_USER_N:]]
    raw += [(ts, "assistant", text) for ts, text in assistants[-RECENT_ASSISTANT_N:]]
    raw.sort(key=lambda m: m[0])
    recent_msgs = [{"role": role, "text": text[:500]} for (_, role, text) in raw if text]

    return {
        "id": facts["id"],
        "project_path": facts["project_path"],
        "jsonl_path": str(jsonl_path),
        "mtime": stat.st_mtime,
        "mtime_iso": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        "size": stat.st_size,
        "source": "kimi",
        "model": facts["model"],
        "cli_version": None,   # the wire does not record the CLI version
        "custom_title": facts["custom_title"],
        "user_turn_count": user_turns,
        "last_stop_reason": last_stop,
        "recent_msgs": recent_msgs,
        "first_user_msg": users[0][1] if users else "",
    }


def extract_conversation(jsonl_path: Path) -> Optional[dict]:
    """Kimi wire.jsonl -> conversation-view dict (turn types: user / assistant / tool / qa)."""
    jsonl_path = Path(jsonl_path)
    try:
        jsonl_path.stat()
    except FileNotFoundError:
        return None
    lines, total_lines = _read_lines(jsonl_path)
    facts = _session_facts(jsonl_path, lines)
    turns = []
    for turn in _collect_turns(lines):
        t = turn["type"]
        if t == "user":
            turns.append({**turn, "text": _truncate(turn["text"], CONV_USER_MAX)})
        elif t == "assistant":
            turns.append({**turn, "text": _truncate(turn["text"], CONV_ASSISTANT_MAX)})
        elif t == "tool":
            turns.append({**turn,
                          "summary": _truncate(turn["summary"], CONV_TOOL_INPUT_MAX),
                          "result": _truncate(turn["result"], CONV_TOOL_RESULT_MAX)})
        else:
            turns.append(turn)
    return {
        "id": facts["id"],
        "project_path": facts["project_path"],
        "custom_title": facts["custom_title"],
        "total_lines": total_lines,
        "source": "kimi",
        "turns": turns,
    }


def extract_transcript(jsonl_path: Path) -> str:
    """Token-optimized export with the same markdown shape as Claude/Codex/Antigravity.

    user / assistant are not truncated; tool results are truncated to 200 chars; QA becomes a ## QA block.
    """
    jsonl_path = Path(jsonl_path)
    try:
        jsonl_path.stat()
    except FileNotFoundError:
        return ""
    lines, total_lines = _read_lines(jsonl_path)
    facts = _session_facts(jsonl_path, lines)
    out_blocks = []
    for turn in _collect_turns(lines):
        t = turn["type"]
        if t == "user":
            out_blocks.append(f"## USER\n{turn['text']}\n")
        elif t == "assistant":
            out_blocks.append(f"## ASSISTANT\n{turn['text']}\n")
        elif t == "qa":
            out_blocks.append("## QA\n" + _format_qa_transcript({"questions": turn["questions"]}) + "\n")
        elif t == "tool":
            summary = (turn.get("summary") or "").strip()
            if len(summary) > CONV_TOOL_INPUT_MAX:
                summary = summary[:CONV_TOOL_INPUT_MAX] + "…"
            result = (turn.get("result") or "").strip()
            if len(result) > TRANSCRIPT_TOOL_RESULT_MAX:
                n = TRANSCRIPT_TOOL_RESULT_MAX
                nlines = result.count("\n") + 1
                result = result[:n].rstrip() + f" …[+{len(result)-n} chars, ~{nlines} lines]"
            head = f"[tool: {turn.get('name', '?')}({summary})]"
            out_blocks.append(head + ("\n" + result + "\n" if result else "\n"))
    header = (f"# Session {facts['id']}\n"
              f"Project: {facts['project_path']}\n"
              f"Raw lines: {total_lines}\n\n")
    return header + "\n".join(out_blocks)
