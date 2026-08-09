# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-08-09

### Added
- Initial public release of Session Logbook.
- Unified, read-only dashboard for Claude Code, Codex, and Antigravity sessions.
- Four-zone layout (Starred / Recent / Dusty / Archived) with automatic time decay and project grouping.
- Card previews, full conversation view, standalone reader, and user-message navigation (`j`/`k`).
- Full-text search (ripgrep-accelerated, pure-Python fallback).
- Star / Archive / Note, persisted to `~/.session-logbook/state.json`.
- Files panel (recent files + `fd`-backed name search).
- Downloadable anchored transcript export.

[Unreleased]: https://github.com/chyang-ken/session-logbook/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/chyang-ken/session-logbook/releases/tag/v0.1.0
