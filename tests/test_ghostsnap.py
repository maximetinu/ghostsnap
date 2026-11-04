"""Unit tests for ghostsnap. They do not need Ghostty, Claude Code or Codex."""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ghostsnap as g  # noqa: E402


class Opts:
    def __init__(self, **kw):
        self.no_agents = False
        self.force = False
        self.tabs = None
        self.__dict__.update(kw)


class TempEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old = {k: os.environ.get(k) for k in ("GHOSTSNAP_DIR", "CLAUDE_CONFIG_DIR", "CODEX_HOME")}
        os.environ["GHOSTSNAP_DIR"] = str(self.root / "state")
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.root / "claude")
        os.environ["CODEX_HOME"] = str(self.root / "codex")

    def tearDown(self):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()


class ParsingTests(unittest.TestCase):
    def test_parse_ps(self):
        out = (
            "16234 16014 16234 16234 S+   ttys011  Wed Nov  4 22:18:27 2026     claude\n"
            "16014 16012 16014 16234 S    ttys011  Wed Nov  4 22:18:20 2026     -/bin/zsh\n"
            "  101     1   101     0 Ss   ??       Wed Nov  4 10:00:00 2026     /Applications/My App.app/x\n"
        )
        procs = g.parse_ps(out)
        self.assertEqual(procs[16234]["comm"], "claude")
        self.assertEqual(procs[16234]["tty"], "ttys011")
        self.assertEqual(procs[16234]["lstart"], "Wed Nov 4 22:18:27 2026")
        self.assertEqual(procs[101]["comm"], "/Applications/My App.app/x")
        self.assertTrue(g.is_shell(procs[16014]["comm"]))

    def test_parse_procargs2(self):
        argv = [b"claude", b"-n", b"my name", b"# Task:\nmulti line"]
        data = (len(argv)).to_bytes(4, sys.byteorder) + b"/usr/bin/claude\0\0\0" + b"\0".join(argv) + b"\0PATH=/bin\0"
        exec_path, parsed = g.parse_procargs2(data)
        self.assertEqual(exec_path, "/usr/bin/claude")
        self.assertEqual(parsed, ["claude", "-n", "my name", "# Task:\nmulti line"])

    def test_agent_kind(self):
        self.assertEqual(g.agent_kind("/x/claude", ["claude", "-n", "a"])[0], "claude")
        self.assertEqual(g.agent_kind("/Users/u/.local/share/claude/versions/2.1.3", ["2.1.3"])[0], "claude")
        self.assertEqual(g.agent_kind("/x/codex", ["codex", "resume", "id"]), ("codex", ["resume", "id"]))
        self.assertEqual(g.agent_kind("/x/node", ["node", "/lib/node_modules/@anthropic-ai/claude-code/cli.js", "-c"])[0], "claude")
        self.assertEqual(g.agent_kind("/x/node", ["node", "/lib/node_modules/@openai/codex/bin/codex.js"])[0], "codex")
        self.assertEqual(g.agent_kind("/bin/zsh", ["-zsh"]), (None, []))

    def test_claude_flags(self):
        args = ["-w", "robot-body", "-n", "robot-body", "--model", "opus",
                "--add-dir", "/a", "/b", "--allowedTools", "Read,Bash(x:*)",
                "--dangerously-skip-permissions", "--permission-mode=plan",
                "--resume", "abc", "# Task: --chrome inside a prompt"]
        flags = g.filter_flags(args, g.CLAUDE_KEEP_VALUE, g.CLAUDE_KEEP_MULTI, g.CLAUDE_KEEP_BOOL)
        self.assertEqual(flags, ["--model", "opus", "--add-dir", "/a", "/b",
                                 "--allowedTools", "Read,Bash(x:*)",
                                 "--dangerously-skip-permissions", "--permission-mode=plan"])

    def test_claude_interactive(self):
        self.assertTrue(g.claude_interactive([]))
        self.assertTrue(g.claude_interactive(["-n", "x", "do the thing"]))
        self.assertFalse(g.claude_interactive(["-p", "hi"]))
        self.assertFalse(g.claude_interactive(["mcp", "list"]))
        self.assertFalse(g.claude_interactive(["rc"]))

    def test_codex_mode_and_flags(self):
        self.assertEqual(g.codex_mode([]), (True, None))
        self.assertEqual(g.codex_mode(["-m", "gpt-5", "resume", "0199-abc"]), (True, "0199-abc"))
        self.assertEqual(g.codex_mode(["resume", "--last"]), (True, None))
        self.assertEqual(g.codex_mode(["fork", "x"]), (True, None))
        self.assertEqual(g.codex_mode(["exec", "hi"]), (False, None))
        self.assertEqual(g.codex_mode(["app-server", "--listen", "stdio://"]), (False, None))
        self.assertEqual(g.codex_mode(["fix the bug"]), (True, None))
        flags = g.filter_flags(["-m", "gpt-5", "-c", "a=1", "--search", "-i", "x.png", "resume", "id"],
                               g.CODEX_KEEP_VALUE, set(), g.CODEX_KEEP_BOOL)
        self.assertEqual(flags, ["-m", "gpt-5", "-c", "a=1", "--search"])

    def test_title_prefix(self):
        self.assertEqual(g.title_id_prefix("grisalla · Continúa · 01a19958-5685-77"), "01a19958-5685-77")
        self.assertEqual(g.title_id_prefix("sage · x · e3e6b705-79e1-44"), "e3e6b705-79e1-44")
        self.assertIsNone(g.title_id_prefix("~/git/smartd"))

    def test_sanitize_and_lstart(self):
        self.assertEqual(g.sanitize_path("/Users/u/git/sage/.claude/worktrees/x"),
                         "-Users-u-git-sage--claude-worktrees-x")
        self.assertTrue(g.same_lstart("Wed Nov  4 22:18:27 2026", "Wed Nov 4 22:18:27 2026"))
        self.assertTrue(g.same_lstart("Wed Nov  4 22:18:27 2026", "Wed Nov 4 23:18:27 2026"))  # UTC vs CET
        self.assertFalse(g.same_lstart("Wed Nov  4 22:18:27 2026", "Wed Nov 4 22:18:28 2026"))
        self.assertFalse(g.same_lstart("Wed Nov  4 22:18:27 2026", "Thu Nov 5 22:18:27 2026"))

    def test_parse_tab_list(self):
        self.assertEqual(g.parse_tab_list("1,3,5-7"), {1, 3, 5, 6, 7})
        self.assertIsNone(g.parse_tab_list(None))


