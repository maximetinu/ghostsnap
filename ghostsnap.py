#!/usr/bin/env python3
"""ghostsnap: save and restore Ghostty windows, tabs and the AI agent sessions
(Claude Code, Codex) that run in them.

macOS only. Uses the Ghostty AppleScript API (Ghostty 1.4 or later), `ps`,
`lsof` and the session files that Claude Code and Codex keep on disk.
Python standard library only. See README.md.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import datetime as dt
import glob
import json
import os
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

__version__ = "0.1.0"
FORMAT = 1

US, RS = "\x1f", "\x1e"  # field and record separators for the AppleScript output

SHELLS = {
    "zsh", "bash", "fish", "sh", "dash", "ksh", "tcsh", "csh",
    "nu", "xonsh", "elvish", "pwsh", "login",
}

EXIT_ERROR, EXIT_USAGE, EXIT_NO_GHOSTTY, EXIT_NOT_FOUND = 1, 2, 3, 4


class GsError(Exception):
    def __init__(self, msg: str, code: int = EXIT_ERROR):
        super().__init__(msg)
        self.code = code


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

def data_dir() -> Path:
    if os.environ.get("GHOSTSNAP_DIR"):
        return Path(os.environ["GHOSTSNAP_DIR"]).expanduser()
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / "ghostsnap"


def snap_dir() -> Path:
    return data_dir() / "snapshots"


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"))


def codex_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))


# --------------------------------------------------------------------------
# AppleScript
# --------------------------------------------------------------------------

def osa(script: str, *args: object, timeout: float = 60) -> str:
    """Run an AppleScript from stdin with `on run argv` arguments."""
    try:
        p = subprocess.run(
            ["osascript", "-", *[str(a) for a in args]],
            input=script, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        raise GsError("osascript not found. ghostsnap runs on macOS only.")
    except subprocess.TimeoutExpired:
        raise GsError("osascript timed out while it talked to Ghostty.")
    if p.returncode != 0:
        err = p.stderr.strip()
        if "-1743" in err or "Not authorized" in err:
            raise GsError(
                "macOS blocked Apple Events to Ghostty. Allow the terminal app in "
                "System Settings > Privacy & Security > Automation > Ghostty."
            )
        raise GsError(f"osascript error: {err}")
    out = p.stdout
    return out[:-1] if out.endswith("\n") else out


READ_SCRIPT = r'''
on s(x)
  if x is missing value then return ""
  return x as text
end s

on run argv
  set FS to character id 31
  set RSEP to character id 30
  set out to {}
  tell application "Ghostty"
    set frontId to ""
    try
      set frontId to id of front window
    end try
    set end of out to "A" & FS & (my s(version)) & FS & frontId
    repeat with w in windows
      set wid to id of w
      set end of out to "W" & FS & wid & FS & (my s(name of w))
      repeat with t in tabs of w
        set tid to id of t
        set ftid to ""
        try
          set ftid to id of focused terminal of t
        end try
        set end of out to "T" & FS & wid & FS & tid & FS & (my s(index of t)) & FS & (my s(selected of t)) & FS & (my s(name of t)) & FS & (my s(name override of t)) & FS & ftid
        repeat with x in terminals of t
          set end of out to "S" & FS & wid & FS & tid & FS & (id of x) & FS & (my s(tty of x)) & FS & (my s(pid of x)) & FS & (my s(working directory of x)) & FS & (my s(name of x)) & FS & (my s(name override of x))
        end repeat
      end repeat
    end repeat
  end tell
  set AppleScript's text item delimiters to RSEP
  return out as text
end run
'''

NEW_TAB_SCRIPT = r'''
on run argv
  set winId to item 1 of argv
  set cwd to item 2 of argv
  set inputText to item 3 of argv
  set tabTitle to item 4 of argv
  set termTitle to item 5 of argv
  tell application "Ghostty"
    set cfg to new surface configuration
    if cwd is not "" then set initial working directory of cfg to cwd
    if inputText is not "" then set initial input of cfg to inputText & linefeed
    if winId is "" then
      set w to new window with configuration cfg
      set t to selected tab of w
    else
      set w to window id winId
      set t to new tab in w with configuration cfg
    end if
    if tabTitle is not "" then set name override of t to tabTitle
    set x to focused terminal of t
    if termTitle is not "" then set name override of x to termTitle
    return (id of w) & (character id 31) & (id of t) & (character id 31) & (id of x)
  end tell
end run
'''

SPLIT_SCRIPT = r'''
on run argv
  set termId to item 1 of argv
  set cwd to item 2 of argv
  set inputText to item 3 of argv
  set termTitle to item 4 of argv
  tell application "Ghostty"
    set cfg to new surface configuration
    if cwd is not "" then set initial working directory of cfg to cwd
    if inputText is not "" then set initial input of cfg to inputText & linefeed
    set x to split (terminal id termId) direction right with configuration cfg
    if termTitle is not "" then set name override of x to termTitle
    return id of x
  end tell
end run
'''

SELECT_TAB_SCRIPT = r'''
on run argv
  tell application "Ghostty"
    select tab (tab id (item 2 of argv) of window id (item 1 of argv))
  end tell
end run
'''

ACTIVATE_WINDOW_SCRIPT = r'''
on run argv
  tell application "Ghostty"
    activate window (window id (item 1 of argv))
  end tell
end run
'''

CLOSE_WINDOW_SCRIPT = r'''
on run argv
  tell application "Ghostty"
    close window (window id (item 1 of argv))
  end tell
end run
'''

CLOSE_TAB_SCRIPT = r'''
on run argv
  tell application "Ghostty"
    close tab (tab id (item 2 of argv) of window id (item 1 of argv))
  end tell
end run
'''


def ghostty_running() -> bool:
    try:
        return osa('return (application "Ghostty" is running) as text') == "true"
    except GsError:
        return False


def read_ghostty() -> dict:
    raw = osa(READ_SCRIPT)
    windows: list = []
    wmap: dict = {}
    tmap: dict = {}
    version = front = ""
    for rec in raw.split(RS):
        f = rec.split(US)
        kind = f[0]
        if kind == "A" and len(f) >= 3:
            version, front = f[1], f[2]
        elif kind == "W" and len(f) >= 3:
            w = {"id": f[1], "title": f[2], "tabs": []}
            windows.append(w)
            wmap[f[1]] = w
        elif kind == "T" and len(f) >= 8:
            t = {
                "id": f[2], "index": int(f[3] or 0), "selected": f[4] == "true",
                "title": f[5], "title_override": f[6], "focused_terminal": f[7],
                "terminals": [],
            }
            wmap[f[1]]["tabs"].append(t)
            tmap[(f[1], f[2])] = t
        elif kind == "S" and len(f) >= 9:
            tmap[(f[1], f[2])]["terminals"].append({
                "id": f[3], "tty": f[4], "pid": int(f[5] or 0), "cwd": f[6],
                "title": f[7], "title_override": f[8],
            })
    for w in windows:
        w["tabs"].sort(key=lambda t: t["index"])
    return {"version": version, "front_window": front, "windows": windows}


# --------------------------------------------------------------------------
# Processes
# --------------------------------------------------------------------------

def ps_all() -> dict:
    out = subprocess.run(
        ["ps", "-axww", "-o", "pid=,ppid=,pgid=,tpgid=,stat=,tty=,lstart=,comm="],
        capture_output=True, text=True, env=dict(os.environ, LC_ALL="C"),
    ).stdout
    return parse_ps(out)


def parse_ps(out: str) -> dict:
    procs = {}
    for line in out.splitlines():
        parts = line.split(None, 11)
        if len(parts) < 12:
            continue
        try:
            pid, ppid, pgid, tpgid = (int(x) for x in parts[:4])
        except ValueError:
            continue
        lstart = " ".join(parts[6:11])
        try:
            start = time.mktime(time.strptime(lstart, "%a %b %d %H:%M:%S %Y"))
        except ValueError:
            start = 0.0
        procs[pid] = {
            "pid": pid, "ppid": ppid, "pgid": pgid, "tpgid": tpgid,
            "stat": parts[4], "tty": parts[5], "lstart": lstart, "start": start,
            "comm": parts[11],
        }
    return procs


_libc = None


def proc_args(pid: int):
    """Return (exec_path, argv) of a process via sysctl KERN_PROCARGS2."""
    global _libc
    try:
        if _libc is None:
            _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        argmax = ctypes.c_int(0)
        size = ctypes.c_size_t(ctypes.sizeof(argmax))
        mib = (ctypes.c_int * 2)(1, 8)  # CTL_KERN, KERN_ARGMAX
        if _libc.sysctl(mib, 2, ctypes.byref(argmax), ctypes.byref(size), None, 0) != 0:
            return None, None
        buf = ctypes.create_string_buffer(argmax.value)
        size = ctypes.c_size_t(argmax.value)
        mib3 = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2, pid
        if _libc.sysctl(mib3, 3, buf, ctypes.byref(size), None, 0) != 0:
            return None, None
        return parse_procargs2(buf.raw[: size.value])
    except Exception:
        return None, None


def parse_procargs2(data: bytes):
    argc = int.from_bytes(data[:4], sys.byteorder)
    rest = data[4:]
    i = rest.find(b"\0")
    exec_path = rest[:i].decode("utf-8", "replace")
    rest = rest[i:].lstrip(b"\0")
    argv = [a.decode("utf-8", "replace") for a in rest.split(b"\0")[:argc]]
    return exec_path, argv


def proc_cwds(pids) -> dict:
    pids = [str(p) for p in pids if p]
    if not pids:
        return {}
    out = subprocess.run(
        ["lsof", "-a", "-d", "cwd", "-Fn", "-p", ",".join(pids)],
        capture_output=True, text=True,
    ).stdout
    res, cur = {}, None
    for line in out.splitlines():
        if line.startswith("p"):
            cur = int(line[1:])
        elif line.startswith("n") and cur is not None:
            res[cur] = line[1:]
    return res


def base(p: str) -> str:
    return os.path.basename(p or "").lstrip("-")


def is_shell(comm: str) -> bool:
    return base(comm) in SHELLS


# --------------------------------------------------------------------------
# Agent detection and command-line flags
# --------------------------------------------------------------------------

CLAUDE_SUBCMDS = {
    "agents", "attach", "auth", "auto-mode", "doctor", "gateway", "import",
    "install", "logs", "mcp", "plugin", "plugins", "purge", "respawn", "rm",
    "setup-token", "stop", "kill", "update", "upgrade", "rc", "remote-control",
    "migrate-installer", "config", "help",
}
CLAUDE_KEEP_VALUE = {
    "--model", "--permission-mode", "--effort", "--agent", "--settings",
    "--setting-sources", "--fallback-model", "--append-system-prompt",
    "--system-prompt", "--append-system-prompt-file", "--system-prompt-file",
}
CLAUDE_KEEP_MULTI = {
    "--add-dir", "--mcp-config", "--plugin-dir", "--allowedTools",
    "--allowed-tools", "--disallowedTools", "--disallowed-tools", "--tools",
}
CLAUDE_KEEP_BOOL = {
    "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
    "--chrome", "--no-chrome", "--ide", "--strict-mcp-config", "--verbose", "--brief",
}

CODEX_NONINTERACTIVE = {
    "exec", "e", "review", "login", "logout", "mcp", "mcp-server", "app-server",
    "app", "completion", "sandbox", "debug", "apply", "a", "cloud", "features",
    "help", "generate-ts", "generate-json-schema", "responses-api-proxy",
    "stdio-to-uds", "proto",
}
CODEX_VALUE_OPTS = {
    "-c", "--config", "--enable", "--disable", "--remote", "--remote-auth-token-env",
    "-i", "--image", "-m", "--model", "--local-provider", "-p", "--profile",
    "-s", "--sandbox", "-a", "--ask-for-approval", "-C", "--cd", "--add-dir",
}
CODEX_KEEP_VALUE = {
    "-c", "--config", "--enable", "--disable", "-m", "--model", "--local-provider",
    "-p", "--profile", "-s", "--sandbox", "-a", "--ask-for-approval", "--add-dir",
    "--remote", "--remote-auth-token-env",
}
CODEX_KEEP_BOOL = {
    "--search", "--dangerously-bypass-approvals-and-sandbox", "--approve-for-me",
    "--full-auto", "--oss", "--no-alt-screen", "--no-daemon", "--strict-config",
    "--dangerously-bypass-hook-trust", "--yolo",
}


def agent_kind(exec_path: str, argv: list):
    """Return (kind, args after the program name) or (None, [])."""
    if not argv:
        return None, []
    a0 = base(argv[0])
    ex = exec_path or ""
    if a0 == "claude" or "/claude/versions/" in ex or base(ex) == "claude":
        return "claude", argv[1:]
    if a0 == "codex" or base(ex) == "codex":
        return "codex", argv[1:]
    if a0 in ("node", "bun", "deno") and len(argv) > 1:
        a1 = argv[1]
        if base(a1) == "claude" or "claude-code" in a1:
            return "claude", argv[2:]
        if base(a1) in ("codex", "codex.js") or "/@openai/codex/" in a1:
            return "codex", argv[2:]
    return None, []


def filter_flags(args: list, keep_value: set, keep_multi: set, keep_bool: set) -> list:
    """Keep only the options that shape a session (model, permissions, ...).

    Prompts, session names, worktree names and resume ids are dropped.
    """
    out: list = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            break
        if a.startswith("--") and "=" in a:
            if a.split("=", 1)[0] in keep_value | keep_multi | keep_bool:
                out.append(a)
            i += 1
            continue
        if a in keep_bool:
            out.append(a)
            i += 1
            continue
        if a in keep_value and i + 1 < len(args):
            out += [a, args[i + 1]]
            i += 2
            continue
        if a in keep_multi:
            j, vals = i + 1, []
            while j < len(args) and not args[j].startswith("-"):
                vals.append(args[j])
                j += 1
            if vals:
                out += [a, *vals]
            i = j
            continue
        i += 1
    return out


def codex_positionals(args: list) -> list:
    pos, i = [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            pos.extend(args[i + 1:])
            break
        if a.startswith("-") and a != "-":
            i += 2 if (a in CODEX_VALUE_OPTS) else 1
            continue
        pos.append(a)
        i += 1
    return pos


def claude_interactive(args: list) -> bool:
    if any(a in ("-p", "--print", "-h", "--help", "-v", "--version") for a in args):
        return False
    return not (args and args[0] in CLAUDE_SUBCMDS)


def codex_mode(args: list):
    """Return (interactive, resume_ref)."""
    if any(a in ("-h", "--help", "-V", "--version") for a in args):
        return False, None
    pos = codex_positionals(args)
    if not pos:
        return True, None
    if pos[0] in CODEX_NONINTERACTIVE:
        return False, None
    if pos[0] in ("resume", "fork"):
        ref = pos[1] if len(pos) > 1 and pos[0] == "resume" else None
        return True, ref
    return True, None


UUID_PREFIX_RE = re.compile(r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{1,4}[0-9a-f-]*)")


def title_id_prefix(title: str):
    m = UUID_PREFIX_RE.search(title or "")
    return m.group(1).rstrip("-") if m else None


def short(text: str, n: int = 80) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


# --------------------------------------------------------------------------
# Claude Code sessions
# --------------------------------------------------------------------------

def sanitize_path(p: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", p)


def same_lstart(a: str, b: str) -> bool:
    """True if two `ps lstart` strings name the same instant.

    Claude Code writes procStart in UTC and `ps` prints local time, so a
    difference of a whole time zone offset (n x 15 minutes, max 14 h) is equal.
    """
    a, b = " ".join((a or "").split()), " ".join((b or "").split())
    if a == b:
        return True
    try:
        ta = dt.datetime.strptime(a, "%a %b %d %H:%M:%S %Y")
        tb = dt.datetime.strptime(b, "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return False
    diff = abs((ta - tb).total_seconds())
    return diff <= 14 * 3600 and diff % 900 == 0


def claude_session_file(pid: int, proc: dict | None):
    f = claude_dir() / "sessions" / f"{pid}.json"
    try:
        d = json.loads(f.read_text())
    except Exception:
        return None
    if proc and d.get("procStart") and not same_lstart(d["procStart"], proc.get("lstart", "")):
        return None  # stale file of an earlier process with the same pid
    return d


def claude_transcripts(sid: str) -> list:
    if not sid:
        return []
    return [Path(p) for p in glob.glob(str(claude_dir() / "projects" / "*" / f"{glob.escape(sid)}.jsonl"))]


def claude_resume_dir(sid: str, candidates: list):
    """Find the transcript and the directory that `claude --resume` must run in."""
    hits = claude_transcripts(sid)
    if not hits:
        return None, None
    cands = [c for c in candidates if c]
    for t in hits:
        proj = t.parent.name
        for c in cands:
            s = sanitize_path(c)
            if s == proj or (len(s) > 200 and proj.startswith(s[:200])):
                return c, str(t)
    t = hits[0]
    try:
        with open(t, encoding="utf-8", errors="replace") as fh:
            for n, line in enumerate(fh):
                if n > 200:
                    break
                try:
                    c = json.loads(line).get("cwd")
                except Exception:
                    continue
                if c and sanitize_path(c) == t.parent.name:
                    return c, str(t)
    except OSError:
        pass
    return (cands[0] if cands else None), str(t)


def claude_by_title_prefix(prefix: str):
    if not prefix or len(prefix) < 8:
        return None
    hits = glob.glob(str(claude_dir() / "projects" / "*" / f"{glob.escape(prefix)}*.jsonl"))
    ids = {Path(h).stem for h in hits}
    return ids.pop() if len(ids) == 1 else None


def live_claude_sessions(procs: dict) -> dict:
    """session id -> pid, for every Claude process that runs now (any terminal)."""
    res = {}
    for f in (claude_dir() / "sessions").glob("*.json"):
        try:
            d = json.loads(f.read_text())
            pid = int(d.get("pid") or f.stem)
        except Exception:
            continue
        p = procs.get(pid)
        if p and (not d.get("procStart") or same_lstart(d["procStart"], p["lstart"])):
            if d.get("sessionId"):
                res[d["sessionId"]] = pid
    return res


# --------------------------------------------------------------------------
# Codex sessions
# --------------------------------------------------------------------------

def codex_threads() -> list:
    dbs = sorted(
        codex_dir().glob("state_*.sqlite"),
        key=lambda p: int(re.sub(r"\D", "", p.stem) or 0),
    )
    if not dbs:
        return []
    try:
        con = sqlite3.connect(f"file:{dbs[-1]}?mode=ro", uri=True, timeout=2)
        cols = {r[1] for r in con.execute("pragma table_info(threads)")}
        want = ["id", "cwd", "title", "name", "updated_at", "created_at",
                "thread_source", "source", "archived", "first_user_message"]
        sel = ", ".join(c if c in cols else f"NULL AS {c}" for c in want)
        rows = con.execute(f"SELECT {sel} FROM threads").fetchall()
        con.close()
    except sqlite3.Error:
        return []
    threads = []
    for r in rows:
        t = dict(zip(want, r))
        src = str(t.get("source") or "")
        t["subagent"] = t.get("thread_source") == "subagent" or src.startswith('{"subagent"')
        threads.append(t)
    return threads


def codex_locked_ids() -> set:
    d = codex_dir() / "thread-writer-locks"
    files = [str(p) for p in d.glob("*.lock") if not p.name.startswith(".")]
    if not files:
        return set()
    out = subprocess.run(["lsof", "-Fn", "--", *files], capture_output=True, text=True).stdout
    return {Path(line[1:]).stem for line in out.splitlines() if line.startswith("n/")}


def assign_codex_threads(agents: list) -> None:
    """Find the thread id of each Codex process (in place).

    Codex TUIs talk to a shared app-server, so no file maps a pid to a thread.
    Order of evidence: `codex resume <id>` in argv, a thread id in the terminal
    title, then the newest top-level thread in the same directory that the
    app-server holds open.
    """
    if not agents:
        return
    threads = codex_threads()
    by_id = {t["id"]: t for t in threads}
    locked = codex_locked_ids()
    used: set = set()
    for a in agents:
        ref = a.get("resume_ref")
        if ref and ref in by_id:
            a.update(session_id=ref, resolved_by="argv")
        elif ref:
            named = [t for t in threads if t.get("name") == ref or t.get("title") == ref]
            if len(named) == 1:
                a.update(session_id=named[0]["id"], resolved_by="argv-name")
        if not a.get("session_id"):
            pre = title_id_prefix(a.get("terminal_title", ""))
            if pre:
                hits = [t for t in threads if t["id"].startswith(pre)]
                if len(hits) == 1:
                    a.update(session_id=hits[0]["id"], resolved_by="terminal-title")
        if a.get("session_id"):
            used.add(a["session_id"])
    for a in sorted(agents, key=lambda x: -x.get("start", 0)):
        if a.get("session_id"):
            continue
        cwd = a.get("cwd")
        cands = [
            t for t in threads
            if t.get("cwd") == cwd and not t["subagent"] and not t.get("archived")
            and t["id"] not in used
        ]
        held = [t for t in cands if t["id"] in locked]
        recent = [t for t in cands if (t.get("updated_at") or 0) >= a.get("start", 0) - 60]
        pool = held or recent
        if pool:
            best = max(pool, key=lambda t: t.get("updated_at") or 0)
            a.update(session_id=best["id"], resolved_by="heuristic-cwd")
            used.add(best["id"])
    for a in agents:
        t = by_id.get(a.get("session_id") or "")
        if t:
            a["name"] = t.get("name") or t.get("title") or ""
            a["name"] = short(a["name"], 80)
            a["session_cwd"] = t.get("cwd") or ""


# --------------------------------------------------------------------------
# Capture
# --------------------------------------------------------------------------

def capture(window_filter: str | None = None) -> dict:
    if not ghostty_running():
        raise GsError("Ghostty is not running.", EXIT_NO_GHOSTTY)
    g = read_ghostty()
    if not g["windows"]:
        raise GsError("Ghostty has no open windows.", EXIT_NO_GHOSTTY)
    procs = ps_all()
    by_tty: dict = {}
    for p in procs.values():
        by_tty.setdefault(p["tty"], []).append(p)

    agents: list = []
    cwd_needed: list = []
    windows = g["windows"]
    if window_filter:
        wf = g["front_window"] if window_filter == "front" else window_filter
        windows = [w for w in windows if w["id"] == wf]
        if not windows:
            raise GsError(f"No Ghostty window with id {window_filter}.", EXIT_NOT_FOUND)

    for w in windows:
        for t in w["tabs"]:
            for term in t["terminals"]:
                analyze_terminal(term, by_tty.get(term["tty"].replace("/dev/", ""), []), procs, agents)
                if term.get("agent"):
                    cwd_needed.append(term["agent"]["pid"])
                if term.get("shell_pid") and not term.get("cwd"):
                    cwd_needed.append(term["shell_pid"])

    cwds = proc_cwds(cwd_needed)
    for w in windows:
        for t in w["tabs"]:
            for term in t["terminals"]:
                if not term.get("cwd") and term.get("shell_pid") in cwds:
                    term["cwd"] = cwds[term["shell_pid"]]
                a = term.get("agent")
                if a:
                    a["cwd"] = cwds.get(a["pid"], "") or term.get("cwd", "")

    claude_agents = [a for a in agents if a["kind"] == "claude"]
    codex_agents = [a for a in agents if a["kind"] == "codex"]
    for a in claude_agents:
        resolve_claude(a)
    assign_codex_threads(codex_agents)
    for a in agents:
        a.pop("start", None)
        a.pop("terminal_title", None)
        a.pop("resume_ref", None)
        a.pop("shell_cwd", None)

    out_windows = []
    for wi, w in enumerate(windows, 1):
        tabs = []
        for t in w["tabs"]:
            terms = []
            for term in t["terminals"]:
                terms.append({
                    "cwd": term.get("cwd", ""),
                    "title": term.get("title", ""),
                    "title_override": term.get("title_override", ""),
                    "focused": term["id"] == t.get("focused_terminal"),
                    "tty": term.get("tty", ""),
                    "state": term["state"],
                    "command": term.get("command"),
                    "agent": term.get("agent"),
                })
            tabs.append({
                "index": t["index"], "title": t["title"],
                "title_override": t["title_override"], "selected": t["selected"],
                "terminals": terms,
            })
        out_windows.append({
            "index": wi, "title": w["title"], "front": w["id"] == g["front_window"],
            "tabs": tabs,
        })
    return {
        "format": FORMAT,
        "tool": f"ghostsnap {__version__}",
        "captured_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "host": socket.gethostname(),
        "ghostty_version": g["version"],
        "windows": out_windows,
    }


def analyze_terminal(term: dict, plist: list, procs: dict, agents: list) -> None:
    pids = {p["pid"] for p in plist}
    # The shell is the first process on the tty whose parent is not on the tty
    # (Ghostty starts `login`, which starts the shell) and that is a shell.
    roots = sorted((p for p in plist if p["ppid"] not in pids), key=lambda p: p["start"])
    shell = None
    for r in roots:
        if is_shell(r["comm"]) and base(r["comm"]) != "login":
            shell = r
            break
        kids = sorted((p for p in plist if p["ppid"] == r["pid"]), key=lambda p: p["start"])
        for k in kids:
            if is_shell(k["comm"]):
                shell = k
                break
        if shell:
            break
    term["shell_pid"] = shell["pid"] if shell else None
    tpgid = plist[0]["tpgid"] if plist else -1

    found = []
    for p in plist:
        if base(p["comm"]) in SHELLS:
            continue
        exec_path, argv = proc_args(p["pid"])
        if argv is None:
            continue
        kind, args = agent_kind(exec_path, argv)
        if not kind:
            continue
        if kind == "claude":
            if not claude_interactive(args):
                continue
            ref = None
        else:
            interactive, ref = codex_mode(args)
            if not interactive:
                continue
        found.append((p, kind, args, argv, ref))

    def has_agent_ancestor(p) -> bool:
        cur = procs.get(p["ppid"])
        agent_pids = {f[0]["pid"] for f in found}
        while cur and cur["pid"] > 1:
            if cur["pid"] in agent_pids:
                return True
            cur = procs.get(cur["ppid"])
        return False

    top = [f for f in found if not has_agent_ancestor(f[0])]
    top.sort(key=lambda f: (f[0]["pgid"] != tpgid, f[0]["start"]))

    if top:
        p, kind, args, argv, ref = top[0]
        if kind == "claude":
            flags = filter_flags(args, CLAUDE_KEEP_VALUE, CLAUDE_KEEP_MULTI, CLAUDE_KEEP_BOOL)
        else:
            flags = filter_flags(args, CODEX_KEEP_VALUE, set(), CODEX_KEEP_BOOL)
        agent = {
            "kind": kind, "pid": p["pid"], "session_id": None, "name": "",
            "flags": flags, "argv_preview": short(" ".join([base(argv[0]), *args]), 160),
            "foreground": p["pgid"] == tpgid, "resolved_by": None,
            "start": p["start"], "lstart": p["lstart"], "terminal_title": term.get("title", ""),
            "resume_ref": ref, "shell_cwd": term.get("cwd", ""),
        }
        if len(top) > 1:
            agent["other_agents"] = [
                {"kind": f[1], "pid": f[0]["pid"], "argv_preview": short(" ".join(f[3]), 120)}
                for f in top[1:]
            ]
        term["agent"] = agent
        agents.append(agent)
        term["state"] = "agent"
        return

    fg = procs.get(term.get("pid") or 0)
    if not fg or (shell and fg["pid"] == shell["pid"]) or is_shell(fg["comm"]):
        term["state"] = "idle"
        return
    _, argv = proc_args(fg["pid"])
    term["state"] = "command"
    term["command"] = {
        "pid": fg["pid"],
        "argv": [a[:500] for a in (argv or [fg["comm"]])],
        "preview": short(" ".join(argv or [fg["comm"]]), 160),
    }


def resolve_claude(a: dict) -> None:
    proc = {"lstart": a.get("lstart", "")}
    sess = claude_session_file(a["pid"], proc)
    if sess:
        a["session_id"] = sess.get("sessionId")
        a["name"] = sess.get("name") or ""
        a["name_source"] = sess.get("nameSource") or ""
        a["status"] = sess.get("status") or ""
        a["session_cwd"] = sess.get("cwd") or ""
        a["resolved_by"] = "session-file"
    else:
        sid = claude_by_title_prefix(title_id_prefix(a.get("terminal_title", "")) or "")
        if sid:
            a["session_id"] = sid
            a["resolved_by"] = "terminal-title"
    if a.get("session_id"):
        rdir, transcript = claude_resume_dir(
            a["session_id"], [a.get("session_cwd"), a.get("cwd"), a.get("shell_cwd")]
        )
        a["resume_dir"] = rdir or a.get("session_cwd") or a.get("cwd") or ""
        a["transcript"] = transcript
    a.pop("lstart", None)


# --------------------------------------------------------------------------
# Snapshots on disk
# --------------------------------------------------------------------------

def list_snapshots() -> list:
    d = snap_dir()
    snaps = []
    for f in d.glob("*.json"):
        try:
            s = json.loads(f.read_text())
        except Exception:
            continue
        s["_path"] = str(f)
        snaps.append(s)
    snaps.sort(key=lambda s: s.get("captured_at", ""), reverse=True)
    return snaps


def write_snapshot(s: dict) -> Path:
    d = snap_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
        os.chmod(d.parent, 0o700)
    except OSError:
        pass
    path = d / f"{s['id']}.json"
    tmp = path.with_suffix(".tmp")
    data = {k: v for k, v in s.items() if not k.startswith("_")}
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def new_snapshot_id() -> str:
    base_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    sid, n = base_id, 2
    while (snap_dir() / f"{sid}.json").exists():
        sid, n = f"{base_id}-{n}", n + 1
    return sid


def find_snapshot(ref: str | None) -> dict:
    snaps = list_snapshots()
    if not snaps:
        raise GsError("There are no saved snapshots.", EXIT_NOT_FOUND)
    if ref in (None, "", "latest", "last"):
        return snaps[0]
    if re.fullmatch(r"\d{1,3}", ref):
        i = int(ref)
        if 1 <= i <= len(snaps):
            return snaps[i - 1]
    for s in snaps:
        if s.get("id") == ref:
            return s
    named = [s for s in snaps if s.get("name") == ref]
    if named:
        return named[0]
    pre = [s for s in snaps if s.get("id", "").startswith(ref)]
    if len(pre) == 1:
        return pre[0]
    raise GsError(f"No snapshot matches '{ref}'. Run `ghostsnap list`.", EXIT_NOT_FOUND)


def counts(s: dict) -> dict:
    c = {"windows": 0, "tabs": 0, "claude": 0, "codex": 0, "commands": 0}
    for w in s.get("windows", []):
        c["windows"] += 1
        for t in w["tabs"]:
            c["tabs"] += 1
            for term in t["terminals"]:
                a = term.get("agent")
                if a:
                    c[a["kind"]] = c.get(a["kind"], 0) + 1
                elif term.get("state") == "command":
                    c["commands"] += 1
    return c


def summary(s: dict, index: int | None = None) -> dict:
    out = {
        "id": s.get("id"), "name": s.get("name", ""), "note": s.get("note", ""),
        "captured_at": s.get("captured_at"), **counts(s),
    }
    if index is not None:
        out["index"] = index
    return out


# --------------------------------------------------------------------------
# Restore
# --------------------------------------------------------------------------

def agent_command(a: dict):
    """Return (shell command, run directory, resumes?, warnings) for an agent."""
    warns = []
    resumes = False
    if a["kind"] == "claude":
        parts = ["claude", *a.get("flags", [])]
        sid = a.get("session_id")
        rdir = a.get("resume_dir") or a.get("cwd") or ""
        if sid and claude_transcripts(sid):
            parts += ["--resume", sid]
            resumes = True
        elif sid:
            warns.append(f"Claude transcript for session {sid} not found: a new session starts.")
        else:
            warns.append("Claude session id unknown: a new session starts.")
        if a.get("name_source") == "user" and a.get("name"):
            parts += ["-n", a["name"]]
    else:
        sid = a.get("session_id")
        rdir = a.get("session_cwd") or a.get("cwd") or ""
        if sid:
            parts = ["codex", "resume", *a.get("flags", []), sid]
            resumes = True
        else:
            parts = ["codex", *a.get("flags", [])]
            warns.append("Codex thread id unknown: a new Codex session starts.")
    return " ".join(shlex.quote(p) for p in parts), rdir, resumes, warns


def terminal_plan(term: dict, opts, running: dict) -> dict:
    cwd = term.get("cwd") or os.path.expanduser("~")
    p = {"cwd": cwd, "input": "", "title_override": term.get("title_override", ""),
         "action": "shell", "warnings": []}
    if term.get("cwd") and not os.path.isdir(term["cwd"]):
        p["warnings"].append(f"Directory {term['cwd']} does not exist: the shell opens in the home directory.")
        p["cwd"] = os.path.expanduser("~")
    a = term.get("agent")
    if not a:
        if term.get("state") == "command" and term.get("command"):
            p["previous_command"] = term["command"].get("preview")
        return p
    p["agent"] = {"kind": a["kind"], "session_id": a.get("session_id"), "name": a.get("name", "")}
    if opts.no_agents:
        p["action"] = "shell (agents off)"
        return p
    sid = a.get("session_id")
    if sid and (a["kind"], sid) in running and not opts.force:
        p["action"] = "skip agent (already running)"
        p["warnings"].append(
            f"{a['kind']} session {sid} already runs (pid {running[(a['kind'], sid)]}): not started again."
        )
        return p
    cmd, rdir, resumes, warns = agent_command(a)
    p["warnings"] += warns
    if rdir and not os.path.isdir(rdir):
        p["warnings"].append(f"Agent directory {rdir} does not exist: the agent is not started.")
        p["action"] = "shell (agent directory missing)"
        return p
    if rdir and os.path.realpath(rdir) != os.path.realpath(p["cwd"]):
        cmd = f"cd {shlex.quote(rdir)} && {cmd}; cd {shlex.quote(p['cwd'])}"
    p["input"] = cmd
    p["action"] = f"{'resume' if resumes else 'start new'} {a['kind']}"
    return p


def running_agents() -> dict:
    """(kind, session id) -> pid for agents that run now."""
    res: dict = {}
    procs = ps_all()
    for sid, pid in live_claude_sessions(procs).items():
        res[("claude", sid)] = pid
    if ghostty_running():
        try:
            live = capture()
        except GsError:
            live = {"windows": []}
        for w in live["windows"]:
            for t in w["tabs"]:
                for term in t["terminals"]:
                    a = term.get("agent")
                    if a and a.get("session_id"):
                        res[(a["kind"], a["session_id"])] = a["pid"]
    return res


def parse_tab_list(text: str | None):
    if not text:
        return None
    sel = set()
    for part in text.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            sel.update(range(int(a), int(b) + 1))
        elif part:
            sel.add(int(part))
    return sel


def build_plan(s: dict, opts, running: dict) -> dict:
    tabsel = parse_tab_list(getattr(opts, "tabs", None))
    plan = {"snapshot": s.get("id"), "windows": []}
    n = 0
    for w in s.get("windows", []):
        pw = {"index": w.get("index"), "front": w.get("front", False), "tabs": []}
        for t in w["tabs"]:
            n += 1
            if tabsel is not None and n not in tabsel:
                continue
            pt = {
                "number": n, "title_override": t.get("title_override", ""),
                "title": t.get("title", ""), "selected": t.get("selected", False),
                "terminals": [terminal_plan(term, opts, running) for term in t["terminals"]],
            }
            pw["tabs"].append(pt)
        if pw["tabs"]:
            plan["windows"].append(pw)
    return plan


def execute_plan(plan: dict, opts, log) -> dict:
    result = {"windows": [], "warnings": []}
    front_target = None
    front_win = ""
    if opts.window == "front":
        try:
            front_win = read_ghostty().get("front_window", "")
        except GsError:
            front_win = ""
    for pw in plan["windows"]:
        win_id = front_win if opts.window == "front" else ""
        rw = {"window_id": None, "tabs": []}
        select = None
        for pt in pw["tabs"]:
            terms = pt["terminals"]
            first = terms[0]
            out = osa(NEW_TAB_SCRIPT, win_id, first["cwd"], first["input"],
                      pt["title_override"], first["title_override"])
            win_id, tab_id, term_id = out.split(US)
            rw["window_id"] = win_id
            log(f"  tab {pt['number']:>2}  {pt['title_override'] or short(pt['title'], 30):<30} {first['action']}")
            prev = term_id
            for extra in terms[1:]:
                prev = osa(SPLIT_SCRIPT, prev, extra["cwd"], extra["input"], extra["title_override"])
                log(f"          split  {extra['action']}")
            for term in terms:
                result["warnings"] += [f"tab {pt['number']}: {w}" for w in term["warnings"]]
            rw["tabs"].append({"number": pt["number"], "tab_id": tab_id,
                               "actions": [t["action"] for t in terms]})
            if pt.get("selected"):
                select = tab_id
            if any(t["input"] for t in terms):
                time.sleep(max(0.0, opts.stagger))
            else:
                time.sleep(0.2)
        if select and rw["window_id"]:
            try:
                osa(SELECT_TAB_SCRIPT, rw["window_id"], select)
            except GsError:
                pass
        if pw.get("front"):
            front_target = rw["window_id"]
        result["windows"].append(rw)
    if front_target:
        try:
            osa(ACTIVATE_WINDOW_SCRIPT, front_target)
        except GsError:
            pass
    return result


def my_tty():
    for fd in (0, 1, 2):
        try:
            return os.ttyname(fd)
        except OSError:
            continue
    return None


def terminal_activity(term: dict, procs: dict, by_tty: dict, mine, parent: int) -> str:
    """'idle' (only a shell), 'self' (the shell that runs ghostsnap) or 'busy'."""
    plist = by_tty.get(term["tty"].replace("/dev/", ""), [])
    if mine and term["tty"] == mine:
        p = procs.get(parent)
        return "self" if p and is_shell(p["comm"]) else "busy"
    fg = procs.get(term["pid"])
    if not fg or not is_shell(fg["comm"]):
        return "busy"
    for p in plist:
        if base(p["comm"]) == "login" or is_shell(p["comm"]):
            continue
        return "busy"  # any other process (also a stopped or background job)
    return "idle"


def idle_windows(keep_windows: set) -> list:
    """Windows where every tab only shows an idle shell.

    A window with any agent, running command or background job is never a
    candidate. The tab of a person who runs ghostsnap from a shell counts as
    idle; it closes last, after ghostsnap exits.
    """
    g = read_ghostty()
    procs = ps_all()
    by_tty: dict = {}
    for p in procs.values():
        by_tty.setdefault(p["tty"], []).append(p)
    mine, parent = my_tty(), os.getppid()
    out = []
    for w in g["windows"]:
        if w["id"] in keep_windows:
            continue
        self_tab, busy = None, False
        for t in w["tabs"]:
            for term in t["terminals"]:
                act = terminal_activity(term, procs, by_tty, mine, parent)
                if act == "busy":
                    busy = True
                elif act == "self":
                    self_tab = t["id"]
        if not busy:
            out.append({"window_id": w["id"], "title": w["title"], "tabs": len(w["tabs"]),
                        "self_tab": self_tab})
    return out


def close_idle_windows(keep_windows: set, log) -> dict:
    cands = idle_windows(keep_windows)
    closes_self = False
    for w in cands:
        if not w["self_tab"]:
            osa(CLOSE_WINDOW_SCRIPT, w["window_id"])
            continue
        # Close the other tabs now and this tab after ghostsnap exits.
        g = read_ghostty()
        for gw in g["windows"]:
            if gw["id"] == w["window_id"]:
                for t in gw["tabs"]:
                    if t["id"] != w["self_tab"]:
                        osa(CLOSE_TAB_SCRIPT, gw["id"], t["id"])
        script = CLOSE_TAB_SCRIPT.replace("'", "'\\''")
        subprocess.Popen(
            ["/bin/sh", "-c",
             f"sleep 1.5; printf '%s' '{script}' | osascript - '{w['window_id']}' '{w['self_tab']}'"],
            start_new_session=True, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        closes_self = True
    log(f"Closed {len(cands)} idle window(s)" + (" (this tab closes in 2 seconds)." if closes_self else "."))
    return {"closed_windows": cands, "closes_current_tab": closes_self}


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------

def use_color() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if use_color() else text


def home_short(p: str) -> str:
    h = os.path.expanduser("~")
    return "~" + p[len(h):] if p and (p == h or p.startswith(h + "/")) else (p or "")


def fmt_time(iso: str) -> str:
    try:
        return dt.datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso or ""


def print_json(obj) -> None:
    json.dump(obj, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def describe_terminal(term: dict) -> str:
    a = term.get("agent")
    if a:
        sid = (a.get("session_id") or "?")[:8]
        extra = []
        if a.get("name"):
            extra.append(short(a["name"], 40))
        if a.get("status"):
            extra.append(a["status"])
        if a.get("resolved_by") and a["resolved_by"] not in ("session-file", "argv"):
            extra.append(f"match: {a['resolved_by']}")
        label = c(a["kind"], "1;36" if a["kind"] == "claude" else "1;35")
        return f"{label} {sid}" + (f"  ({', '.join(extra)})" if extra else "")
    if term.get("state") == "command":
        return c("cmd ", "33") + short(term.get("command", {}).get("preview", ""), 60)
    return c("shell", "2")


def print_state(s: dict) -> None:
    n = 0
    for w in s.get("windows", []):
        front = "  (front)" if w.get("front") else ""
        print(c(f"Window {w.get('index')}{front}", "1") + c(f"  {short(w.get('title', ''), 60)}", "2"))
        for t in w["tabs"]:
            n += 1
            title = t.get("title_override") or t.get("title") or ""
            sel = c("*", "1;32") if t.get("selected") else " "
            for k, term in enumerate(t["terminals"]):
                num = f"{n:>3}{sel}" if k == 0 else "    "
                ttl = short(title, 24) if k == 0 else c("  split", "2")
                print(f"{num} {ttl:<24} {short(home_short(term.get('cwd', '')), 46):<46} {describe_terminal(term)}")
    c_ = counts(s)
    print(c(f"\n{c_['tabs']} tabs, {c_['claude']} Claude, {c_['codex']} Codex, "
            f"{c_['commands']} other commands. * = selected tab.", "2"))


def print_list(snaps: list) -> None:
    if not snaps:
        print("No snapshots yet. Run `ghostsnap save`.")
        return
    print(c(f"{'#':>3}  {'ID':<18} {'NAME':<22} {'SAVED':<16} {'TABS':>4} {'CLAUDE':>6} {'CODEX':>5}", "1"))
    for i, s in enumerate(snaps, 1):
        k = counts(s)
        print(f"{i:>3}  {s.get('id', ''):<18} {short(s.get('name', ''), 22):<22} "
              f"{fmt_time(s.get('captured_at', '')):<16} {k['tabs']:>4} {k['claude']:>6} {k['codex']:>5}"
              + (c(f"  {short(s['note'], 40)}", "2") if s.get("note") else ""))


def print_plan(plan: dict) -> None:
    for pw in plan["windows"]:
        print(c(f"Window {pw['index']}", "1"))
        for pt in pw["tabs"]:
            for k, tp in enumerate(pt["terminals"]):
                head = f"{pt['number']:>3} {short(pt['title_override'] or pt['title'], 24):<24}" if k == 0 else f"    {c('  split', '2'):<24}"
                print(f"{head} {short(home_short(tp['cwd']), 40):<40} {tp['action']}")
                if tp["input"]:
                    print(c(f"      $ {short(tp['input'], 150)}", "2"))
                for wmsg in tp["warnings"]:
                    print(c(f"      ! {wmsg}", "33"))


def collect_warnings(s: dict) -> list:
    out, n = [], 0
    for w in s.get("windows", []):
        for t in w["tabs"]:
            n += 1
            for term in t["terminals"]:
                a = term.get("agent")
                if not a:
                    continue
                if not a.get("session_id"):
                    out.append(f"tab {n}: {a['kind']} session id not found; restore starts a new session.")
                elif a.get("resolved_by") == "heuristic-cwd":
                    out.append(f"tab {n}: Codex thread {a['session_id']} is a best guess (same directory, newest).")
                if a.get("status") == "busy":
                    out.append(f"tab {n}: Claude is busy now; its current turn is lost if you reboot before it ends.")
                if a.get("other_agents"):
                    out.append(f"tab {n}: more than one agent runs in this terminal; only the foreground one is saved.")
    return out


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_inspect(args) -> int:
    s = capture(args.window)
    if args.json:
        print_json(s)
    else:
        print_state(s)
        for wmsg in collect_warnings(s):
            print(c(f"! {wmsg}", "33"))
    return 0


def do_save(name: str = "", note: str = "", window: str | None = None) -> dict:
    s = capture(window)
    s["id"] = new_snapshot_id()
    s["name"] = name or ""
    s["note"] = note or ""
    path = write_snapshot(s)
    s["_path"] = str(path)
    return s


def cmd_save(args) -> int:
    s = do_save(args.name, args.note, args.window)
    warns = collect_warnings(s)
    if args.json:
        print_json({"saved": summary(s), "path": s["_path"], "warnings": warns})
    else:
        k = counts(s)
        print(f"Saved snapshot {c(s['id'], '1')}" + (f" ({s['name']})" if s["name"] else "")
              + f": {k['tabs']} tabs, {k['claude']} Claude, {k['codex']} Codex.")
        print(c(f"File: {s['_path']}", "2"))
        for wmsg in warns:
            print(c(f"! {wmsg}", "33"))
    return 0


def cmd_list(args) -> int:
    snaps = list_snapshots()
    if args.json:
        print_json([summary(s, i) for i, s in enumerate(snaps, 1)])
    else:
        print_list(snaps)
    return 0


def cmd_show(args) -> int:
    s = find_snapshot(args.ref)
    if args.json:
        print_json({k: v for k, v in s.items() if not k.startswith("_")})
    else:
        print(c(f"Snapshot {s['id']}", "1") + (f"  {s['name']}" if s.get("name") else "")
              + c(f"  saved {fmt_time(s.get('captured_at', ''))}", "2"))
        if s.get("note"):
            print(c(f"Note: {s['note']}", "2"))
        print_state(s)
    return 0


def do_restore(s: dict, opts, log) -> dict:
    running = {} if opts.force else running_agents()
    plan = build_plan(s, opts, running)
    if not plan["windows"]:
        raise GsError("Nothing to restore: the tab selection is empty.", EXIT_USAGE)
    if opts.dry_run:
        out = {"dry_run": True, "plan": plan}
        if opts.close_idle and ghostty_running():
            out["would_close_windows"] = idle_windows(set())
        return out
    res = execute_plan(plan, opts, log)
    new_windows = {w["window_id"] for w in res["windows"] if w["window_id"]}
    out = {"dry_run": False, "snapshot": s.get("id"), "result": res, "plan": plan}
    if opts.close_idle:
        time.sleep(0.5)
        out["close_idle"] = close_idle_windows(new_windows, log)
    return out


def cmd_restore(args) -> int:
    s = find_snapshot(args.ref)
    quiet = args.json
    log = (lambda m: None) if quiet else print
    if not quiet and not args.dry_run:
        print(f"Restoring snapshot {c(s['id'], '1')}" + (f" ({s['name']})" if s.get("name") else "") + " ...")
    out = do_restore(s, args, log)
    if args.json:
        print_json(out)
        return 0
    if args.dry_run:
        print(c(f"Dry run of snapshot {s['id']}: nothing was opened.", "1"))
        print_plan(out["plan"])
        if "would_close_windows" in out:
            wc = out["would_close_windows"]
            print(f"--close-idle would close {len(wc)} window(s) that only have idle shells"
                  + (": " + ", ".join(short(w["title"], 30) for w in wc) if wc else "."))
        return 0
    for wmsg in out["result"]["warnings"]:
        print(c(f"! {wmsg}", "33"))
    print("Done.")
    return 0


def cmd_delete(args) -> int:
    s = find_snapshot(args.ref)
    if sys.stdin.isatty() and not args.yes and not args.json:
        ans = input(f"Delete snapshot {s['id']} {s.get('name', '')}? [y/N] ").strip().lower()
        if ans not in ("y", "yes", "s", "si", "sí"):
            print("Not deleted.")
            return 0
    os.remove(s["_path"])
    if args.json:
        print_json({"deleted": summary(s)})
    else:
        print(f"Deleted snapshot {s['id']}.")
    return 0


def cmd_rename(args) -> int:
    s = find_snapshot(args.ref)
    s["name"] = args.name
    if args.note is not None:
        s["note"] = args.note
    write_snapshot(s)
    if args.json:
        print_json({"renamed": summary(s)})
    else:
        print(f"Snapshot {s['id']} is now named '{args.name}'.")
    return 0


def cmd_prune(args) -> int:
    snaps = list_snapshots()
    drop = snaps[args.keep:]
    for s in drop:
        os.remove(s["_path"])
    if args.json:
        print_json({"deleted": [summary(s) for s in drop], "kept": min(len(snaps), args.keep)})
    else:
        print(f"Deleted {len(drop)} snapshot(s); kept {min(len(snaps), args.keep)}.")
    return 0


def cmd_doctor(args) -> int:
    checks = []

    def add(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    add("macOS", sys.platform == "darwin", sys.platform)
    add("osascript", shutil.which("osascript"), shutil.which("osascript") or "missing")
    running = ghostty_running()
    add("Ghostty running", running)
    if running:
        try:
            g = read_ghostty()
            add("Ghostty AppleScript", True, f"version {g['version']}, {len(g['windows'])} window(s)")
        except GsError as e:
            add("Ghostty AppleScript", False, str(e))
    add("claude on PATH", shutil.which("claude"), shutil.which("claude") or "missing")
    add("codex on PATH", shutil.which("codex"), shutil.which("codex") or "missing (only needed for Codex tabs)")
    add("Claude sessions folder", (claude_dir() / "sessions").is_dir(), str(claude_dir() / "sessions"))
    dbs = list(codex_dir().glob("state_*.sqlite"))
    add("Codex state database", bool(dbs), str(dbs[-1]) if dbs else "missing (only needed for Codex tabs)")
    add("Snapshot folder", True, str(snap_dir()))
    if args.json:
        print_json(checks)
    else:
        for ch in checks:
            mark = c("ok ", "32") if ch["ok"] else c("NO ", "31")
            print(f"{mark} {ch['check']:<24} {ch['detail']}")
    return 0 if all(ch["ok"] for ch in checks if ch["check"] not in ("codex on PATH", "Codex state database")) else 1


# --------------------------------------------------------------------------
# Interactive menu
# --------------------------------------------------------------------------

MENU = (
    "\n  s        save the current state      i        inspect the current state"
    "\n  r [N]    restore snapshot N          v N      view snapshot N"
    "\n  d N      delete snapshot N           n N      rename snapshot N"
    "\n  q        quit"
)


def ask(prompt: str, default: str = "") -> str:
    try:
        ans = input(prompt).strip()
    except EOFError:
        return default
    return ans or default


def yes(prompt: str, default: bool) -> bool:
    ans = ask(prompt + (" [Y/n] " if default else " [y/N] "), "y" if default else "n").lower()
    return ans in ("y", "yes", "s", "si", "sí")


class Opts:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def interactive() -> int:
    print(c(f"ghostsnap {__version__}", "1") + c("  save and restore Ghostty tabs and agent sessions", "2"))
    while True:
        print()
        snaps = list_snapshots()
        print_list(snaps)
        print(c(MENU, "2"))
        try:
            line = input(c("> ", "1")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        cmd, _, arg = line.partition(" ")
        arg = arg.strip()
        try:
            if cmd in ("q", "quit", "exit"):
                return 0
            if cmd in ("i", "inspect"):
                s = capture()
                print_state(s)
                for wmsg in collect_warnings(s):
                    print(c(f"! {wmsg}", "33"))
            elif cmd in ("s", "save"):
                s = capture()
                print_state(s)
                for wmsg in collect_warnings(s):
                    print(c(f"! {wmsg}", "33"))
                name = ask("Name for this snapshot (optional): ")
                s["id"] = new_snapshot_id()
                s["name"], s["note"] = name, ""
                path = write_snapshot(s)
                print(c(f"Saved {s['id']} in {path}", "32"))
            elif cmd in ("v", "view", "show"):
                s = find_snapshot(arg or "1")
                print(c(f"Snapshot {s['id']}", "1") + (f"  {s['name']}" if s.get("name") else ""))
                print_state(s)
            elif cmd in ("d", "delete", "rm"):
                s = find_snapshot(arg)
                if yes(f"Delete snapshot {s['id']} {s.get('name', '')}?", False):
                    os.remove(s["_path"])
                    print("Deleted.")
            elif cmd in ("n", "rename"):
                s = find_snapshot(arg)
                s["name"] = ask("New name: ", s.get("name", ""))
                write_snapshot(s)
                print("Renamed.")
            elif cmd in ("r", "restore"):
                s = find_snapshot(arg or "1")
                opts = Opts(no_agents=False, force=False, tabs=None, dry_run=True,
                            window="new", stagger=1.0, close_idle=False)
                pre = do_restore(s, opts, print)
                print(c(f"Plan for snapshot {s['id']}:", "1"))
                print_plan(pre["plan"])
                if not yes("Open these tabs in a new Ghostty window?", True):
                    continue
                opts.dry_run = False
                idle = idle_windows(set()) if ghostty_running() else []
                opts.close_idle = bool(idle) and yes(
                    f"After that, close {len(idle)} other window(s) where every tab only shows "
                    "an idle shell (for example the tabs Ghostty reopened after a reboot)?", False)
                out = do_restore(s, opts, print)
                for wmsg in out["result"]["warnings"]:
                    print(c(f"! {wmsg}", "33"))
                print(c("Done.", "32"))
                if out.get("close_idle", {}).get("closes_current_tab"):
                    return 0
            else:
                print(f"Unknown command '{cmd}'.")
        except GsError as e:
            print(c(f"Error: {e}", "31"))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print JSON (for scripts and AI agents)")

    p = argparse.ArgumentParser(
        prog="ghostsnap",
        description="Save and restore Ghostty windows, tabs, titles, directories, "
                    "and the Claude Code / Codex sessions that run in them. "
                    "Run it with no command for an interactive menu.",
        parents=[common],
    )
    p.add_argument("--version", action="version", version=f"ghostsnap {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    sp = sub.add_parser("inspect", parents=[common], help="show the current Ghostty state (does not save)")
    sp.add_argument("--window", help="only this window: a window id or 'front'")
    sp.set_defaults(func=cmd_inspect)

    sp = sub.add_parser("save", parents=[common], help="save the current state as a new snapshot")
    sp.add_argument("-n", "--name", default="", help="a short name for the snapshot")
    sp.add_argument("--note", default="", help="a free text note")
    sp.add_argument("--window", help="only this window: a window id or 'front'")
    sp.set_defaults(func=cmd_save)

    sp = sub.add_parser("list", aliases=["ls"], parents=[common], help="list snapshots, newest first")
    sp.set_defaults(func=cmd_list)

    ref_help = "snapshot: 'latest' (default), list number (1 = newest), id, id prefix or name"
    sp = sub.add_parser("show", parents=[common], help="show the tabs in a snapshot")
    sp.add_argument("ref", nargs="?", default="latest", help=ref_help)
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("restore", parents=[common], help="open the tabs of a snapshot and resume its agents")
    sp.add_argument("ref", nargs="?", default="latest", help=ref_help)
    sp.add_argument("--dry-run", action="store_true", help="print the plan; open nothing")
    sp.add_argument("--window", choices=["new", "front"], default="new",
                    help="open tabs in new windows (default) or add them to the front window")
    sp.add_argument("--tabs", help="only these tab numbers, for example 1,3,5-7 (numbers from `show`)")
    sp.add_argument("--no-agents", action="store_true", help="open tabs and directories, but start no agents")
    sp.add_argument("--force", action="store_true", help="start agents even if the same session already runs")
    sp.add_argument("--stagger", type=float, default=1.0, help="seconds to wait after a tab that starts an agent (default 1)")
    sp.add_argument("--close-idle", action="store_true",
                    help="after the restore, close other windows where every tab only shows an idle shell")
    sp.set_defaults(func=cmd_restore)

    sp = sub.add_parser("delete", aliases=["rm"], parents=[common], help="delete a snapshot")
    sp.add_argument("ref", help=ref_help)
    sp.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    sp.set_defaults(func=cmd_delete)

    sp = sub.add_parser("rename", parents=[common], help="rename a snapshot")
    sp.add_argument("ref", help=ref_help)
    sp.add_argument("name", help="the new name")
    sp.add_argument("--note", help="also replace the note")
    sp.set_defaults(func=cmd_rename)

    sp = sub.add_parser("prune", parents=[common], help="delete old snapshots")
    sp.add_argument("--keep", type=int, default=10, help="how many of the newest to keep (default 10)")
    sp.set_defaults(func=cmd_prune)

    sp = sub.add_parser("doctor", parents=[common], help="check that ghostsnap can work on this Mac")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("path", parents=[common], help="print the snapshot folder")
    sp.set_defaults(func=lambda a: (print_json({"path": str(snap_dir())}) if a.json else print(snap_dir())) or 0)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if not args.cmd:
            if sys.stdin.isatty() and sys.stdout.isatty() and not args.json:
                return interactive()
            args.func = cmd_list
        return args.func(args)
    except GsError as e:
        if getattr(args, "json", False):
            print_json({"error": str(e), "code": e.code})
        else:
            print(f"ghostsnap: {e}", file=sys.stderr)
        return e.code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
