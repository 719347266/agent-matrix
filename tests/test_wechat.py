"""WeChat as a chat source: read through the wechat-cli command, filtered like every other chat."""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from agent_matrix.models import Config, MatrixError
from agent_matrix.sources import wechat
from agent_matrix.sources.chats import pending
from agent_matrix.state import State
from agent_matrix.timeutil import TZ

NOW = datetime(2026, 11, 20, 14, 0, tzinfo=TZ)
T = int(NOW.timestamp())
SESSIONS = [
    {"chat": "产品组", "username": "1001@chatroom", "is_group": True, "unread": 2, "last_message": "收到", "msg_type": "文本", "sender": "李工", "timestamp": T - 60},
    {"chat": "李工", "username": "wxid_li", "is_group": False, "unread": 1, "last_message": "明天把报价发我一下", "msg_type": "文本", "sender": "", "timestamp": T - 120},
    {"chat": "某公众号", "username": "gh_abc", "is_group": False, "unread": 9, "last_message": "推送", "msg_type": "文本", "sender": "", "timestamp": T - 10},
    {"chat": "折叠的群", "username": "@placeholder_foldgroup", "is_group": False, "unread": 0, "last_message": "x", "msg_type": "文本", "sender": "", "timestamp": T - 10},
    {"chat": "老同学", "username": "wxid_old", "is_group": False, "unread": 0, "last_message": "哈哈", "msg_type": "文本", "sender": "", "timestamp": T - 99999},
]
HISTORY = {
    "1001@chatroom": ["[2026-11-20 13:50] 李工: @王小明 周五前把定价方案发群里", "[2026-11-20 13:52] 张三: [图片] (local_id=1)",
                      "[2026-11-20 13:55] 小王: 我明天把测试报告整理出来", "[2026-11-20 13:58] 李工: 收到"],
    "wxid_li": ["[2026-11-20 13:40] 小王: 好的我来跟进这个事情", "[2026-11-20 13:58] 李工: 明天把报价发我一下"],
}


def fake(history=HISTORY, sessions=SESSIONS):
    calls = []

    def run(cli, *args, timeout=0):
        calls.append(args)
        if args[0] == "sessions":
            return sessions
        if history is None:
            raise MatrixError("找不到消息记录")
        return {"messages": history.get(args[1], [])}
    run.calls = calls
    return run


class WeChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "state").mkdir()
        self.state = State(self.root / "state/state.db")
        self.addCleanup(self.state.close)
        self.config = Config(owner_name="王小明", wechat_enabled=True, wechat_binary="/x/wechat-cli")

    def test_only_real_chats_that_changed_are_read_and_the_same_filter_applies(self):
        run = fake()
        read = wechat.collect(self.config, {}, NOW, run=run)
        self.assertEqual([c["id"] for c in read["chats"]], ["1001@chatroom", "wxid_li"])   # no official accounts, folders, or old chats
        self.assertEqual(read["behind"], 0)
        out = wechat.store(self.config, self.root, self.state, NOW, read)
        got = {m["text"]: m for m in pending(self.state)}
        self.assertEqual(out["candidates"], len(got))
        self.assertEqual(got["@王小明 周五前把定价方案发群里"]["kind"], "@你")
        self.assertEqual(got["明天把报价发我一下"]["kind"], "找你的")                  # a private chat: asked of the owner
        mine = got["好的我来跟进这个事情"]                                             # in a private chat the other side is the owner
        self.assertEqual((mine["kind"], mine["sender_name"]), ("我的承诺", "我"))
        self.assertEqual(self.state.get("wechat:me"), "小王")                        # the owner's WeChat name, learned there
        self.assertNotIn("[图片] (local_id=1)", got)
        for m in got.values():
            self.assertTrue(m["message_id"].startswith("wechat:") and m["chat_name"].startswith("微信 · "))
        self.assertEqual(wechat.cursors(self.state), {"1001@chatroom": T - 60, "wxid_li": T - 120})
        # the owner's name learned in the private chat now marks their lines in groups too
        self.state.db.execute("DELETE FROM chat_candidates")
        wechat.store(self.config, self.root, self.state, NOW, wechat.collect(self.config, {}, NOW, run=fake()))
        got = {m["text"]: m for m in pending(self.state)}
        self.assertEqual(got["我明天把测试报告整理出来"]["sender_name"], "我")
        # nothing new since: nothing is read again, and reading twice makes no duplicates
        again = wechat.collect(self.config, wechat.cursors(self.state), NOW, run=fake())
        self.assertEqual(again["chats"], [])
        before = len(pending(self.state))
        wechat.store(self.config, self.root, self.state, NOW, wechat.collect(self.config, {}, NOW, run=fake()))
        self.assertEqual(len(pending(self.state)), before)

    def test_when_the_records_cannot_be_read_the_last_message_still_counts(self):
        read = wechat.collect(self.config, {}, NOW, run=fake(history=None))
        self.assertEqual(read["behind"], 2)
        lines = {c["id"]: c["lines"] for c in read["chats"]}
        self.assertEqual(lines["wxid_li"], ["[2026-11-20 13:58] 李工: 明天把报价发我一下"])    # unread in a private chat: the other person said it
        out = wechat.store(self.config, self.root, self.state, NOW, read)
        self.assertEqual(out["candidates"], 1)
        self.assertTrue(self.state.get("wechat:status")["behind"])
        read_ones = [{**s, "unread": 0} for s in SESSIONS]
        quiet = wechat.collect(self.config, {}, NOW, run=fake(history=None, sessions=read_ones))
        self.assertEqual({c["id"]: c["lines"] for c in quiet["chats"]}["wxid_li"], [])           # read already: could be either side, so left out

    def test_a_round_reads_a_few_chats_and_the_rest_wait(self):
        many = [{"chat": f"群{i}", "username": f"{i}@chatroom", "is_group": True, "unread": 1, "last_message": "x", "msg_type": "文本",
                 "sender": "甲", "timestamp": T - i} for i in range(1, 10)]
        read = wechat.collect(self.config, {}, NOW, run=fake(history={}, sessions=many))
        self.assertEqual(([c["id"] for c in read["chats"]], read["waiting"]), (["1@chatroom", "2@chatroom", "3@chatroom", "4@chatroom"], 5))

    def test_without_the_command_it_says_so(self):
        import unittest.mock as mock
        none = Config(wechat_enabled=True)
        with mock.patch.object(wechat.shutil, "which", return_value=None):
            self.assertEqual(wechat.check(none)["installed"], False)
            with self.assertRaises(MatrixError):
                wechat.collect(none, {}, NOW, run=fake())
        self.assertEqual(wechat.check(self.config, run=fake())["ok"], True)

        def broken(cli, *args, timeout=0):
            raise MatrixError("微信没有打开")
        self.assertEqual(wechat.check(self.config, run=broken), {"ok": False, "installed": True, "error": "微信没有打开"})


if __name__ == "__main__":
    unittest.main()
