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

    def test_the_schedule_check_says_go_only_when_cindy_does_items_and_one_waits(self):
        args = ["gate", "--config", str(self.root / "config.yaml")]
        (self.root / "config.yaml").write_text("{}", encoding="utf-8")
        with patch.object(drive, "install_shim"), patch.object(drive, "has_items_for", return_value=True), redirect_stdout(io.StringIO()):
            self.assertEqual(drive.main(args), 2)              # items are done locally: nothing for Cindy
            set_setting(self.root, "engine", "cindy")
            self.assertEqual(drive.main(args), 0)
        with patch.object(drive, "install_shim"), patch.object(drive, "has_items_for", return_value=False), redirect_stdout(io.StringIO()):
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
