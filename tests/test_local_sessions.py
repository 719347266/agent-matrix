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
            db.execute("CREATE TABLE threads(id,title,cwd,rollout_path,updated_at,archived)")
            for sid, events in [("working",["task_started"]),("ended",["task_started","task_complete"]),("aborted",["task_started","turn_aborted"])]:
                path = folder/(sid+".jsonl")
                path.write_text("\n".join(json.dumps({"timestamp":stamp,"type":"event_msg","payload":{"type":ev}}) for ev in events))
                db.execute("INSERT INTO threads VALUES(?,?,?,?,?,0)",(sid,sid,str(folder),str(path),self.now))
        (base/"process_manager").mkdir()
        (base/"process_manager/chat_processes.json").write_text(json.dumps([dict(osPid=os.getpid(),updatedAtMs=self.now*1000,conversationId="ended")]))
        found = ls.discover(self.home)
        self.assertEqual({k: v["working"] for k, v in found.items()},
                         {"codex:working": True, "codex:ended": False, "codex:aborted": False})

    def test_unbound_then_verified_binding_preserves_original_session(self):
        rec = ls._record("cindy","exact-id","不像事项标题",self.now)
        key = rec["key"]
        known = [{"rid":"r6","n":6}]
        with patch.object(ls,"discover",return_value={key:rec}), patch("agent_matrix.monitor._snapshot",return_value={"rows":[{"record_id":"r6","编号":6}]}):
            self.assertIsNone(ls.view(self.home,{},[],known)[0]["n"])
            self.assertEqual(ls.view(self.home,{"r6":[{"session":"exact-id"}]},[],known)[0]["n"],6)
            with self.assertRaises(MatrixError): ls.associate(self.home,key,7)
            ls.associate(self.home,key,6)
            self.assertEqual(ls.view(self.home,{},[],known)[0]["n"],6)
        hist = {}
        with patch.object(ls,"discover",return_value={}), patch.object(ls.subprocess,"run") as native:
            self.assertEqual(ls.view(self.home,hist,[],known),[])
            self.assertEqual(hist["r6"][0]["status"],"recorded")
            self.assertEqual(sessions.open_session(self.home,None,hist["r6"][0]["job"]),{"opened":"cindy"})
            self.assertEqual(native.call_args.args[0], ["open","cindy://session/exact-id"])

    def test_latest_request_skips_tool_results_and_system_notes(self):
        folder = self.home / ".claude/projects/-Users-x-proj"
        folder.mkdir(parents=True)
        rows = [{"type": "user", "message": {"content": "第一句"}},
                {"type": "user", "message": {"content": [{"type": "text", "text": "<pasted_content id=\"a\">把 #62 的流程图补齐</pasted_content><system-reminder>内部提示</system-reminder>"}]}},
                *[{"type": "user", "message": {"content": [{"type": "tool_result", "content": "x" * 50}]}} for _ in range(30)]]
        (folder / "abcd-1234.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows))
        self.assertEqual(ls._last_request(self.home, "abcd-1234"), "把 #62 的流程图补齐")

    def test_an_unbound_conversation_suggests_the_item_it_names(self):
        items = [{"rid": "r6", "n": 6, "title": "整理聚光投放测试策略", "status": "进行中"},
                 {"rid": "r9", "n": 9, "title": "已经做完的事情", "status": "完成"}]
        named = ls._record("claude", "c-1", "随便聊聊", self.now, doing="帮我看下 #6 的数据")
        titled = ls._record("claude", "c-2", "整理聚光投放测试策略", self.now)
        closed = ls._record("claude", "c-3", "接着做 #9", self.now)
        with patch.object(ls, "discover", return_value={r["key"]: r for r in (named, titled, closed)}):
            out = {r["key"]: r["suggest"] for r in ls.view(self.home, {}, [], items)}
        self.assertEqual(out, {"claude:c-1": 6, "claude:c-2": 6, "claude:c-3": None})

    def test_a_claude_conversation_opened_from_an_item_is_bound_with_its_context(self):
        root = self.home / "project"
        (root / "state").mkdir(parents=True)
        work = self.home / "work"
        work.mkdir()
        item = {"record_id": "r62", "n": 62, "title": "补齐角色流程图", "status": "完成", "who": "我", "next": "发流程图"}
        with patch("agent_matrix.worker.claude_binary", return_value="/bin/claude"), \
             patch("agent_matrix.worker.workdir_for", return_value=work), \
             patch.object(sessions, "open_terminal") as terminal:
            out = ls.start_for_item(root, None, item, history="你：先画一级页面")
        line = terminal.call_args.args[0]
        self.assertIn("--session-id " + out["session"], line)
        self.assertIn("先画一级页面", (work / "心响事项.md").read_text(encoding="utf-8"))
        with closing(ls._db(root)) as db:
            self.assertEqual(db.execute("SELECT key, rid FROM local_session_items").fetchone(), ("claude:" + out["session"], "r62"))