class ClaudeTests(TempEnv):
    def test_resume_dir_from_transcript_folder(self):
        wt = "/Users/u/git/sage/.claude/worktrees/robot"
        proj = self.root / "claude" / "projects" / g.sanitize_path(wt)
        proj.mkdir(parents=True)
        (proj / "sid-1.jsonl").write_text(json.dumps({"cwd": wt}) + "\n")
        rdir, transcript = g.claude_resume_dir("sid-1", ["/Users/u/git/sage", wt])
        self.assertEqual(rdir, wt)
        self.assertTrue(transcript.endswith("sid-1.jsonl"))
        self.assertEqual(g.claude_resume_dir("missing", [wt]), (None, None))

    def test_resume_dir_from_transcript_cwd(self):
        real = "/Users/u/other place"
        proj = self.root / "claude" / "projects" / g.sanitize_path(real)
        proj.mkdir(parents=True)
        (proj / "sid-2.jsonl").write_text('{"type":"x"}\n' + json.dumps({"cwd": real}) + "\n")
        rdir, _ = g.claude_resume_dir("sid-2", ["/nope"])
        self.assertEqual(rdir, real)

    def test_session_file_stale_pid(self):
        d = self.root / "claude" / "sessions"
        d.mkdir(parents=True)
        (d / "42.json").write_text(json.dumps({"pid": 42, "sessionId": "s", "procStart": "Wed Nov  4 22:18:27 2026"}))
        self.assertIsNotNone(g.claude_session_file(42, {"lstart": "Wed Nov 4 23:18:27 2026"}))
        self.assertIsNone(g.claude_session_file(42, {"lstart": "Thu Nov 5 09:00:00 2026"}))


