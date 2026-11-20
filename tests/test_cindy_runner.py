"""Items done by Cindy: the built-in driver only sorts, Cindy's schedule asks first (no work = no tokens), and Cindy is
told it works in the owner's own project, not on 心响."""
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from agent_matrix import drive, local_sessions as ls
from agent_matrix.thread import set_setting


class CindyRunner(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "state").mkdir()

    def test_with_cindy_doing_items_the_builtin_driver_only_takes_sorting_work(self):
        set_setting(self.root, "engine", "cindy")
        self.assertEqual(drive.item_runner(self.root), "cindy")
        seen = []
        stub = lambda name: (lambda root, config, driver, **k: seen.append(name) or None)
        with patch.object(drive, "_prepare_triage", stub("triage")), patch.object(drive, "_prepare_meeting", stub("meeting")), \
             patch.object(drive, "_prepare_chat", stub("chat")), patch.object(drive, "_prepare_learn", stub("learn")), \
             patch.object(drive, "_prepare_item", stub("item")):
            drive.next_job(self.root, None, "本机 GPT", kinds=drive.SORTING)
            self.assertEqual(seen, ["triage", "meeting", "chat", "learn"])
            seen.clear()
            drive.next_job(self.root, None, "Cindy", kinds=["item"])
            self.assertEqual(seen, ["item"])

    def test_the_owner_decides_what_cindy_takes_and_the_builtin_driver_takes_the_rest(self):
        self.assertEqual((drive.cindy_kinds(self.root), drive.local_kinds(self.root)), ([], [*drive.SORTING, "item"]))
        set_setting(self.root, "engine_sort", "cindy")                      # Cindy sorts, a local AI does the items
        self.assertEqual((drive.cindy_kinds(self.root), drive.local_kinds(self.root)), ([*drive.SORTING], ["item"]))
        set_setting(self.root, "engine", "cindy")                           # Cindy does everything
        self.assertEqual((drive.cindy_kinds(self.root), drive.local_kinds(self.root)), ([*drive.SORTING, "item"], ["item"]))  # items handed to a local AI one by one
        seen = []
        stub = lambda name: (lambda root, config, driver, **k: seen.append(name) or None)
        with patch.object(drive, "_prepare_triage", stub("triage")), patch.object(drive, "_prepare_meeting", stub("meeting")), \
             patch.object(drive, "_prepare_chat", stub("chat")), patch.object(drive, "_prepare_learn", stub("learn")), \
             patch.object(drive, "_prepare_item", stub("item")):
            drive.next_job(self.root, None, "Cindy", kinds=["item"])        # an older schedule still asks for items only
            self.assertEqual(seen, ["triage", "meeting", "chat", "learn", "item"])
            seen.clear()
            set_setting(self.root, "engine", "gemini")
            set_setting(self.root, "engine_sort", "gemini")
            self.assertIsNone(drive.next_job(self.root, None, "Cindy"))     # nothing was handed to Cindy: no sorting,
            self.assertEqual(seen, ["item"])                                 # only items handed to it one by one
        args = ["gate", "--config", str(self.root / "config.yaml")]
        (self.root / "config.yaml").write_text("{}", encoding="utf-8")
        set_setting(self.root, "engine_sort", "cindy")
        with patch.object(drive, "install_shim"), patch.object(drive, "has_sorting_for", return_value="chat"), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(drive.main(args), 0)                           # a round of chats waits: Cindy is let in
        self.assertIn('"work": "chat"', out.getvalue())
        with patch.object(drive, "install_shim"), patch.object(drive, "has_sorting_for", return_value=None), redirect_stdout(io.StringIO()):
            self.assertEqual(drive.main(args), 2)

    def test_whichever_ai_says_hello_takes_the_work_and_the_next_one_takes_it_over(self):
        from agent_matrix import aidrivers
        seen = []
        stub = lambda name: (lambda root, config, driver, **k: seen.append(name) or None)
        with patch.object(drive, "_prepare_triage", stub("triage")), patch.object(drive, "_prepare_meeting", stub("meeting")), \
             patch.object(drive, "_prepare_chat", stub("chat")), patch.object(drive, "_prepare_learn", stub("learn")), \
             patch.object(drive, "_prepare_item", stub("item")):
            drive.next_job(self.root, None, "某个老脚本")                   # never said hello: as before, it may take anything
            self.assertEqual(seen, ["triage", "meeting", "chat", "learn", "item"])
            seen.clear()
            aidrivers.hello(self.root, "Antigravity", app="Antigravity", model="gemini-3")
            self.assertEqual((drive.item_runner(self.root), drive.sort_runner(self.root), drive.local_kinds(self.root)), ("driver", "driver", ["item"]))
            self.assertEqual(drive.kinds_for(self.root, "Antigravity"), [*drive.SORTING, "item"])
            drive.next_job(self.root, None, "Antigravity")
            self.assertEqual(seen, ["triage", "meeting", "chat", "learn", "item"])
            seen.clear()
            aidrivers.hello(self.root, "Claude Code", model="opus")          # the message went to another AI: it takes over
            self.assertIsNone(drive.next_job(self.root, None, "Antigravity"))
            self.assertEqual(seen, ["item"])                                 # Antigravity: only items handed to it one by one
            seen.clear()
            drive.next_job(self.root, None, "Claude Code")
            self.assertEqual(seen, ["triage", "meeting", "chat", "learn", "item"])
            self.assertEqual(drive.kinds_for(self.root, "Cindy"), ["item"])
        names = {d["name"]: d for d in aidrivers.known(self.root)}
        self.assertEqual(set(names), {"Antigravity", "Claude Code"})
        self.assertEqual((names["Antigravity"]["model"], names["Antigravity"]["online"]), ("gemini-3", True))
        with self.assertRaises(ValueError):
            aidrivers.hello(self.root, "  ")
        (self.root / "config.yaml").write_text("{}", encoding="utf-8")
        with patch.object(drive, "install_shim"), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(drive.main(["hello", "--name", "Gemini", "--app", "Gemini CLI", "--model", "gemini-2.5-pro", "--config", str(self.root / "config.yaml")]), 0)
        self.assertIn('"you": "Gemini"', out.getvalue()); self.assertIn("--wait 600", out.getvalue())
        self.assertEqual(drive.kinds_for(self.root, "Gemini"), [*drive.SORTING, "item"])

    def test_gemini_is_a_local_ai_like_the_others(self):
        import json
        from agent_matrix import engines
        from agent_matrix.models import Config
        self.assertIs(engines.engine(self.root), engines.CodexRun)
        set_setting(self.root, "engine", "gemini")
        self.assertIs(engines.engine(self.root), engines.GeminiRun)
        self.assertIs(engines.engine(self.root, "sort"), engines.GeminiRun)
        set_setting(self.root, "engine_sort", "cindy")                      # what the built-in driver still does uses the work AI
        self.assertIs(engines.engine(self.root, "sort"), engines.GeminiRun)
        config = Config(worker_gemini="/x/gemini", worker_gemini_model="gemini-pro", worker_gemini_api_key="k-123")
        run = engines.GeminiRun(config, self.root)
        argv = run.argv()
        self.assertEqual((argv[0], argv[-2:]), ("/x/gemini", ["-p", ""]))
        self.assertIn("stream-json", argv); self.assertIn("gemini-pro", argv); self.assertNotIn("--resume", argv)
        self.assertEqual((run.env()["GEMINI_API_KEY"], run.env()["GEMINI_CLI_TRUST_WORKSPACE"]), ("k-123", "true"))
        for event in ({"type": "init", "session_id": "s-1", "model": "m"}, {"type": "message", "role": "user", "content": "任务"},
                      {"type": "message", "role": "assistant", "content": "先看", "delta": True},
                      {"type": "tool_use", "tool_name": "run_shell_command", "tool_id": "t", "parameters": {}},
                      {"type": "tool_result", "tool_id": "t", "status": "success"},
                      {"type": "message", "role": "assistant", "content": "做", "delta": True},
                      {"type": "message", "role": "assistant", "content": "完了", "delta": True},
                      {"type": "result", "status": "success", "stats": {"input_tokens": 10, "output_tokens": 4, "cached": 1}}):
            run._event(json.dumps(event, ensure_ascii=False), None)
        run._event("not json", None)
        self.assertEqual((run.thread, run.commands, run.messages, run.usage["input_tokens"]), ("s-1", 1, 2, 10))
        self.assertEqual((self.root / ".last.txt").read_text(encoding="utf-8"), "做完了")       # its last message is the answer
        self.assertEqual((self.root / ".gemini-session").read_text(encoding="utf-8"), "s-1")
        self.assertIn("--resume", engines.GeminiRun(config, self.root, thread="s-1").argv())
        self.assertNotIn("GEMINI_API_KEY", engines.GeminiRun(Config(worker_gemini="/x/gemini"), self.root).env())

    def test_the_schedule_check_says_go_only_when_cindy_does_items_and_one_waits(self):
        args = ["gate", "--config", str(self.root / "config.yaml")]
        (self.root / "config.yaml").write_text("{}", encoding="utf-8")
        from agent_matrix import aidrivers
        waiting = (0, {"ready": [{"record_id": "rA", "status": "queued"}, {"record_id": "rB", "status": "queued"}]})
        with patch.object(drive, "install_shim"), patch("agent_matrix.workenv._check", return_value=waiting), redirect_stdout(io.StringIO()):
            self.assertEqual(drive.main(args), 2)              # items are done locally: nothing for Cindy
            aidrivers.assign(self.root, "rB", "cindy")
            self.assertEqual(drive.main(args), 0)              # but this one was handed to Cindy
            aidrivers.assign(self.root, "rB", "")
            set_setting(self.root, "engine", "cindy")
            self.assertEqual(drive.main(args), 0)
        with patch.object(drive, "install_shim"), patch("agent_matrix.workenv._check", return_value=(0, {"ready": []})), redirect_stdout(io.StringIO()):
            self.assertEqual(drive.main(args), 2)              # nothing waiting: skip the round, no tokens

    def test_cindy_is_told_which_project_it_works_in(self):
        set_setting(self.root, "cindy_project", "/Users/x/Documents/Studio")
        head = drive.cindy_header(self.root, {"n": 65, "title": "标签更新"})
        self.assertIn("「Studio」项目", head)
        self.assertIn("不是你要做需求的产品", head)
        self.assertIn("心响 #65 标签更新", head)

    def test_projects_and_the_pickup_schedule_come_from_cindys_records(self):
        home = self.root / "home"
        base = home / "Library/Application Support/Cindy"
        base.mkdir(parents=True)
        with closing(sqlite3.connect(base / "cindy-test.db")) as db, db:
            db.execute("CREATE TABLE sessions(id, project_dir, working_dir, hidden_at)")
            rows = [("a", "/Users/x/Documents/Studio", None, None)] * 3 + [("b", None, "/Users/x/Desktop/Notes", None)] \
                + [("c", None, "/Users/x/Library/Application Support/Cindy/owners/o/bots/b1/workspace", None)]
            db.executemany("INSERT INTO sessions VALUES (?,?,?,?)", rows)
            db.execute("CREATE TABLE schedules(id, name, status, cron_expr, working_dir, pre_run_hook_command, last_fired_at, last_finished_at, agent_kind, updated_at)")
            db.execute("INSERT INTO schedules VALUES (?,?,?,?,?,?,?,?,?,?)", ("s1", ls.SCHEDULE_NAME, "active", "*/5 * * * *",
                       "/Users/x/Documents/Studio", "node hook.mjs # runs xinxiang gate", int(time.time() * 1000), None, "codex", 1))
        self.assertEqual([p["name"] for p in ls.cindy_projects(home)], ["Studio", "Notes"])   # bots left out
        sch = ls.cindy_schedule(home)
        self.assertEqual((sch["status"], sch["hooked"], sch["folder"]), ("active", True, "/Users/x/Documents/Studio"))

    def test_a_hook_through_the_projects_own_check_script_is_known_by_the_gates_answer(self):
        home = self.root / "home"
        base = home / "Library/Application Support/Cindy"
        base.mkdir(parents=True)
        with closing(sqlite3.connect(base / "cindy-test.db")) as db, db:
            db.execute("CREATE TABLE schedules(id, name, status, cron_expr, working_dir, pre_run_hook_command, last_fired_at, last_finished_at, agent_kind, updated_at)")
            db.execute("INSERT INTO schedules VALUES (?,?,?,?,?,?,?,?,?,?)", ("s1", ls.SCHEDULE_NAME, "active", "*/5 * * * *",
                       "/Users/x/Documents/Studio", "node '/Users/x/Documents/Studio/scripts/checks/check.mjs'", None, None, "codex", 1))
            db.execute("CREATE TABLE schedule_runs(id, schedule_id, fired_at, status, pre_run_hook_result)")
        self.assertIsNone(ls.cindy_schedule(home)["hooked"])            # not run yet: not known
        with closing(sqlite3.connect(base / "cindy-test.db")) as db, db:
            out = '{"exitCode":2,"stdout":"{\\"ok\\": true, \\"work\\": null}\\n"}'
            db.execute("INSERT INTO schedule_runs VALUES (?,?,?,?,?)", ("r1", "s1", int(time.time() * 1000), "skipped", out))
        sch = ls.cindy_schedule(home)
        self.assertEqual((sch["hooked"], sch["last_check"]["result"]), (True, "没活，跳过"))
        with closing(sqlite3.connect(base / "cindy-test.db")) as db, db:   # a new round has just begun: its check has not answered yet
            db.execute("INSERT INTO schedule_runs VALUES (?,?,?,?,?)", ("r2", "s1", int(time.time() * 1000) + 5, "running", None))
        sch = ls.cindy_schedule(home)
        self.assertEqual((sch["hooked"], sch["last_check"]["result"]), (True, "正在做"))   # still set up; not "missing its check"
        with closing(sqlite3.connect(base / "cindy-test.db")) as db, db:
            db.execute("UPDATE schedules SET pre_run_hook_command=NULL")
        self.assertFalse(ls.cindy_schedule(home)["hooked"])


if __name__ == "__main__":
    unittest.main()
