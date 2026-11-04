# ghostsnap

Save and restore your [Ghostty](https://ghostty.org) windows and tabs on macOS,
together with the AI coding agents that run in them.

You have 12 tabs open. Each tab has a title, a directory, and maybe a
Claude Code or Codex conversation. You must reboot. ghostsnap saves all of
that in a snapshot. After the reboot, one command opens the same tabs again,
with the same titles and directories, and resumes each conversation.

```
$ ghostsnap save -n before-reboot
Saved snapshot 20261104-233507 (before-reboot): 12 tabs, 8 Claude, 1 Codex.

# ... reboot ...

$ ghostsnap restore before-reboot
Restoring snapshot 20261104-233507 (before-reboot) ...
  tab  1  generalpurpose                 resume claude
  tab  2  PD                             resume codex
  tab  3  MONI                           resume claude
  tab  6  CURL                           shell
  ...
Done.
```

## What it saves and restores

For each Ghostty window, tab and split:

| Item | Saved | Restored |
|---|---|---|
| Window and tab order, selected tab, front window | yes | yes |
| Tab title that you set (`set_tab_title`), terminal title override | yes | yes |
| Working directory of the shell | yes | yes |
| Splits | yes (count and directories) | yes, as splits to the right (Ghostty does not expose direction or size) |
| Claude Code session | session id, name, directory, flags | `claude --resume <id>` in the session directory |
| Codex session | thread id, directory, flags | `codex resume <id>` in the thread directory |
| Other running commands (`pnpm dev`, `tail -f` ...) | command line, for reference | no: the tab opens with a shell |
| Window position and size, scrollback | no | no |

Flags that shape a session are kept, for example `--model`, `--permission-mode`,
`--dangerously-skip-permissions`, `--add-dir`, `--allowedTools` (Claude) and
`-m`, `-c`, `-s`, `-a`, `--search` (Codex). Prompts, `-w <worktree>` and
`-n <name>` are not typed again. A session that you started with `claude -w`
resumes in its worktree, and the shell goes back to the original directory
when the agent exits.

## Requirements

- macOS.
- Ghostty 1.4 or later. ghostsnap uses the Ghostty AppleScript API. It was
  tested with Ghostty 1.4.0.
- Python 3.9 or later (standard library only).
- Claude Code and/or Codex, if you want to resume agents.

The first time, macOS can ask for permission to let your terminal control
Ghostty (System Settings > Privacy & Security > Automation).

## Install

With [uv](https://docs.astral.sh/uv/):

```sh
uv tool install git+https://github.com/maximetinu/ghostsnap
```

Or from a clone (this links the script, so `git pull` updates it):

```sh
git clone https://github.com/maximetinu/ghostsnap ~/git/ghostsnap
~/git/ghostsnap/install.sh            # links ~/.local/bin/ghostsnap
~/git/ghostsnap/install.sh --skill    # also links the Claude Code skill
```

Then run `ghostsnap doctor`.

## Use

Run `ghostsnap` with no command to get a menu:

```
  #  ID                 NAME                   SAVED            TABS CLAUDE CODEX
  1  20261104-233507    before-reboot          2026-11-04 23:35   12      8     1

  s        save the current state      i        inspect the current state
  r [N]    restore snapshot N          v N      view snapshot N
  d N      delete snapshot N           n N      rename snapshot N
  q        quit
```

Or use the commands directly:

| Command | What it does |
|---|---|
| `ghostsnap inspect` | Show the current windows, tabs, directories and agents. Saves nothing. |
| `ghostsnap save [-n NAME] [--note TEXT] [--window ID\|front]` | Save the current state as a new snapshot. |
| `ghostsnap list` | List snapshots, newest first. |
| `ghostsnap show [REF]` | Show the tabs in a snapshot. |
| `ghostsnap restore [REF]` | Open the tabs of a snapshot and resume its agents. |
| `ghostsnap rename REF NAME [--note TEXT]` | Rename a snapshot. |
| `ghostsnap delete REF [-y]` | Delete a snapshot. |
| `ghostsnap prune --keep N` | Keep only the N newest snapshots. |
| `ghostsnap doctor` | Check that ghostsnap can work on this Mac. |
| `ghostsnap path` | Print the snapshot folder. |

`REF` is `latest` (the default), a list number (`1` is the newest), an id, an
id prefix, or a name.

### Restore options

| Option | Effect |
|---|---|
| `--dry-run` | Print the plan and the exact commands. Open nothing. |
| `--tabs 1,3,5-7` | Restore only these tabs (numbers from `show`). |
| `--no-agents` | Open the tabs and directories, but start no agent. |
| `--window front` | Add the tabs to the front window, not to a new window. |
| `--stagger 1.0` | Seconds to wait after each tab that starts an agent. |
| `--force` | Start an agent even if the same session already runs. |
| `--close-idle` | After the restore, close the other windows in which every tab only shows an idle shell. |

ghostsnap never starts a session that already runs. If you restore a snapshot
while some of its agents still run, those tabs open with a shell only.

### After a reboot

Ghostty can reopen its last windows by itself (`window-save-state`). Those
tabs only contain new shells. After `ghostsnap restore`, you have the old
idle window and the new window with the agents. Use `--close-idle` (or answer
yes in the menu) to close the idle window. ghostsnap closes only windows in
which every tab is an idle shell. A window with any agent, command or
background job stays open.

## How it finds the sessions

- **Tabs**: the Ghostty AppleScript API gives windows, tabs, terminals,
  titles, the working directory, the TTY and the foreground process.
- **Processes**: `ps` lists the processes on each TTY. The full command line
  comes from `sysctl(KERN_PROCARGS2)`, and directories come from `lsof`.
- **Claude Code**: each interactive session writes
  `~/.claude/sessions/<pid>.json` with its session id, name and directory.
  ghostsnap checks the process start time, so a stale file of an old process
  is not used. The transcript in `~/.claude/projects/` tells the directory
  where `claude --resume` must run.
- **Codex**: Codex terminals talk to a shared app-server, so no file maps a
  process to a thread. ghostsnap uses, in this order: `codex resume <id>` in
  the command line, a thread id in the terminal title, and then the newest
  top-level thread in the same directory that the app-server holds open
  (`~/.codex/state_*.sqlite`). A snapshot marks the last case as a best guess
  (`resolved_by: heuristic-cwd`).

## For scripts and AI agents

Every command takes `--json` and then prints only JSON on stdout. No command
asks a question when stdin is not a terminal. Exit codes:

| Code | Meaning |
|---|---|
| 0 | OK |
| 1 | Error |
| 2 | Usage error |
| 3 | Ghostty is not running or has no window |
| 4 | Snapshot not found |

Examples:

```sh
ghostsnap inspect --json                 # what runs in each tab now
ghostsnap save -n before-update --json   # save, get the id and warnings
ghostsnap restore latest --dry-run --json
ghostsnap restore latest --tabs 3 --json
```

The folder `skills/ghostsnap/` contains a Claude Code skill that tells an
agent how to use the tool. `install.sh --skill` links it into
`~/.claude/skills/`.

## Files and privacy

Snapshots are JSON files in `~/.local/state/ghostsnap/snapshots/` (change it
with `GHOSTSNAP_DIR`). The folder is private (mode 700) and each file has
mode 600. A snapshot contains directories, tab titles and command lines.
Command lines can contain prompts or secrets, so do not share snapshot files.

ghostsnap also reads `CLAUDE_CONFIG_DIR` and `CODEX_HOME` if you set them.

## Development

```sh
python3 -m unittest discover -s tests -v
```

The tests do not need Ghostty, Claude Code or Codex. CI runs them on every
push.

## License

MIT
