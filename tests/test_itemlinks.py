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
        rows = [{"record_id": "a", "编号": 1, "标题": "标签体系调研", "状态": ["完成"], "交付物链接": "https://x.feishu.cn/docx/A1", "预做结果摘要": "分三大类"},
                {"record_id": "b", "编号": 2, "标题": "标签需求文档", "状态": ["进行中"]},
                {"record_id": "c", "编号": 3, "标题": "标签上线排期", "状态": ["未开始"]}]
        itemlinks.link(self.root, "a", "b")
        itemlinks.link(self.root, "b", "c")
        before, after = background.linked_items(self.root, rows, rows[1])
        self.assertEqual(before, ["- #1 标签体系调研（完成） 交付：https://x.feishu.cn/docx/A1 结论：分三大类"])
        self.assertEqual(after, ["- #3 标签上线排期（未开始）"])
        text = background.build(self.root, None, rows, rows[2], NOW)
        self.assertIn("接着前面这些事做", text)
        self.assertLess(text.index("#2 标签需求文档"), text.index("#1 标签体系调研"))  # nearest first
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
