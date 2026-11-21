"""背景包: what the items before delivered, the owner's passed examples and rejection reasons, and the length cap."""
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from agent_matrix import background, itemlinks, memory, projects, thread

NOW = datetime(2026, 11, 21, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


class PackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def deliver(self, rid, text, template="", summary="做完了"):
        work = self.root / "state/work" / rid
        work.mkdir(parents=True, exist_ok=True)
        (work / "交付.md").write_text(text, encoding="utf-8")
        if template:
            (work / "结果.json").write_text(json.dumps({"template": template}, ensure_ascii=False), encoding="utf-8")
        thread.add_deliverable(self.root, rid, summary=summary, url=f"https://x.feishu.cn/docx/{rid}")

    def test_what_the_items_before_delivered_is_shown(self):
        rows = [{"record_id": "a", "编号": 1, "标题": "标签体系调研", "状态": ["完成"]},
                {"record_id": "b", "编号": 2, "标题": "标签需求文档", "状态": ["进行中"]},
                {"record_id": "c", "编号": 3, "标题": "标签上线排期", "状态": ["未开始"]}]
        itemlinks.link(self.root, "a", "b")
        itemlinks.link(self.root, "b", "c")
        self.deliver("a", "# 标签体系\n分三大类", summary="标签分三大类，口径按月活")
        text = background.build(self.root, None, rows, rows[2], NOW)
        self.assertIn("## 这件事接着前面这些事做", text)
        part = text.split("## 这件事接着前面这些事做", 1)[1].split("\n## ", 1)[0]
        self.assertIn("#1 标签体系调研（第 1 版）：标签分三大类，口径按月活", part)
        self.assertIn("https://x.feishu.cn/docx/a", part)
        self.assertIn(str(self.root / "state/work/a/交付.md"), part)
        self.assertIn("#2 标签需求文档（进行中）", part)  # nothing delivered yet: listed once, without a result
        self.assertEqual(part.count("#1 标签体系调研"), 1, "a delivered item is listed once, with its result")
        self.assertNotIn("这件事接着前面这些事做", background.build(self.root, None, rows, rows[0], NOW))  # nothing before it

    def test_passed_examples_of_the_same_kind_and_rejection_reasons(self):
        rows = [{"record_id": "w1", "编号": 1, "标题": "第45周周报"},
                {"record_id": "w2", "编号": 2, "标题": "第46周周报"},
                {"record_id": "p1", "编号": 3, "标题": "增长方案"},
                {"record_id": "new", "编号": 4, "标题": "第47周周报"}]
        self.deliver("w1", "## 本周完成\n- 上线标签\n\n## 风险\n- 无", template="周报")
        self.deliver("w2", "## 本周完成\n- 太散了", template="周报")
        self.deliver("p1", "## 推荐\n方案 A", template="方案")
        memory.record_outcome(self.root, "w1", "通过")
        memory.record_outcome(self.root, "w2", "退回", "风险没写具体负责人")
        memory.record_outcome(self.root, "p1", "通过")
        text = background.build(self.root, None, rows, rows[3], NOW)
        self.assertIn("## 被通过的同类交付（范例）", text)
        part = text.split("## 被通过的同类交付（范例）", 1)[1].split("\n## ", 1)[0]
        self.assertIn("这是本人通过过的写法，照它的结构和语气，不要照抄内容", part)
        self.assertIn("#1 第45周周报", part)
        self.assertIn("> - 上线标签", part)
        self.assertNotIn("方案 A", part)  # another kind
        self.assertIn("风险没写具体负责人", part)
        self.assertNotIn("太散了", part)  # a rejected one is not shown as an example

    def test_long_examples_are_cut_to_their_opening(self):
        rows = [{"record_id": "w1", "编号": 1, "标题": "第45周周报"}, {"record_id": "new", "编号": 2, "标题": "第47周周报"}]
        self.deliver("w1", "\n\n".join(f"第{i}段" + "字" * 300 for i in range(20)), template="周报")
        memory.record_outcome(self.root, "w1", "通过")
        part = background.build(self.root, None, rows, rows[1], NOW).split("## 被通过的同类交付（范例）", 1)[1]
        self.assertIn("第0段", part)
        self.assertNotIn("第10段", part)

    def test_no_kind_falls_back_to_the_same_project(self):
        rows = [{"record_id": "x", "编号": 1, "标题": "标签整理"}, {"record_id": "new", "编号": 2, "标题": "标签收尾"}]
        pid = projects.create(self.root, "标签体系")
        projects.assign(self.root, ["x", "new"], pid)
        self.deliver("x", "## 结论\n按月活分层")
        memory.record_outcome(self.root, "x", "通过")
        text = background.build(self.root, None, rows, rows[1], NOW)
        self.assertIn("同在项目「标签体系」", text)
        self.assertIn("按月活分层", text)

    def test_no_verdicts_no_example_section(self):
        rows = [{"record_id": "w1", "编号": 1, "标题": "第45周周报"}, {"record_id": "new", "编号": 2, "标题": "第47周周报"}]
        self.deliver("w1", "## 本周完成", template="周报")
        text = background.build(self.root, None, rows, rows[1], NOW)
        self.assertNotIn("被通过的同类交付", text)
        self.assertNotIn("这件事接着前面这些事做", text)
        memory.record_outcome(self.root, "w1", "通过")
        self.assertNotIn("被通过的同类交付", background.build(self.root, None, [rows[1]], rows[1] | {"标题": "报销"}, NOW))

    def test_too_long_is_cut_from_the_least_needed_end(self):
        sections = [(background.MUST, "## 本人给过的链接", ["- https://x.feishu.cn/docx/K"] * 50),
                    (background.EXAMPLES, "## 被通过的同类交付（范例）", ["范例" * 40] * 60),
                    (background.DOCS, "## 可能相关的飞书文档", ["文档" * 40] * 60),
                    (background.PAST, "## 这个话题以前讨论过", ["讨论" * 40] * 60)]
        text = background.fit(sections, budget=8000)
        self.assertLessEqual(len(text), 8000)
        self.assertEqual(text.count("https://x.feishu.cn/docx/K"), 50)       # the owner's links are all kept
        for heading in ("## 被通过的同类交付（范例）", "## 可能相关的飞书文档", "## 这个话题以前讨论过"):
            self.assertIn(heading, text)
        past = text.split("## 这个话题以前讨论过", 1)[1]
        self.assertNotIn("讨论讨论", past)                                    # the lowest goes first
        self.assertRegex(past, r"（太长，省略了 \d+ 字，需要时用下面的命令自己去读）")
        self.assertGreater(text.count("范例范例"), text.count("文档文档"))    # examples outrank documents
        self.assertNotIn("省略了", background.fit(sections[:1], budget=8000))

    def test_build_keeps_the_whole_pack_under_budget_and_keeps_the_notes(self):
        rows = [{"record_id": "new", "编号": 1, "标题": "第47周周报", "补充材料": "看 https://x.feishu.cn/docx/OWN"}]
        text = background.build(self.root, None, rows, rows[0], NOW, notes="笔记" * 3000, budget=5000)
        self.assertLessEqual(len(text), 5000)
        self.assertIn("https://x.feishu.cn/docx/OWN", text)
        self.assertIn("省略了", text)
        self.assertIn("不够就自己继续扒", text)


if __name__ == "__main__":
    unittest.main()
