"""交付规格: each kind of deliverable has a spec that grows from a draft to stable by the owner's verdicts, and work uses it
as far as it is settled: the expert view only when forming or reviewing a spec; an outline first and a review round of
its own while the spec is new or shaky; 快速 / 仔细 override."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_matrix import itemwork, memory, specs
from agent_matrix.timeutil import TZ

SPEC = "## 结构\n1. 要解决什么问题\n## 写法\n完整句子\n## 要求\n先说用途\n## 检查清单\n- 每条规则写了边界\n- 有验收标准\n## 范例\n- https://x"


class Maturity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "state").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def verdict(self, *vs):
        out = [{"template": "需求文档", "verdict": v, "at": f"11-{20 + k:02d} 10:00"} for k, v in enumerate(vs)]
        return patch("agent_matrix.memory.outcomes", return_value=out)

    def test_from_nothing_to_draft_to_trial_to_stable(self):
        self.assertEqual(specs.state(self.root, "需求文档")["state"], "none")
        self.assertTrue(specs.form(self.root, "需求文档", SPEC))
        self.assertFalse(specs.form(self.root, "需求文档", SPEC + "x"), "a spec that exists is not overwritten by a new draft")
        with patch("agent_matrix.specs._meta", return_value={}):
            with self.verdict():
                self.assertEqual(specs.state(self.root, "需求文档")["state"], "draft")
            with self.verdict("退回", "通过"):
                self.assertEqual(specs.state(self.root, "需求文档")["state"], "trial")
            with self.verdict("通过", "通过", "要改", "通过", "通过"):
                s = specs.state(self.root, "需求文档")
                self.assertEqual((s["state"], s["passed"], s["recent"]), ("stable", 4, 5))

    def test_a_stable_spec_that_starts_failing_or_is_old_is_due_for_review(self):
        specs.form(self.root, "需求文档", SPEC)
        with patch("agent_matrix.specs._meta", return_value={}):
            with self.verdict("通过", "通过", "通过", "通过", "通过", "退回", "要改"):
                self.assertEqual(specs.state(self.root, "需求文档")["state"], "review")
        old = (datetime.now(TZ) - timedelta(days=40)).isoformat()
        with patch("agent_matrix.specs._meta", return_value={"reviewed": old}), self.verdict("通过", "通过", "通过", "通过", "通过"):
            with patch("agent_matrix.specs.verdicts", return_value=[{"verdict": "通过", "at": "12-30 10:00"}] * 5):
                self.assertEqual(specs.state(self.root, "需求文档")["state"], "review")

    def test_what_a_round_does_follows_how_settled_the_spec_is_and_the_mode(self):
        p = specs.plan(self.root, "r1", "需求文档")
        self.assertEqual((p["outline"], p["review"], p["expert"], p["form"]), (True, True, True, True), "nothing yet: research, outline, review, form")
        specs.form(self.root, "需求文档", SPEC)
        p = specs.plan(self.root, "r1", "需求文档")
        self.assertEqual((p["outline"], p["review"], p["expert"]), (True, True, False), "a draft: no expert view any more")
        with patch("agent_matrix.specs.state", return_value={"state": "stable", "label": "稳定"}):
            p = specs.plan(self.root, "r1", "需求文档")
            self.assertEqual((p["outline"], p["review"], p["check"], p["expert"]), (False, False, True, False), "stable: write, check, hand in")
        self.assertFalse(specs.plan(self.root, "r1", "回复草稿")["outline"], "a small thing never waits for an outline in 自动")
        self.assertFalse(specs.plan(self.root, "r1", "需求文档", outline_done=True)["outline"], "the outline is asked for once")
        specs.set_mode(self.root, "fast")
        self.assertEqual([specs.plan(self.root, "r1", "需求文档")[k] for k in ("outline", "review", "check")], [False, False, True], "快速 still checks against the list")
        specs.set_mode(self.root, "careful", rid="r2")
        self.assertTrue(specs.plan(self.root, "r2", "回复草稿")["outline"], "one item can be more careful than the rest")
        specs.set_mode(self.root, "", rid="r2")
        self.assertEqual(specs.mode(self.root, "r2"), "fast")

    def test_the_way_of_working_is_three_choices_and_the_named_ways_are_mixes_of_them(self):
        self.assertEqual(specs.style(self.root)["preset"], "auto", "平衡 to begin with")
        self.assertEqual(specs.describe(specs.style(self.root)), "直接开工 · 新类型先给提纲 · 新类型多审一轮")
        st = specs.set_style(self.root, outline="always")
        self.assertEqual((st["preset"], st["start"], st["outline"], st["review"]), ("custom", "auto", "always", "new"))
        self.assertTrue(specs.plan(self.root, "r1", "回复草稿")["outline"], "每件先给提纲: even a small thing")
        self.assertTrue(specs.plan(self.root, "r1", "回复草稿")["review"], "新类型多审一轮: a kind with no settled way of writing yet gets its round")
        with self.assertRaises(ValueError):
            specs.set_style(self.root, start="maybe")
        specs.set_mode(self.root, "careful")
        self.assertEqual(specs.style(self.root), {"start": "ask", "outline": "always", "review": "always", "preset": "careful"})
        specs.set_mode(self.root, "fast", rid="r9")
        self.assertFalse(specs.plan(self.root, "r9", "需求文档")["outline"], "one item can use another named way")
        self.assertEqual(specs.mode(self.root, "r9"), "fast")

    def test_an_owner_from_before_keeps_the_named_way_chosen_then(self):
        from agent_matrix.thread import set_setting
        set_setting(self.root, "work_mode", "careful")
        self.assertEqual(specs.style(self.root)["preset"], "careful")

    def test_a_revision_waits_for_the_owner(self):
        specs.form(self.root, "方案", SPEC)
        self.assertTrue(specs.propose(self.root, "方案", SPEC + "\n- 写清成本"))
        self.assertNotIn("写清成本", specs.text(self.root, "方案"))
        self.assertTrue(specs.accept(self.root, "方案"))
        self.assertIn("写清成本", specs.text(self.root, "方案"))
        self.assertEqual(specs.proposed(self.root, "方案"), "")

    def test_kind_is_guessed_from_the_words_and_the_checklist_is_read(self):
        self.assertEqual(specs.guess("编写对话切换模型需求"), "需求文档")
        self.assertEqual(specs.guess("调研洞察卡新牌型"), "调研报告")
        self.assertEqual(specs.guess("告诉实习生 12 月安排"), "回复草稿")
        self.assertEqual(specs.checklist(SPEC), ["每条规则写了边界", "有验收标准"])
        for kind in specs.TYPES:
            self.assertTrue(specs.checklist(specs.expert(".", kind)), f"{kind} has a built-in expert view with a checklist")


class TheRound(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def result(self, **r):
        (self.dir / "结果.json").write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")

    def test_an_outline_round_hands_in_an_outline_and_forms_the_spec(self):
        specs.keep_plan(self.dir, {"kind": "需求文档", "outline": True, "form": True, "review": True, "checklist": ["有验收标准"]})
        self.result(outcome="done", template="需求文档", summary="x", sources=["a"], memory=[])
        self.assertIn("只交提纲", itemwork.check_result(self.dir, {})[1])
        self.result(outcome="outline", summary="提纲要点", question="按这个写吗？")
        self.assertIn("提纲.md", itemwork.check_result(self.dir, {})[1])
        (self.dir / "提纲.md").write_text("## 调研\n同类文档两份\n## 提纲\n1. 要解决什么问题：现在会员只能用默认模型，切换后的扣次没人说清楚\n2. 需求：入口、切换规则、倍率怎么显示、次数用完怎么办\n3. 验收标准\n## 要你定的\n无", encoding="utf-8")
        self.assertIn("规格.md", itemwork.check_result(self.dir, {})[1])
        (self.dir / "规格.md").write_text(SPEC, encoding="utf-8")
        self.assertIsNone(itemwork.check_result(self.dir, {})[1])

    def test_the_review_is_a_round_of_its_own_asked_for_after_the_draft(self):
        specs.keep_plan(self.dir, {"kind": "需求文档", "outline": False, "review": True, "check": True, "checklist": ["有验收标准"]})
        (self.dir / "交付.md").write_text("# 需求\n## 要解决什么问题\n" + "会员只能用默认模型。" * 20 + "\n## 需求\n1. 会员可切换\n## 用到的资料\n- 会议\n## 我做的假设\n无\n", encoding="utf-8")
        self.result(outcome="done", template="需求文档", summary="切换模型需求", sources=["会议"], memory=[])
        problem = itemwork.check_result(self.dir, {})[1]
        self.assertIn("资深产品经理", problem)
        self.assertIn("有验收标准", problem)
        (self.dir / "自审.md").write_text("## 自审\n- ✓ 有验收标准\n- 改了：补了次数用完的情况", encoding="utf-8")
        self.assertIsNone(itemwork.check_result(self.dir, {})[1])

    def test_the_round_is_told_what_to_do(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "state").mkdir()
            text = specs.work_text(d, specs.plan(d, "r", "需求文档"))
            for line in ("这一轮不写正文", "提纲.md", "规格.md", "资深产品经理会怎么做", '"outcome": "outline"'):
                self.assertIn(line, text)
            specs.form(d, "需求文档", SPEC)
            text = specs.work_text(d, {**specs.plan(d, "r", "需求文档", outline_done=True), "workdir": d})
            self.assertIn("本人这一类交付的规格", text)
            self.assertNotIn("资深产品经理会怎么做", text, "the expert view is not brought every time")


class BuiltIn(unittest.TestCase):
    def test_the_community_expert_prompts_ship_with_their_licences(self):
        from agent_matrix import promptfile
        with tempfile.TemporaryDirectory() as d:
            for kind in ("需求文档", "周报", "数据汇总"):
                text = promptfile.read(d, f"specs/{kind}.md")
                self.assertIn("## 参考：社区里的专家提示词（原文）", text, kind)
                self.assertIn("在心响里用时", text, kind)
            licences = promptfile.read(d, "specs/LICENSES.md")
            for name in ("snarktank/ai-dev-tasks", "anthropics/skills", "wshobson/agents", "op7418/Humanizer-zh", "Apache License"):
                self.assertIn(name, licences)

    def test_the_review_brings_the_writing_checklist_into_the_folder(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d, "w"); work.mkdir()
            self.assertTrue(specs.writing_check(d, work))
            self.assertIn("AI", (work / specs.WRITING).read_text(encoding="utf-8"))
            self.assertIn(specs.WRITING, specs.review_request({"kind": "方案", "checklist": []}))

if __name__ == "__main__":
    unittest.main()


class HandIn(unittest.TestCase):
    def test_an_outline_stops_the_item_for_the_owner_and_the_first_spec_is_kept(self):
        from agent_matrix import drive
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root / "state").mkdir()
            folder = root / "work"; folder.mkdir()
            specs.keep_plan(folder, {"kind": "需求文档", "outline": True, "form": True, "review": True, "checklist": []})
            (folder / "提纲.md").write_text("## 调研\n找到两份同类需求文档\n## 提纲\n1. 要解决什么问题：会员切换模型后扣次没说清楚\n2. 需求：入口和规则\n3. 验收\n## 要你定的\n无", encoding="utf-8")
            (folder / "规格.md").write_text(SPEC, encoding="utf-8")
            (folder / "结果.json").write_text(json.dumps({"outcome": "outline", "summary": "提纲三节", "question": "按这个写吗？"}, ensure_ascii=False), encoding="utf-8")
            job = drive._open(root, {"id": "item-r1", "kind": "item", "title": "t", "folder": str(folder), "n": 1,
                                     "ref": {"record_id": "r1", "version": "a" * 64}, "output": [], "instructions": "x"}, "本机 GPT")
            with patch("agent_matrix.drive._item") as item, patch("agent_matrix.drive._row_of", return_value={}):
                item.return_value.workdir, item.return_value.n = folder, 1
                out = drive.submit(root, None, job["id"])
                self.assertEqual((out["status"], out.get("outline")), ("blocked", True), out)
                item.return_value.block.assert_called_once()
                self.assertIn("提纲好了", item.return_value.block.call_args[0][0])
                item.return_value.publish.assert_not_called()
            self.assertIn("## 检查清单", specs.text(root, "需求文档"))
            self.assertEqual(specs.state(root, "需求文档")["state"], "draft")
