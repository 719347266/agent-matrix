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



class LibraryTests(unittest.TestCase):
    """经验库 the way 数字分身 keeps it: a topic per file, the index goes out, the AI only drafts, the owner says 用."""

    def setUp(self):
        from agent_matrix import library
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "state").mkdir()
        self.lib = library
        library.save(self.root, "发 PR 经验", "name：发 PR 经验\ndescription：要建分支、发 PR、通知 reviewer 时打开。\n\n"
                     "## 从 release 切分支\n不要从 dev 切。\n**Why:** 本人说过\n\n## 用 cw5-pr-notify 通知群\n发完 PR 通知 dev 群。\n**Why:** #12", None)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_index_goes_out_and_the_bodies_go_to_the_job_folder(self):
        job = self.root / "job"
        job.mkdir()
        text = self.lib.memory_text(self.root, job)
        self.assertIn("- 发 PR 经验 — 要建分支、发 PR、通知 reviewer 时打开。", text)
        self.assertNotIn("不要从 dev 切", text)
        self.assertIn("不要从 dev 切", (job / "记忆" / "发 PR 经验.md").read_text(encoding="utf-8"))
        self.assertIn("发 PR 经验", (job / "记忆" / "目录.md").read_text(encoding="utf-8"))

    def test_revise_is_matched_again_when_the_owner_says_use(self):
        draft = {"mode": "revise", "name": "发 PR 经验", "old_text": "不要从 dev 切。", "body": "不要从 dev、test 切。", "why": "本人补充"}
        _, _, data = self.lib.check_draft(self.root, draft)
        raw = self.lib.get(self.root, "发 PR 经验")["raw"]
        self.lib.save(self.root, "发 PR 经验", raw.replace("不要从 dev 切。", "严禁从 dev 切。"), "发 PR 经验")  # the owner edits it meanwhile
        self.assertIn("找不到", self.lib.apply_draft(self.root, data))       # not applied over the owner's edit
        self.assertIn("严禁从 dev 切", self.lib.get(self.root, "发 PR 经验")["body"])

    def test_drafts_that_would_break_the_library_are_refused(self):
        bad = [{"mode": "append", "name": "发 PR 经验", "body": "没有小节标题", "why": "w"},
               {"mode": "append", "name": "发 PR 经验", "body": "## 从 release 切分支\n重复的标题", "why": "w"},
               {"mode": "remove", "name": "发 PR 经验", "old_text": "**Why:**", "why": "w"},           # matches twice
               {"mode": "create", "name": "发 PR 经验", "description": "x", "body": "## a\nb", "why": "w"},
               {"mode": "append", "name": "没有这份", "body": "## a\nb", "why": "w"},
               {"mode": "append", "name": "发 PR 经验", "body": "## a\nb"}]                              # no why
        for d in bad:
            with self.assertRaises(ValueError, msg=d):
                self.lib.check_draft(self.root, d)

    def test_move_takes_a_whole_section_to_another_memory(self):
        section = "## 用 cw5-pr-notify 通知群\n发完 PR 通知 dev 群。\n**Why:** #12"
        _, _, data = self.lib.check_draft(self.root, {"mode": "move", "name": "发 PR 经验", "old_text": section, "to": "通知同事经验",
                                                       "description": "要在群里通知同事时打开。", "why": "通知不只发 PR 时用"})
        self.assertEqual(self.lib.apply_draft(self.root, data), "")
        self.assertEqual([t for t, _ in self.lib.get(self.root, "发 PR 经验")["sections"]], ["从 release 切分支"])
        self.assertEqual([t for t, _ in self.lib.get(self.root, "通知同事经验")["sections"]], ["用 cw5-pr-notify 通知群"])

    def test_a_verdict_keeps_which_memories_the_ai_said_it_used(self):
        from agent_matrix import memory
        (self.root / "state/work/r1").mkdir(parents=True)
        (self.root / "state/work/r1/结果.json").write_text(json.dumps({"template": "方案", "memory": ["发 PR 经验#从 release 切分支"]}),
                                                         encoding="utf-8")
        memory.record_outcome(self.root, "r1", "通过")
        self.assertEqual(memory.outcomes(self.root)[0]["memory"], ["发 PR 经验#从 release 切分支"])


if __name__ == "__main__":
    unittest.main()
