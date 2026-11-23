from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent_matrix import drive, inbox
from agent_matrix.feishu import FeishuError
from agent_matrix.models import Config
from agent_matrix.sources import chats
from agent_matrix.state import State
from agent_matrix.timeutil import TZ


class ChatPolling(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = State(self.root / "state" / "state.db")
        self.addCleanup(self.state.close)
        self.now = datetime(2026, 11, 23, 10, tzinfo=TZ)
        self.client = Mock(root=self.root)
        self.rows = [{"chat_id": str(i), "chat_mode": "p2p"} for i in range(8)]
        self.activity = []
        self.history = []

        def reply(args):
            if args[1] == "+chat-list":
                return {"chats": self.rows}
            if args[1] == "+messages-search":
                return {"has_more": False, "messages": [{"chat_id": c} for c in self.activity]}
            self.history.append(args)
            return {"messages": [], "has_more": False}

        self.client._exec.side_effect = reply

    def poll(self, minutes=0):
        return chats.poll(Config(), self.client, self.state, self.now + timedelta(minutes=minutes))

    def test_idle_active_and_restart(self):
        self.poll()
        self.assertEqual(self.client._exec.call_count, 2)
        self.poll(2)
        self.assertEqual(self.client._exec.call_count, 2)
        self.assertFalse(self.history)
        self.activity = ["3"]
        self.assertEqual(self.poll(30)["histories_read"], 1)
        self.assertEqual(self.history[0][3], "3")

    def test_busy_chats_rotate_and_keep_deferred_cursors(self):
        self.activity = [row["chat_id"] for row in self.rows]
        self.poll()
        self.assertEqual(len(self.history), 5)
        waiting = self.state.get("chats:cursor:7")
        self.poll(30)
        self.assertEqual(len(self.history), 10)
        self.assertIn(["--chat-id", "7", "--start", waiting], [args[2:6] for args in self.history[5:]])

    def test_failure_waits_even_after_restarting(self):
        self.client._exec.side_effect = FeishuError(429)
        with self.assertRaises(FeishuError):
            self.poll()
        self.poll(30)
        self.assertEqual(self.client._exec.call_count, 1)
        with self.assertRaises(FeishuError):
            self.poll(60)
        self.poll(120)
        self.assertEqual(self.client._exec.call_count, 2)

    def test_failed_batch_waits_and_alerts_once_across_new_leases(self):
        batch = "chat-20261123-0918"
        job = {"id": "chat-" + batch, "kind": "chat", "title": "提取聊天待办",
               "folder": str(self.root), "ref": {"source": str(self.root / batch)}, "lease_until": None}
        checked = {"run_model": True, "batch": {"batch_id": batch}}
        with patch("agent_matrix.workenv._check", return_value=(0, checked)):
            self.assertTrue(drive._chat_due(self.root))
            for attempt in range(2):
                current = dict(job, id=job["id"] + "-" + str(attempt), status="open", rounds=0)
                drive._save(self.root, current)
                drive.fail(self.root, current, "missing")
            self.assertFalse(drive._chat_due(self.root))
            with patch.object(drive, "now", return_value=drive.now() + timedelta(minutes=31)):
                self.assertTrue(drive._chat_due(self.root))
        self.assertEqual(len(inbox.messages(self.root)), 1)
