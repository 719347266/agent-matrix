"""Real-time sources: chat pre-filter, chat extraction rules, Feishu task mapping, listener replies."""
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from agent_matrix.chat_review import ChatAction, ChatReview, due_slot, task_fields as chat_fields, validate
from agent_matrix.models import Config, MatrixError
from agent_matrix.sources.chats import classify, owner_words
from agent_matrix.sources.feishu_tasks import fields_for

TZ = ZoneInfo("Asia/Shanghai")
CONFIG = Config(owner_open_id="ou_me", owner_name="王小明", owner_aliases=["小王"])


class ChatFilterTests(unittest.TestCase):
    names = owner_words(CONFIG)

    def test_keeps_only_lines_about_the_owner(self):
        keep = lambda t, **k: classify(t, names=self.names, **{"from_owner": False, "chat_mode": "group", **k})
        self.assertEqual(keep("好的，这个我来整理，明天给你", from_owner=True), "我的承诺")
        self.assertIsNone(keep("今天天气不错，大家辛苦了", from_owner=True))
        self.assertEqual(keep("小王，周五前帮忙看下这个需求"), "找你的")
        self.assertIsNone(keep("周五前帮忙看下这个需求"))            # group line not addressed to the owner
        self.assertEqual(keep("能不能明天把数据发我", chat_mode="p2p"), "找你的")
        self.assertIsNone(keep("哈哈哈哈哈好", chat_mode="p2p"))
        self.assertIsNone(keep("嗯嗯"))


class ChatReviewTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        folder = self.root / "state/chat-batches/b1"; folder.mkdir(parents=True)
        messages = [{"message_id": "om_1", "chat_id": "oc_1", "chat_name": "运营群", "chat_mode": "group",
                     "sender_id": "ou_x", "sender_name": "陈一凡", "sent_at": "2026-11-17 10:00",
                     "text": "小王，周五前帮忙把投放预算定一下", "context": "[]", "kind": "找你的"}]
        (folder / "candidates.json").write_text(json.dumps({"batch_id": "b1", "messages": messages}, ensure_ascii=False))

    def action(self, **k):
        base = dict(message_id="om_1", title="定投放预算", quote="周五前帮忙把投放预算定一下", commitment_phrase="帮忙把投放预算定一下",
                    owner_name="王小明", due_date="2026-11-20", due_phrase="周五", executor="我", confidence=0.9)
        return ChatAction(**{**base, **k})

    def test_named_owner_and_written_date_pass(self):
        kept = validate(CONFIG, ChatReview(batch_id="b1", actions=[self.action()]), self.root)
        self.assertEqual(len(kept), 1)
        fields = chat_fields(CONFIG, *kept[0])
        self.assertEqual((fields["执行方"], fields["状态"], fields["来源"]), (["我"], ["未开始"], ["聊天"]))

    def test_guesses_are_rejected_and_low_confidence_dropped(self):
        for bad in ({"quote": "没说过的话"}, {"commitment_phrase": "我来负责"}, {"owner_name": "李大伟"}, {"due_date": "2026-11-21"}):
            with self.assertRaises(MatrixError):
                validate(CONFIG, ChatReview(batch_id="b1", actions=[self.action(**bad)]), self.root)
        self.assertEqual(validate(CONFIG, ChatReview(batch_id="b1", actions=[self.action(confidence=0.5)]), self.root), [])

    def test_review_runs_only_at_set_times(self):
        state = SimpleNamespace(get=lambda key, default=None: None)
        self.assertIsNone(due_slot(CONFIG, state, datetime(2026, 11, 17, 10, 0, tzinfo=TZ)))
        self.assertEqual(due_slot(CONFIG, state, datetime(2026, 11, 17, 15, 0, tzinfo=TZ)), "2026-11-17T14:30")


class FeishuTaskTests(unittest.TestCase):
    def test_task_becomes_owner_item_with_link(self):
        task = {"guid": "g1", "summary": "投放方案撰写", "url": "https://applink.feishu.cn/client/todo/detail?guid=g1",
                "due": {"timestamp": "1795622400000"}}
        f = fields_for(task, CONFIG, TZ)
        self.assertEqual((f["执行方"], f["状态"], f["来源"]), (["我"], ["未开始"], ["飞书任务"]))
        self.assertEqual(f["关联任务链接"], task["url"]); self.assertTrue(f["截止日"].startswith("2026-11-"))


class ListenerReplyTests(unittest.TestCase):
    def test_only_owner_private_messages_are_commands(self):
        from agent_matrix.listen import Listener
        listener = Listener.__new__(Listener)
        store = {}
        state = SimpleNamespace(get=lambda k, d=None: store.get(k, d), set=lambda k, v: store.__setitem__(k, v))
        event = {"chat_type": "group", "sender_id": {"open_id": "ou_me"}, "message_id": "om_9", "content": '{"text":"15 过"}'}
        self.assertEqual(listener.on_message(CONFIG, None, state, event), "ignored")
        event = {**event, "chat_type": "p2p", "sender_id": {"open_id": "ou_other"}}
        self.assertEqual(listener.on_message(CONFIG, None, state, event), "ignored")


if __name__ == "__main__":
    unittest.main()


class LockTests(unittest.TestCase):
    def test_waiting_run_gets_the_lock_once_the_other_finishes(self):
        import threading, time as t
        from agent_matrix.state import exclusive_lock
        from agent_matrix.models import MatrixBusy
        root = Path(tempfile.mkdtemp())
        with exclusive_lock(root):
            with self.assertRaises(MatrixBusy):
                with exclusive_lock(root, wait=0):
                    pass
        hold = threading.Event()
        def holder():
            with exclusive_lock(root):
                hold.set(); t.sleep(1.5)
        threading.Thread(target=holder).start(); hold.wait()
        start = t.monotonic()
        with exclusive_lock(root, wait=10):
            self.assertGreater(t.monotonic() - start, 1)
