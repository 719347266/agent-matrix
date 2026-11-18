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
        self.assertEqual(set(records), {"cindy:current","claude:standalone"})

    def test_codex_finished_or_aborted_not_running_even_with_tool_left_over(self):
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
        self.assertEqual(set(ls.discover(self.home)), {"codex:working"})

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
