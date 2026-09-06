# A session that cd's outside its project keeps its startup directory

## Decision

`pick_project_path` treats the folder anchor — the session's startup cwd, recovered from the
encoded project directory name — as the floor for the project path. When the commonpath of all
visited cwd values escapes above that anchor, the anchor wins. Without a usable anchor, a
commonpath that is merely a bare home directory (`/Users/<name>`, `/home/<name>`) is rejected the
same way `/`, `/Users` and `/home` already were, and the last cwd is used instead.

This supersedes the earlier project-path strategy note, which was written before the open-source
cutover and did not travel with the fresh history; `server.py` and `tests/test_server.py` now
reference this record instead of that missing file.

## Rationale

The session list and the conversation view disagreed for real sessions: the list grouped a session
under the home directory (`~`) while the detail view showed its actual project. The two derive
`project_path` differently — `extract_conversation` takes the first recorded cwd, while the list
runs `pick_project_path` over the whole cwd sequence.

An agent that changes directory into an unrelated project mid-session drags the commonpath up to
whatever ancestor the two projects share, which on a normal machine is the user's home directory.
The previous guard only rejected `/`, `/Users` and `/home`, so a bare home directory passed through
and became the project path. The docstring already claimed such a value was rejected, so the code
and its stated contract had drifted apart.

Keeping the anchor restores the module's stated philosophy — the startup cwd expresses project
intent, a mid-session `cd` is an implementation detail — and makes the two views agree.

## Alternatives and why rejected

- **Fall back to the last cwd** (what the docstring literally promised): would group the session
  under whichever directory the agent happened to visit last, which is even less related to the
  project than the home directory. It also still disagrees with the detail view.
- **Make the detail view run `pick_project_path` too**: makes the two views consistent by
  propagating the wrong value to both, rather than fixing it.
- **Add the home directory to the shallow-root set only**: fixes the reported case but leaves the
  anchor unused whenever the shared ancestor happens to be deeper than home, e.g. two projects
  under a common parent directory.

## Evidence

- `tests/test_server.py::TestPickProjectPath` — two new cases:
  `test_cd_into_unrelated_project_keeps_anchor` (anchor beats an escaping commonpath) and
  `test_no_anchor_home_commonpath_falls_back_to_last` (a bare home directory is not a project root).
  Both use synthetic paths.
- Differential check of the old and new selection over a local scan of 1689 sessions that carry a
  cwd sequence: 11 change, every one of them from a home or temporary directory to the session's
  actual project root; no other session is affected. The scan reads private session data, so
  neither the script nor its output is committed — the aggregate counts above contain no session
  content.
- Post-fix API check: for the reported session, `GET /api/sessions` and
  `GET /api/sessions/:id/conversation` now return the same `project_path`.

## Commit

Implemented by `c2b0fec` (the fix) and the commit that adds this record.
