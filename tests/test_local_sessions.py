from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_matrix import local_sessions as ls, sessions
from agent_matrix.models import MatrixError


class LocalSessions(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.now = time.time()

    def test_discovery_cross_app_dedup_and_completed_turns(self):
        data = self.home / "Library/Application Support/Cindy"
        data.mkdir(parents=True)
        with closing(sqlite3.connect(data / "cindy-test.db")) as db, db:
            db.execute("CREATE TABLE sessions(id,title,active_turn_started_at,sdk_session_id,status,hidden_at,last_turn_ended_at,last_activity_at)")
            for sid, end in [("current", 0), ("finished", self.now*1000+5)]:
                db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)",(sid,"心响",self.now*1000,"underlying","active",None,end,self.now*1000))
        folder = self.home / ".claude/sessions"
        folder.mkdir(parents=True)
        for index, (sid, status) in enumerate([("underlying","busy"),("standalone","running"),("idle","idle")]):
            (folder / f"{index}.json").write_text(json.dumps(dict(pid=os.getpid(),sessionId=sid,status=status,updatedAt=self.now*1000)))
        records = ls.discover(self.home)
        # open conversations are listed whether replying or waiting; the Claude one Cindy drives is shown once, as Cindy
        self.assertEqual({k: v["working"] for k, v in records.items()},
                         {"cindy:current": True, "cindy:finished": False, "claude:standalone": True, "claude:idle": False})

    def test_codex_finished_or_aborted_are_waiting_not_replying(self):
        base = self.home / ".codex"
        folder = base / "sessions"
        folder.mkdir(parents=True)
        stamp = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(base/"state_5.sqlite")) as db, db:
            db.execute("CREATE TABLE threads(id,title,cwd,rollout_path,updated_at,archived,source)")
            for sid, events, source in [("working",["task_started"],"vscode"),("ended",["task_started","task_complete"],"cli"),
                                        ("aborted",["task_started","turn_aborted"],"vscode"),("script",["task_started"],"exec")]:
                path = folder/(sid+".jsonl")
                path.write_text("\n".join(json.dumps({"timestamp":stamp,"type":"event_msg","payload":{"type":ev}}) for ev in events))
                db.execute("INSERT INTO threads VALUES(?,?,?,?,?,0,?)",(sid,sid,str(folder),str(path),self.now,source))
        (base/"process_manager").mkdir()
        (base/"process_manager/chat_processes.json").write_text(json.dumps([dict(osPid=os.getpid(),updatedAtMs=self.now*1000,conversationId="ended")]))
        found = ls.discover(self.home)
        self.assertEqual({k: v["working"] for k, v in found.items()},
                         {"codex:working": True, "codex:ended": False, "codex:aborted": False})

    def test_unbound_then_verified_binding_preserves_original_session(self):
        rec = ls._record("cindy","exact-id","不像事项标题",self.now)
        key = rec["key"]
        known = [{"rid":"r6","n":6}]
        with patch.object(ls,"discover",return_value={key:rec}), patch("agent_matrix.snapshot._snapshot",return_value={"rows":[{"record_id":"r6","编号":6}]}):
            self.assertIsNone(ls.view(self.home,{},[],known)[0]["n"])
            self.assertEqual(ls.view(self.home,{"r6":[{"session":"exact-id"}]},[],known)[0]["n"],6)
            with self.assertRaises(MatrixError): ls.associate(self.home,key,7)
            ls.associate(self.home,key,6)
            self.assertEqual(ls.view(self.home,{},[],known)[0]["n"],6)
        hist = {}
        with patch.object(ls,"discover",return_value={}), patch.object(ls.subprocess,"run") as native:
            self.assertEqual(ls.view(self.home,hist,[],known),[])
            self.assertEqual(hist["r6"][0]["status"],"recorded")
            self.assertEqual(ls.open_session(self.home,None,hist["r6"][0]["job"]),{"opened":"cindy"})
            self.assertEqual(native.call_args.args[0], ["open","cindy://session/exact-id"])

    def test_latest_request_skips_tool_results_and_system_notes(self):
        folder = self.home / ".claude/projects/-Users-x-proj"
        folder.mkdir(parents=True)
        rows = [{"type": "user", "message": {"content": "第一句"}},
                {"type": "user", "message": {"content": [{"type": "text", "text": "<pasted_content id=\"a\">把 #62 的流程图补齐</pasted_content><system-reminder>内部提示</system-reminder>"}]}},
                *[{"type": "user", "message": {"content": [{"type": "tool_result", "content": "x" * 50}]}} for _ in range(30)]]
        (folder / "abcd-1234.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows))
        self.assertEqual(ls._last_request(self.home, "abcd-1234"), "把 #62 的流程图补齐")

    def test_a_conversation_about_one_item_binds_itself_and_can_be_undone(self):
        items = [{"rid": "r6", "n": 6, "title": "整理广告投放测试策略", "status": "进行中"},
                 {"rid": "r9", "n": 9, "title": "已经做完的事情", "status": "完成"},
                 {"rid": "r7", "n": 7, "title": "另一件事情要做", "status": "进行中"}]
        named = ls._record("claude", "c-1", "随便聊聊", self.now, doing="帮我看下 #6 的数据")
        titled = ls._record("claude", "c-2", "整理广告投放测试策略", self.now)
        finished = ls._record("claude", "c-3", "接着改 #9", self.now)
        two = ls._record("claude", "c-4", "比较 #6 和 #7", self.now)
        from agent_matrix.worker import work_base
        folder = ls._record("claude", "c-5", "新对话", self.now, cwd=f"{work_base(self.home)}/r7")
        found = {r["key"]: r for r in (named, titled, finished, two, folder)}
        with patch.object(ls, "discover", return_value=found):
            out = {r["key"]: r["n"] for r in ls.view(self.home, {}, [], items)}
        self.assertEqual(out, {"claude:c-1": 6, "claude:c-2": 6, "claude:c-3": 9, "claude:c-4": None, "claude:c-5": 7})
        ls.unbind(self.home, "claude:c-1")
        with patch.object(ls, "discover", return_value=found):
            self.assertIsNone(next(r["n"] for r in ls.view(self.home, {}, [], items) if r["key"] == "claude:c-1"))

    def test_claude_conversations_open_in_the_claude_app_at_that_conversation(self):
        with patch.object(ls.subprocess, "run") as run:
            ls.open_claude("11111111-2222-4333-8444-555555555555", "local_aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
            self.assertEqual(run.call_args.args[0], ["open", "claude://code/continue?session=local_aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"])
            ls.open_claude("11111111-2222-4333-8444-555555555555")
            self.assertEqual(run.call_args.args[0], ["open", "claude://resume?session=11111111-2222-4333-8444-555555555555"])
            with self.assertRaises(MatrixError):
                ls.open_claude("not-an-id")

    def test_a_claude_conversation_opened_from_an_item_carries_its_context(self):
        root = self.home / "project"
        (root / "state").mkdir(parents=True)
        work = self.home / "work"
        work.mkdir()
        item = {"record_id": "r62", "n": 62, "title": "补齐角色流程图", "status": "完成", "who": "我", "next": "发流程图"}
        with patch("agent_matrix.itemwork.workdir_for", return_value=work), patch.object(ls.subprocess, "run") as run:
            ls.start_for_item(root, None, item, history="你：先画一级页面")
        url = run.call_args.args[0][1]
        self.assertTrue(url.startswith("claude://code/new?"))
        self.assertIn("folder=" + str(work).replace("/", "%2F"), url)
        self.assertIn("先画一级页面", (work / "心响事项.md").read_text(encoding="utf-8"))


class PickUpInCindy(unittest.TestCase):
    def test_an_item_is_picked_up_in_cindy_from_its_own_folder_with_the_whole_context(self):
        with tempfile.TemporaryDirectory() as temp:
            root, work, app = Path(temp) / "project", Path(temp) / "work", Path(temp) / "Cindy.app"
            (root / "state").mkdir(parents=True); work.mkdir(); app.mkdir()
            (work / "交付.md").write_text("v1", encoding="utf-8")
            item = {"record_id": "r61", "n": 61, "title": "更新考核方案", "status": "完成", "who": "AI", "next": "交方案"}
            with patch("agent_matrix.itemwork.workdir_for", return_value=work), patch.object(ls, "CINDY_APP", app), \
                 patch.object(ls, "_gpt_said", return_value=["[11-18 11:44] 已完成材料，方案见 交付.md"]), \
                 patch.object(ls.subprocess, "run") as run:
                out = ls.start_in_cindy(root, None, item, history="你：注册权重再高一点")
            self.assertEqual(run.call_args.args[0], ["open", "-a", str(app), "--", str(work)])
            note = (work / "心响事项.md").read_text(encoding="utf-8")
            for words in ("注册权重再高一点", "已完成材料", "- 交付.md"):
                self.assertIn(words, note)
            self.assertIn("先读 心响事项.md", (work / "AGENTS.md").read_text(encoding="utf-8"))
            self.assertEqual(out["opened"], "cindy")

    def test_without_cindy_it_says_so(self):
        with patch.object(ls, "CINDY_APP", Path("/nonexistent/Cindy.app")):
            with self.assertRaises(MatrixError):
                ls.start_in_cindy(Path("/tmp"), None, {"record_id": "r1", "n": 1})


class AppChoice(unittest.TestCase):
    """Open source: everyone has different apps, so where things open is a setting with a sensible default."""
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "state").mkdir()

    def test_the_default_is_the_first_installed_app_and_a_choice_wins_only_if_installed(self):
        from agent_matrix.thread import set_setting
        only_claude = {"cindy": False, "claude": True, "chatgpt": False, "terminal-claude": True, "terminal-codex": False}
        self.assertEqual(ls.pickup_choice(self.root, only_claude), "claude")
        set_setting(self.root, "pickup_app", "terminal-claude")
        self.assertEqual(ls.pickup_choice(self.root, only_claude), "terminal-claude")
        set_setting(self.root, "pickup_app", "cindy")                       # chosen, but not on this computer
        self.assertEqual(ls.pickup_choice(self.root, only_claude), "claude")
        set_setting(self.root, "gpt_open", "chatgpt")
        self.assertEqual(ls.gpt_open_choice(self.root, only_claude), "here")

    def test_picking_up_goes_to_the_chosen_app(self):
        item = {"record_id": "r1", "n": 1}
        with patch.object(ls, "apps", return_value={"cindy": True, "claude": True, "chatgpt": True, "terminal-claude": True, "terminal-codex": True}):
            for choice, target in [("cindy", "start_in_cindy"), ("claude", "start_for_item"), ("terminal-codex", "start_in_terminal")]:
                from agent_matrix.thread import set_setting
                set_setting(self.root, "pickup_app", choice)
                with patch.object(ls, target, return_value={"opened": choice}) as start:
                    self.assertEqual(ls.pick_up(self.root, None, item)["opened"], choice)
                start.assert_called_once()


class BuiltinTranscript(unittest.TestCase):
    def test_a_builtin_gpt_conversation_reads_as_task_words_commands_and_rounds(self):
        rows = [
            {"timestamp": "2026-11-19T02:28:27Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>cwd</environment_context>"}]}},
            {"timestamp": "2026-11-19T02:28:28Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "做 #65：更新标签方案"}]}},
            {"timestamp": "2026-11-19T02:28:30Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "我先读任务单。"}]}},
            {"timestamp": "2026-11-19T02:28:31Z", "type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "input": 'text(await tools.exec_command({cmd:"wc -m \\"交付.md\\""}))'}},
            {"timestamp": "2026-11-19T02:29:00Z", "type": "response_item", "payload": {"type": "function_call", "name": "shell", "arguments": json.dumps({"command": ["ls", "-la"]})}},
            {"timestamp": "2026-11-19T02:37:00Z", "type": "event_msg", "payload": {"type": "task_complete"}},
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rollout-x.jsonl"
            path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\nnot json\n", encoding="utf-8")
            got = [(e["who"], e["text"]) for e in sessions.read_rollout(path)]
        self.assertEqual(got, [("task", "做 #65：更新标签方案"), ("ai", "我先读任务单。"), ("run", 'wc -m "交付.md"'),
                               ("run", "ls -la"), ("sys", "这一轮做完了")])
