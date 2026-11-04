---
name: ghostsnap
description: Save, inspect and restore Ghostty windows and tabs (titles, directories, splits) together with the Claude Code and Codex sessions that run in them, with the ghostsnap CLI. Use when the user wants to save the terminal state before a reboot or update, restore tabs and agent sessions after a reboot or crash, list or manage saved snapshots, or asks what runs in each Ghostty tab.
---

# ghostsnap

`ghostsnap` is a CLI on PATH (macOS, Ghostty 1.4+). Always pass `--json` and
read the JSON. Run `ghostsnap --help` or `ghostsnap <command> --help` for all
options.

## Commands

| Goal | Command |
|---|---|
| What runs in each tab now | `ghostsnap inspect --json` |
| Save the current state | `ghostsnap save -n <short-name> [--note "..."] --json` |
| List snapshots (newest first) | `ghostsnap list --json` |
| See one snapshot | `ghostsnap show <ref> --json` |
| See what a restore will do | `ghostsnap restore <ref> --dry-run --json` |
| Restore | `ghostsnap restore <ref> --json` |
| Restore some tabs | `ghostsnap restore <ref> --tabs 2,5 --json` |
| Rename, delete, prune | `ghostsnap rename <ref> <name>`, `ghostsnap delete <ref> -y`, `ghostsnap prune --keep 10` |
| Check the setup | `ghostsnap doctor --json` |

`<ref>` is `latest`, a list number (1 = newest), an id, an id prefix or a name.

## Rules

1. Before a restore, run the same command with `--dry-run --json` and tell the
   user how many tabs and agents it opens.
2. A restore opens new Ghostty windows and starts agents. Do it only when the
   user asks for a restore.
3. Never pass `--close-idle` or `--force` unless the user asks for that
   exact behavior. `--close-idle` closes Ghostty windows.
4. ghostsnap skips sessions that already run. Tell the user which tabs it
   skipped (actions that start with `skip`).
5. After `save`, report the `warnings`: busy agents lose their current turn
   on a reboot, and a Codex `heuristic-cwd` match is a best guess.
6. Exit code 3 means Ghostty is not running; 4 means the snapshot is not
   found.
