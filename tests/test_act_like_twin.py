"""Doing an item the way 数字分身 does: check first whether it is still wanted, fetch the source conversation on demand."""
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest

from agent_matrix.background import CONTEXT_MAX_HOURS, context, source_pointer
from agent_matrix.itemwork import check_result
from agent_matrix.timeutil import TZ


class HandledTests(unittest.TestCase):
    def check(self, result):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "结果.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            return check_result(tmp, None)[1]

    def test_handled_needs_the_line_that_shows_it(self):
        self.assertIsNone(self.check({"outcome": "handled", "evidence": "[11-20 15:02 本人] 已经加好了", "reason": "本人已加"}))
        self.assertIn("evidence", self.check({"outcome": "handled", "reason": "应该办完了"}))
        self.assertIn("handled", self.check({"outcome": "maybe"}))


class ContextTests(unittest.TestCase):
    def test_meeting_window_widens_and_says_how_to_go_further(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "state/meeting-transcripts/m1"
            folder.mkdir(parents=True)
            (folder / "transcript.md").write_text("\n".join(f"[00:{i:02d}] 第 {i} 行" for i in range(400)), encoding="utf-8")
            row = {"去重键": "meeting:m1:abc", "原话引用": "[00:200] 第 200 行", "标题": "x"}
            now = datetime(2026, 11, 24, 12, tzinfo=TZ)
            narrow, wide = context(root, None, row, now, 3), context(root, None, row, now, 12)
            self.assertIn("第 200 行", narrow)
            self.assertNotIn("第 100 行", narrow)
            self.assertIn("第 100 行", wide)   # 12 h reads 4x the lines around the promise
            self.assertIn("--hours 改成 6", narrow)
            self.assertIn("上限", context(root, None, row, now, 999))
            self.assertIn(f"前 {CONTEXT_MAX_HOURS} 小时", context(root, None, row, now, 999))

    def test_pointer_saves_the_source_next_to_the_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            lines = source_pointer(tmp, {"标题": "加共享人", "来源链接": "https://x.slack.com/archives/C1/p1700000000000100"}, tmp)
            self.assertEqual(json.loads((Path(tmp) / "来源.json").read_text(encoding="utf-8"))["标题"], "加共享人")
            self.assertIn("context --source", lines[0])



class RulesUsedTests(unittest.TestCase):
    def test_a_pass_counts_for_the_rules_the_ai_said_it_followed(self):
        from agent_matrix import memory
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "state/work/r1").mkdir(parents=True)
            memory.add_rule(root, "查线上数据先核表结构")
            rid = next(r["id"] for r in memory.rules(root) if r["rule"] == "查线上数据先核表结构")
            (root / "state/work/r1/结果.json").write_text(json.dumps({"template": "数据汇总", "rules": [rid, 999]}), encoding="utf-8")
            memory.record_outcome(root, "r1", "要改", "少了测试店铺")   # a send-back may be about something else: not counted here
            memory.record_outcome(root, "r1", "通过")
            rule = next(r for r in memory.rules(root) if r["id"] == rid)
            self.assertEqual((rule["wins"], rule["losses"]), (1, 0))
            self.assertEqual([o["rules"] for o in memory.outcomes(root)], [[rid, 999], [rid, 999]])



class RecallByActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "state").mkdir()
        self.folder = self.root / "job"
        self.folder.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_few_action_rules_are_shown_grouped_many_go_to_a_file_behind_an_index(self):
        from agent_matrix import memory
        memory.add_rule(self.root, "交付一律用中文")
        memory.add_rule(self.root, "统计商家排除开发店", trigger="查线上数据时")
        text = memory.rules_text(self.root, None, self.folder)
        self.assertIn("#### 查线上数据", text)                      # "时" is dropped so the same action groups together
        self.assertIn("统计商家排除开发店", text)
        self.assertFalse((self.folder / "规矩.md").exists())
        for k in range(memory.INLINE_MAX):
            memory.add_rule(self.root, f"发 PR 规矩{k}", trigger="发 PR")
        text = memory.rules_text(self.root, None, self.folder)
        self.assertIn("交付一律用中文", text)                         # rules for every job stay in full
        self.assertNotIn("统计商家排除开发店", text)                   # action rules: only the index
        self.assertIn(f"- 发 PR · {memory.INLINE_MAX}", text)
        self.assertIn("## 查线上数据\n- [", (self.folder / "规矩.md").read_text(encoding="utf-8"))
        self.assertIn("统计商家排除开发店", memory.rules_text(self.root, None, self.root / "missing"))  # no folder: all in full

    def test_tidy_puts_a_rule_under_an_action_only_when_the_owner_says_use(self):
        from agent_matrix import memory, tidy
        memory.add_rule(self.root, "统计商家排除开发店")
        rid = next(r["id"] for r in memory.rules(self.root) if r["rule"] == "统计商家排除开发店")
        plan = {"changes": [{"type": "rule_trigger", "id": rid, "trigger": "查线上数据", "why": "只在查数据时用得上"},
                            {"type": "rule_trigger", "id": rid, "trigger": "", "why": "没变"}]}
        proposals, problems = tidy.check(self.root, plan)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(len(problems), 1)
        self.assertEqual(next(r for r in memory.rules(self.root) if r["id"] == rid)["trigger"], "")   # nothing until 用
        self.assertEqual(tidy._apply(self.root, "rule_trigger", proposals[0][3]), "")
        self.assertEqual(next(r for r in memory.rules(self.root) if r["id"] == rid)["trigger"], "查线上数据")


if __name__ == "__main__":
    unittest.main()
