# Examples

## Try the dashboard on synthetic demo data

You don't need any real sessions (or to touch your real `~/.claude`) to see how the
dashboard works. This directory ships a generator that builds a self-contained "demo home"
full of **synthetic** sessions across a few fake projects, with all four zones
(Recent / Dusty / Starred / Archived) populated.

```bash
# 1. Generate the demo home (writes ./examples/demo-home/, which is git-ignored)
python3 examples/make_demo.py

# 2. Point the dashboard at it via HOME — your real ~/.claude is untouched
HOME="$(pwd)/examples/demo-home" python3 server.py

# 3. Open it
open http://127.0.0.1:47821
```

The screenshot in the project README is produced exactly this way.

### How the `HOME` trick works

The dashboard reads sessions from `~/.claude`, `~/.codex`, and `~/.gemini`. Those paths
resolve relative to your home directory, so overriding `HOME` for a single command points
the dashboard at the demo data without changing any code or affecting your real sessions.

> Everything under `demo-home/` is fabricated — fake usernames, fake projects, fake
> dialog. Regenerate or delete it freely.
