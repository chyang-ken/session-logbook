# Contributing

Thanks for your interest in Session Logbook. It's a small, deliberately-scoped tool — contributions that keep it sharp and simple are very welcome.

## Project shape

- **Backend:** a single `server.py` (Python 3.9+, standard library only — no third-party dependencies).
- **Frontend:** a single `index.html` (vanilla HTML/CSS/JS, no framework, **no build step**).
- **Sources:** `sources/` adapts each agent's on-disk format (Claude Code, Codex, Antigravity) into a common shape.
- **Tests:** `tests/` (Python `unittest`) with synthetic fixtures.

There is no bundler, transpiler, or package manager. The dev loop is: edit a file, restart `server.py` (for backend changes) or refresh the browser (for `index.html`).

## Getting set up

```bash
git clone https://github.com/chyang-ken/session-logbook.git
cd session-logbook
python3 server.py          # → http://127.0.0.1:47821
```

Optional, but the tool uses them when present:

- [`ripgrep`](https://github.com/BurntSushi/ripgrep) (`rg`) — accelerates full-text search (pure-Python fallback otherwise).
- [`fd`](https://github.com/sharkdp/fd) — powers the Files panel's name search.

## Running the tests

```bash
python3 -m unittest discover -s tests
```

All tests must pass before a PR is merged. CI runs the same command on every push and pull request.

## Conventions (please read — they keep this repo clean and global-friendly)

This project is developed in the open, for a worldwide audience. A few rules make that sustainable:

1. **English first.** Commit messages, public-facing code comments, docstrings, and repo docs are written in **English**. A `_zh-CN` companion (e.g. `README_zh-CN.md`) is welcome where it helps, but English is the source of truth.
2. **Never commit real session data.** This is a tool that reads *your* private agent logs. Test fixtures and examples must be **synthetic** — no real transcripts, no real usernames, no references to other projects you work on. When in doubt, invent placeholder data (`/Users/alice/my-app`, UUIDs like `aaaa…`).
3. **Keep scratch work out of git.** Experiments, scratch analysis, and personal R&D belong in the git-ignored `_private/` directory (or a separate private repo) — never in the public history. See [`CLAUDE.md`](CLAUDE.md) for the full rationale.
4. **Respect the scope.** Before adding a feature, check [`docs/philosophy.md`](docs/philosophy.md). The dashboard is a read-only cockpit; "send a message", "spawn a session", "multi-user auth", and "live push" are explicit non-goals.
5. **Match the surrounding style.** For UI work, read [`docs/design-system.md`](docs/design-system.md) first — colors and hover semantics are tokenized, not ad-hoc.
6. **No external browser assets.** The default dashboard must work offline and must not load scripts, fonts, or styles from a CDN. Vendor small browser libraries and fonts under `vendor/` and record their versions and licenses in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

## Pull requests

- Keep PRs focused. One concern per PR.
- Describe the problem, the change, and how you verified it. The PR template will prompt you.
- For UI changes, include a before/after screenshot.
- Make sure `python3 -m unittest discover -s tests` is green.

## Reporting bugs & requesting features

Use the [issue templates](https://github.com/chyang-ken/session-logbook/issues/new/choose). For security issues, follow [`SECURITY.md`](SECURITY.md) instead of opening a public issue.
