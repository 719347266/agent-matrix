"""Chats are caught sooner (@you, urgency), and a finished item can go back to its AI with what to change."""
from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from agent_matrix.chat_review import ready
from agent_matrix.models import Config
from agent_matrix.reply import parse
from agent_matrix.sources.chats import classify, mentions_owner, owner_words
from test_v1_flow import applier, row

TZ = ZoneInfo("Asia/Shanghai")
CONFIG = Config(owner_open_id="ou_me", owner_name="王小明", owner_aliases=["小王"])
NOW = datetime(2026, 11, 18, 15, 0, tzinfo=TZ)


def line(text, kind="找你的", minutes_ago=2):
    return {"text": text, "kind": kind, "sent_at": (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M")}


class ChatCatchTests(unittest.TestCase):
    names = owner_words(CONFIG)

    def test_an_at_mention_is_kept_even_without_request_words(self):
        message = {"mentions": [{"id": "ou_me", "key": "@_user_1", "name": "王小明"}]}
        self.assertTrue(mentions_owner(message, "ou_me"))
        self.assertEqual(classify("@王小明 新功能反馈", from_owner=False, chat_mode="group", names=self.names, mentioned=True), "@你")
        self.assertIsNone(classify("@王小明", from_owner=False, chat_mode="group", names=self.names, mentioned=True))

    def test_a_question_in_a_private_chat_is_kept(self):
        self.assertEqual(classify("这个版本你看过了吗？", from_owner=False, chat_mode="p2p", names=self.names), "找你的")

    def test_urgent_or_at_you_goes_at_once_the_rest_follows_the_chosen_pace(self):
        self.assertIsNone(ready(CONFIG, [], NOW, "every"))
        self.assertIsNotNone(ready(CONFIG, [line("小王，今天下班前把数据发我")], NOW))
        self.assertIsNotNone(ready(CONFIG, [line("新功能反馈", kind="@你")], NOW))
        self.assertIsNone(ready(CONFIG, [line("小王，帮忙看下这个需求")], NOW))                # not due yet: waits
        self.assertIsNotNone(ready(CONFIG, [line("小王，帮忙看下这个需求")], NOW, "every"))    # the pace came round
        private = {**line("你们测试服有没有空升级一下包"), "chat_mode": "p2p"}
        self.assertIsNotNone(ready(CONFIG, [private], NOW))                                  # a private ask: at once
        self.assertIsNotNone(ready(CONFIG, [line("小王，帮忙看下这个需求")], NOW, "2026-11-18T14:30"))
        night = NOW.replace(hour=23)
        self.assertIsNone(ready(CONFIG, [line("小王，帮忙看下这个需求")], night, "every"))      # late: morning
        self.assertIsNotNone(ready(CONFIG, [line("小王，马上回我一下")], night))                 # unless urgent


class ReviseTests(unittest.TestCase):
    def test_change_words_parse_as_revise_but_a_date_stays_a_reschedule(self):
        ops, _ = parse("62 改 把颜色换成蓝色", NOW.date())
        self.assertEqual((ops[0].verb, ops[0].arg), ("revise", "把颜色换成蓝色"))
        ops, _ = parse("62 改到周五", NOW.date())
        self.assertEqual(ops[0].verb, "reschedule")

    def test_a_finished_item_goes_back_to_the_ai_with_what_to_change(self):
        a = applier([row(62, "完成", "我", 完成时间="2026-11-18T17:42:08+08:00")])
        out = a.apply(parse("62 改 加上竞品对比", NOW.date())[0][0])
        self.assertIn("✓", out[0])
        rid, patch = a.gateway.writes[-1]
        self.assertEqual((patch["状态"], patch["执行方"], patch["完成时间"]), (["进行中"], ["AI"], None))
        self.assertIn("加上竞品对比", patch["退回意见"])

    def test_a_cancelled_item_is_not_reopened_by_a_change_request(self):
        a = applier([row(63, "取消", "AI")])
        out = a.apply(parse("63 改 再试一次", NOW.date())[0][0])
        self.assertNotIn("✓", out[0])
        self.assertEqual(a.gateway.writes, [])


if __name__ == "__main__":
    unittest.main()
