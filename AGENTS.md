# Notes for agents that work on this repo

ghostsnap is one Python file, `ghostsnap.py`, with no dependencies. Keep it
that way: standard library only, Python 3.9 compatible
(`from __future__ import annotations`, no `match`).

## Layout

- `ghostsnap.py`: the CLI. Sections: AppleScript (read and create Ghostty
  windows), processes (`ps`, `sysctl KERN_PROCARGS2`, `lsof`), agent
  detection and flag filters, Claude Code and Codex session lookup, capture,
  snapshot storage, restore plan and execution, output, commands, menu.
- `tests/test_ghostsnap.py`: unit tests with fake `ps` output, fake
  `~/.claude` and `~/.codex` folders (through `CLAUDE_CONFIG_DIR` and
  `CODEX_HOME`) and fake snapshots (`GHOSTSNAP_DIR`).
- `skills/ghostsnap/SKILL.md`: the Claude Code skill for users of the tool.

## Rules

- Run the unit tests in CI (push and watch the run). They do not need Ghostty.
- To test against a real Ghostty, open a separate test window, use
  `save --window <id>` and `inspect --window <id>`, and close only that window
  by its id. Never close or restore into windows that you did not open.
- Every command must support `--json` and must not prompt when stdin is not a
  TTY. Keep the exit codes stable (see README).
- When Claude Code or Codex change their session files, update the lookup
  functions and add a test with the new format.

## Usage by agents

See `skills/ghostsnap/SKILL.md`.
