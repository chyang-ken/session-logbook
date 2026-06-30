# Security Policy

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

Instead, use GitHub's private reporting:

1. Go to the repository's **Security** tab → **Report a vulnerability** ([Private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)).
2. Describe the issue, the steps to reproduce, and the potential impact.

You can expect an initial response within a few days. If a fix is warranted, we'll coordinate a disclosure timeline with you.

## Scope notes

Session Logbook is a **local-only** tool by design:

- It binds `127.0.0.1` only and has **no authentication** — it assumes a single trusted user on `localhost`. Do not expose it to a network you don't fully control (e.g. via a reverse proxy, port forward, or `0.0.0.0` bind) without adding your own authentication layer.
- It reads your agent session files **read-only**. The only data it writes is its own state file (`~/.session-logbook/state.json`).
- It serves browser assets from this repository and does not load scripts, fonts, or styles from external CDNs in the default UI.
- Session transcripts can contain secrets you pasted into your agent. Treat the dashboard, and anything you export from it, with the same care as the raw session logs.

If you find a way for the tool to write outside its state directory, leak data off the machine, or be reached without authentication from its default configuration, that's in scope — please report it.