class CodexTests(TempEnv):
    def make_db(self, rows):
        d = self.root / "codex"
        d.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(d / "state_5.sqlite")
        con.execute("create table threads (id text, cwd text, title text, name text, updated_at int,"
                    " created_at int, thread_source text, source text, archived int, first_user_message text)")
        con.executemany("insert into threads values (?,?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
        con.close()

    def test_assign(self):
        now = int(time.time())
        self.make_db([
            ("01a1-0000-7000-0000-000000000001", "/w/a", "Old", None, now - 9000, now - 9000, "user", "cli", 0, ""),
            ("01a1-0000-7000-0000-000000000002", "/w/a", "New", None, now - 10, now - 100, "user", "cli", 0, ""),
            ("01a1-0000-7000-0000-000000000003", "/w/a", "Sub", None, now, now - 50, "subagent", "{}", 0, ""),
            ("01a19958-5685-77e0-8ef1-b76aaddf659b", "/w/b", "Title match", None, now - 5000, now - 5000, "user", "cli", 0, ""),
        ])
        agents = [
            {"kind": "codex", "cwd": "/w/a", "start": now - 200, "terminal_title": "", "resume_ref": None},
            {"kind": "codex", "cwd": "/w/b", "start": now - 200, "terminal_title": "b · x · 01a19958-5685-77",
             "resume_ref": None},
            {"kind": "codex", "cwd": "/w/c", "start": now, "terminal_title": "",
             "resume_ref": "01a1-0000-7000-0000-000000000001"},
        ]
        g.assign_codex_threads(agents)
        self.assertEqual(agents[0]["session_id"], "01a1-0000-7000-0000-000000000002")
        self.assertEqual(agents[0]["resolved_by"], "heuristic-cwd")
        self.assertEqual(agents[1]["session_id"], "01a19958-5685-77e0-8ef1-b76aaddf659b")
        self.assertEqual(agents[1]["resolved_by"], "terminal-title")
        self.assertEqual(agents[2]["session_id"], "01a1-0000-7000-0000-000000000001")
        self.assertEqual(agents[2]["resolved_by"], "argv")


class PlanTests(TempEnv):
    def setUp(self):
        super().setUp()
        self.shell_dir = self.root / "repo"
        self.wt = self.root / "repo" / ".claude" / "worktrees" / "x"
        self.wt.mkdir(parents=True)
        proj = self.root / "claude" / "projects" / g.sanitize_path(str(self.wt))
        proj.mkdir(parents=True)
        (proj / "sid-wt.jsonl").write_text("{}\n")

    def snapshot(self):
        return {"id": "t", "windows": [{"index": 1, "front": True, "tabs": [
            {"index": 1, "title": "x", "title_override": "ROBOT", "selected": True, "terminals": [
                {"cwd": str(self.shell_dir), "title_override": "", "state": "agent", "agent": {
                    "kind": "claude", "pid": 1, "session_id": "sid-wt", "name": "robot-body",
                    "name_source": "user", "flags": ["--model", "opus"], "resume_dir": str(self.wt),
                    "cwd": str(self.wt)}}]},
            {"index": 2, "title": "t", "title_override": "", "selected": False, "terminals": [
                {"cwd": str(self.shell_dir), "title_override": "", "state": "agent", "agent": {
                    "kind": "codex", "pid": 2, "session_id": "th-1", "name": "", "flags": ["--search"],
                    "session_cwd": str(self.shell_dir), "cwd": str(self.shell_dir)}}]},
            {"index": 3, "title": "zsh", "title_override": "LOGS", "selected": False, "terminals": [
                {"cwd": "/does/not/exist", "title_override": "", "state": "idle", "agent": None}]},
        ]}]}

    def test_plan_commands(self):
        plan = g.build_plan(self.snapshot(), Opts(), {})
        tabs = plan["windows"][0]["tabs"]
        claude = tabs[0]["terminals"][0]
        self.assertEqual(claude["action"], "resume claude")
        self.assertEqual(
            claude["input"],
            f"cd {self.wt} && claude --model opus --resume sid-wt -n robot-body; cd {self.shell_dir}",
        )
        self.assertEqual(tabs[1]["terminals"][0]["input"], "codex resume --search th-1")
        self.assertEqual(tabs[2]["terminals"][0]["cwd"], os.path.expanduser("~"))
        self.assertTrue(tabs[2]["terminals"][0]["warnings"])

    def test_plan_skips_running_and_selects_tabs(self):
        plan = g.build_plan(self.snapshot(), Opts(tabs="1,3"), {("claude", "sid-wt"): 99})
        tabs = plan["windows"][0]["tabs"]
        self.assertEqual([t["number"] for t in tabs], [1, 3])
        self.assertEqual(tabs[0]["terminals"][0]["action"], "skip agent (already running)")
        self.assertEqual(tabs[0]["terminals"][0]["input"], "")
        forced = g.build_plan(self.snapshot(), Opts(force=True), {("claude", "sid-wt"): 99})
        self.assertEqual(forced["windows"][0]["tabs"][0]["terminals"][0]["action"], "resume claude")

    def test_missing_transcript_starts_new(self):
        s = self.snapshot()
        s["windows"][0]["tabs"][0]["terminals"][0]["agent"]["session_id"] = "gone"
        t = g.build_plan(s, Opts(), {})["windows"][0]["tabs"][0]["terminals"][0]
        self.assertEqual(t["action"], "start new claude")
        self.assertNotIn("--resume", t["input"])

    def test_no_agents(self):
        t = g.build_plan(self.snapshot(), Opts(no_agents=True), {})["windows"][0]["tabs"][0]["terminals"][0]
        self.assertEqual(t["input"], "")


class StorageTests(TempEnv):
    def test_save_find_delete(self):
        for i, name in enumerate(["a", "b", "c"]):
            g.write_snapshot({"id": f"2026010{i}-000000", "name": name,
                              "captured_at": f"2026-01-0{i + 1}T00:00:00+00:00", "windows": []})
        self.assertEqual(g.find_snapshot("latest")["name"], "c")
        self.assertEqual(g.find_snapshot("1")["name"], "c")
        self.assertEqual(g.find_snapshot("3")["name"], "a")
        self.assertEqual(g.find_snapshot("b")["id"], "20260101-000000")
        self.assertEqual(g.find_snapshot("20260100")["name"], "a")
        with self.assertRaises(g.GsError):
            g.find_snapshot("nope")
        mode = os.stat(g.snap_dir() / "20260100-000000.json").st_mode & 0o777
        self.assertEqual(mode, 0o600)
        self.assertEqual(g.main(["prune", "--keep", "1", "--json"]), 0)
        self.assertEqual(len(g.list_snapshots()), 1)

    def test_cli_list_json(self):
        g.write_snapshot({"id": "x", "name": "n", "captured_at": "2026-01-01T00:00:00+00:00", "windows": []})
        self.assertEqual(g.main(["list", "--json"]), 0)
        self.assertEqual(g.main(["show", "nope", "--json"]), g.EXIT_NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
