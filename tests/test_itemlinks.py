"""Items strung on one line, and replacing where an item's deliverable lives."""
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from agent_matrix import background, itemlinks
from agent_matrix.models import MatrixError
from agent_matrix.reply import parse
from test_v1_flow import applier, row

NOW = datetime(2026, 11, 19, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_a_line_of_three_is_walked_nearest_first_both_ways(self):
        itemlinks.link(self.root, "a", "b")
        itemlinks.link(self.root, "b", "c")
        itemlinks.link(self.root, "b", "c")  # again: still one link
        self.assertEqual(itemlinks.pairs(self.root), [("a", "b"), ("b", "c")])
        self.assertEqual(itemlinks.upstream(self.root, "c"), ["b", "a"])
        self.assertEqual(itemlinks.downstream(self.root, "a"), ["b", "c"])
        self.assertEqual(itemlinks.direct(self.root, "b"), {"before": ["a"], "after": ["c"]})

    def test_a_loop_or_a_link_to_itself_is_refused(self):
        itemlinks.link(self.root, "a", "b")
        itemlinks.link(self.root, "b", "c")
        for earlier, later in (("c", "a"), ("a", "a")):
            with self.assertRaises(MatrixError):
                itemlinks.link(self.root, earlier, later)
        self.assertEqual(len(itemlinks.pairs(self.root)), 2)

    def test_turning_one_link_round_replaces_it_and_unlink_works_either_way(self):
        itemlinks.link(self.root, "a", "b")
        itemlinks.link(self.root, "b", "a")
        self.assertEqual(itemlinks.pairs(self.root), [("b", "a")])
        itemlinks.unlink(self.root, "a", "b")
        self.assertEqual(itemlinks.pairs(self.root), [])

    def test_the_ai_is_shown_what_came_before_with_its_deliverable(self):
        rows = [{"record_id": "a", "编号": 1, "标题": "数据看板调研", "状态": ["完成"], "交付物链接": "https://x.feishu.cn/docx/A1", "预做结果摘要": "分三大类"},
                {"record_id": "b", "编号": 2, "标题": "标签需求文档", "状态": ["进行中"]},
                {"record_id": "c", "编号": 3, "标题": "标签上线排期", "状态": ["未开始"]}]
        itemlinks.link(self.root, "a", "b")
        itemlinks.link(self.root, "b", "c")
        before, after = background.linked_items(self.root, rows, rows[1])
        self.assertEqual(before, ["- #1 数据看板调研（完成） 交付：https://x.feishu.cn/docx/A1 结论：分三大类"])
        self.assertEqual(after, ["- #3 标签上线排期（未开始）"])
        text = background.build(self.root, None, rows, rows[2], NOW)
        self.assertIn("接着前面这些事做", text)
        self.assertLess(text.index("#2 标签需求文档"), text.index("#1 数据看板调研"))  # nearest first
        self.assertNotIn("后面还有这些事", text)


class ReplaceLinkTests(unittest.TestCase):
    def test_the_owner_replaces_the_deliverable_link_without_changing_the_status(self):
        a = applier([row(62, "待验收", "AI", 交付物链接="https://x.feishu.cn/docx/OLD")])
        out = a.apply(parse("62 交付链接 https://x.feishu.cn/docx/NEW", NOW.date())[0][0])
        self.assertIn("换好了 ✓", out[0])
        self.assertEqual(a.gateway.writes[-1][1], {"交付物链接": "https://x.feishu.cn/docx/NEW"})

    def test_a_finished_item_takes_a_link_and_counts_as_delivered(self):
        a = applier([row(62, "完成", "我", 完成时间="2026-11-18T17:42:08+08:00")])
        out = a.apply(parse("62 交付链接 https://x.feishu.cn/docx/NEW", NOW.date())[0][0])
        self.assertIn("✓", out[0])
        self.assertEqual(a.gateway.writes[-1][1], {"交付物链接": "https://x.feishu.cn/docx/NEW", "交付情况": ["有交付"]})

    def test_not_a_link_same_link_and_a_cancelled_item_change_nothing(self):
        a = applier([row(62, "待验收", "AI", 交付物链接="https://x.feishu.cn/docx/OLD"), row(63, "取消", "我")])
        before = len(a.gateway.writes)
        self.assertIn("没改", a.apply(parse("62 交付链接 随便写的", NOW.date())[0][0])[0])
        self.assertIn("已经是这个链接", a.apply(parse("62 交付链接 https://x.feishu.cn/docx/OLD", NOW.date())[0][0])[0])
        self.assertIn("没动", a.apply(parse("63 交付链接 https://x.feishu.cn/docx/NEW", NOW.date())[0][0])[0])
        self.assertEqual(len(a.gateway.writes), before)


if __name__ == "__main__":
    unittest.main()


class AiLinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.rows = [{"record_id": "a", "编号": 31}, {"record_id": "b", "编号": 32}]

    def test_the_ai_strings_a_new_item_after_the_one_it_continues(self):
        self.assertTrue(itemlinks.link_by_number(self.root, self.rows, "b", 31))
        self.assertEqual(itemlinks.pairs(self.root), [("a", "b")])
        self.assertEqual(itemlinks.made_by(self.root), {("a", "b"): "AI"})

    def test_a_wrong_guess_links_nothing_and_never_raises(self):
        for number in (None, 0, 999, 32):  # nothing, not an item, the item itself
            self.assertFalse(itemlinks.link_by_number(self.root, self.rows, "b", number))
        self.assertEqual(itemlinks.pairs(self.root), [])

    def test_the_model_is_given_open_and_recently_finished_items_to_pick_from(self):
        import json
        from unittest.mock import patch
        from agent_matrix import drive
        rows = [{"编号": 1, "标题": "在做", "状态": "进行中"}, {"编号": 2, "标题": "刚做完", "状态": "完成", "完成时间": drive.now().isoformat()},
                {"编号": 3, "标题": "很久前做完", "状态": "完成", "完成时间": "2026-01-01T00:00:00"}, {"编号": 4, "标题": "不做了", "状态": "取消"},
                {"编号": 5, "标题": "别人的", "状态": "进行中", "排除本人提醒": True}]
        with patch("agent_matrix.snapshot._snapshot", return_value={"rows": rows}):
            drive._write_related(self.root, self.root)
        self.assertEqual([x["编号"] for x in json.loads((self.root / "related_items.json").read_text(encoding="utf-8"))], [1, 2])


class VagueDateTests(unittest.TestCase):
    """「下周」 has no exact day: the date is left empty instead of sending the batch back to the model."""

    def test_a_date_the_words_do_not_give_is_cleared_and_the_action_kept(self):
        import json
        from agent_matrix import chat_review
        from agent_matrix.models import Config
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "state/chat-batches/b1"
            folder.mkdir(parents=True)
            message = {"message_id": "m1", "chat_id": "c", "chat_name": "群", "chat_mode": "group", "sender_name": "王小明",
                       "sent_at": "2026-11-19 17:46", "text": "下周我来看心理画像，周五前给你结论", "kind": "@你"}
            (folder / "candidates.json").write_text(json.dumps({"batch_id": "b1", "messages": [message]}, ensure_ascii=False))
            def action(**kw):
                return chat_review.ChatAction(message_id="m1", title="看心理画像", quote="下周我来看心理画像", commitment_phrase="我来",
                                              owner_name="王小明", confidence=0.9, **kw)
            config = Config(owner_name="麦", owner_open_id="ou_me")
            vague = chat_review.ChatReview(batch_id="b1", actions=[action(due_date="2026-11-26", due_phrase="下周")])
            kept = chat_review.validate(config, vague, tmp)
            self.assertEqual([(a.due_date, a.due_phrase) for a, _ in kept], [(None, None)])
            exact = chat_review.ChatReview(batch_id="b1", actions=[action(due_date="2026-11-20", due_phrase="周五前")])
            self.assertEqual(chat_review.validate(config, exact, tmp)[0][0].due_date, "2026-11-20")
            guessed = chat_review.ChatReview(batch_id="b1", actions=[action(due_date="2026-11-30", due_phrase=None)])
            self.assertIsNone(chat_review.validate(config, guessed, tmp)[0][0].due_date)
