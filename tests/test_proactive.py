"""主动层: interrupt only for what matters, never during a meeting, and open the right item."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")


class ProactiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_what_matters_interrupts(self):
        from agent_matrix import inbox, proactive
        from agent_matrix.brief_v1 import _block, _row_item, card
        inbox.add(self.root, "a", card("AI 做完了，请你验收", "", "green", [_block("做完了", "green", [
            _row_item(("#61 考核方案", "", "grey", None), cmd="61 过")])], "", None))
        inbox.add(self.root, "b", "聊天 · 新增 2 件：别人 #57 #58")            # other people's: quiet
        inbox.add(self.root, "c", "聊天 · 新增 3 件：别人 #57 #58 / 你 #59")    # mine among them: alert, open #59
        inbox.add(self.root, "d", {"header": {"title": {"content": "11/18 晚报"}}, "body": {"elements": []}}, kind="brief")
        inbox.add(self.root, "e", "⚠ 会议提取没做成\n原因：……", kind="system")
        items = proactive.alerts(self.root)["items"]
        self.assertEqual([(i["title"], i["n"]) for i in items],
                         [("AI 做完了，等你验收", 61), ("新进来的事，等你定谁来做", 59), ("⚠ 会议提取没做成", None)])
        self.assertEqual(proactive.recent(self.root, 2)[0]["title"], "⚠ 会议提取没做成")

    def test_alerts_wait_while_the_owner_is_in_a_meeting(self):
        from agent_matrix import proactive
        now = datetime(2026, 11, 18, 10, 0, tzinfo=TZ)
        (self.root / "state").mkdir()
        db = sqlite3.connect(self.root / "state/state.db")
        db.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)")
        events = [{"summary": "AI项目例会", "start_time": {"datetime": (now - timedelta(minutes=30)).isoformat()},
                   "end_time": {"datetime": (now + timedelta(minutes=45)).isoformat()}}]
        db.execute("INSERT INTO kv VALUES ('meeting_agenda_snapshot', ?)", (json.dumps({"events": events}),))
        db.commit(); db.close()
        held, why, until = proactive.quiet(self.root, now)
        self.assertTrue(held)
        self.assertIn("AI项目例会", why)
        self.assertFalse(proactive.quiet(self.root, now + timedelta(hours=1))[0])

    def test_focus_counts(self):
        from agent_matrix import proactive
        need = [{"label": "待你验收"}, {"label": "待你复查"}, {"label": "卡住", "who": "AI"}, {"label": "需要你定"}]
        self.assertEqual(proactive.focus(need), {"review": 2, "ask": 1, "decide": 1, "other": 0, "total": 4})


if __name__ == "__main__":
    unittest.main()
