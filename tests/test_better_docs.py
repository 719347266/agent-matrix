"""AI 交的文档：备材料带上原来那件事的会和原话；需求文档按团队的写法；交给 AI 的说明原样留下，本人能看。"""
import json
from pathlib import Path
import tempfile
import unittest

from agent_matrix import background, itemwork, promptfile

PARENT = {"record_id": "p1", "编号": "107", "标题": "调研洞察卡新牌型", "来源": ["会议"], "下一步动作": "调研可新增的牌型，再定框架",
          "来源链接": "https://applink.feishu.cn/client/vctab/open?meetingId=m1", "去重键": "meeting:m1:ab",
          "原话引用": '[02:27:44] <cite type="user" uid-ref="u2" user-name="杨奕菲"></cite> 02:28:10  \n\n\n\n不一定非得加塔罗'}
CHILD = {"record_id": "c1", "编号": "108", "标题": "备材料：调研洞察卡新牌型", "来源": ["手动"], "父事项": ["p1"],
         "完成标准": "能直接用于：调研洞察卡新牌型"}


class PrepWorksFromItsParent(unittest.TestCase):
    def test_a_prep_item_takes_its_parents_meeting_and_words(self):
        row = background.with_parent_source(CHILD, [PARENT, CHILD])
        self.assertEqual(row["来源链接"], PARENT["来源链接"])
        self.assertEqual(row["去重键"], PARENT["去重键"])
        self.assertEqual(row["record_id"], "c1", "still its own item")
        own = {**CHILD, "原话引用": "自己的原话"}
        self.assertIs(background.with_parent_source(own, [PARENT, own]), own, "an item with words of its own keeps them")

    def test_the_order_says_what_prep_means_and_drops_the_circular_done_line(self):
        sheet = background.task_sheet(CHILD, [PARENT, CHILD])
        self.assertIn("杨奕菲 02:28:10", sheet)
        self.assertIn("不一定非得加塔罗", sheet)
        self.assertNotIn("<cite", sheet, "who said it, without the markup")
        self.assertIn("那件事由本人亲自做", sheet)
        self.assertNotIn("能直接用于", sheet, "「能直接用于：标题」 says nothing")

    def test_markup_and_blank_lines_go_from_a_transcript(self):
        self.assertEqual(background.plain_quote('<cite type="user" user-name="田沐"></cite> 02:23:40  \n\n\n没有。'), "田沐 02:23:40  \n没有。")


class Documents(unittest.TestCase):
    def check(self, template, doc):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "交付.md").write_text(doc, encoding="utf-8")
            (Path(d) / "结果.json").write_text(json.dumps({"outcome": "done", "template": template, "summary": "需求稿", "sources": ["会议"],
                                                        "memory": []}, ensure_ascii=False), encoding="utf-8")
            return itemwork.check_result(d, {})[1]

    def test_a_requirements_document_is_a_type_of_its_own_with_room_to_say_things(self):
        body = "现在会员只能用默认模型。" * 120   # ~1300 字：方案 used to stop at 1200
        doc = f"# 示例 App｜对话｜切换模型｜需求\n\n## 要解决什么问题\n{body}\n\n## 需求\n1. 会员可以切换。\n\n## 用到的资料\n- 会议\n\n## 我做的假设\n无\n"
        self.assertIsNone(self.check("需求文档", doc))
        self.assertIsNone(self.check("需求文档", doc.replace("## 要解决什么问题", "## 背景")), "either opening will do: teams name it differently")
        self.assertIn("要解决什么问题或背景", self.check("需求文档", doc.replace("## 要解决什么问题", "## 说明")))

    def test_the_prompt_asks_for_the_teams_own_format_full_sentences_and_nothing_made_up(self):
        worker = promptfile.read(".", "worker.md")
        for line in ("先找同类，再动笔", "读者没参加那场会", "逐条落进正文", "不编造", "用完整的句子写", "需求文档的第一节就是「要解决什么问题」"):
            self.assertIn(line, worker)
        self.assertNotIn("能用表格就不用段落", worker)
        self.assertIn("需求文档", itemwork.first_prompt(Path("."), {"编号": 1, "标题": "x"}))


class ThePromptIsKept(unittest.TestCase):
    def test_what_the_ai_was_given_is_kept_for_the_owner_to_read(self):
        from agent_matrix import drive
        with tempfile.TemporaryDirectory() as d:
            drive.keep_prompt(d, {"kind": "item", "ref": {"record_id": "rec1"}, "instructions": "全部说明", "builtin_prompt": "本机 GPT 收到的"})
            self.assertEqual(drive.prompt_path(d, "rec1").read_text(encoding="utf-8"), "本机 GPT 收到的")
            drive.keep_prompt(d, {"kind": "triage", "ref": {}, "instructions": "x"})
            self.assertEqual(len(list((Path(d) / "state/prompts").iterdir())), 1, "only items")


if __name__ == "__main__":
    unittest.main()


class WorkUsesMemory(unittest.TestCase):
    """The brief carries the owner's memory that bears on the work, picked in code: not left to the AI to go and look."""
    def setUp(self):
        from agent_matrix import library, memory
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "state").mkdir()
        (self.root / "config.yaml").write_text("owner_name: 王小明\n", encoding="utf-8")
        library.folder(self.root).mkdir(parents=True)
        (library.folder(self.root) / ".migrated").write_text("x", encoding="utf-8")
        library.save(self.root, library.GENERAL, library.render(library.GENERAL, "每件事动手前都读。", "## 交付先给结论\n先给结论。"))
        library.save(self.root, "写需求经验", library.render("写需求经验", "要写需求文档、会员规则时打开。", "## 会员规则先查现有权益文档\n先查。"))
        library.save(self.root, "发 PR 经验", library.render("发 PR 经验", "要发 PR、通知 reviewer 时打开。", "## 从 main 切分支\n切。"))
        db = memory._db(self.root)
        with db:
            for sid, title, kind, text in (("m1", "周会", "decision", "会员每天对话次数按档位给，Pro 每天 100 句"),
                                           ("m1", "周会", "decision", "下周上线小组件"), ("m2", "本次会", "decision", "切换模型排入 2.4")):
                db.execute("INSERT INTO moments (source, source_id, source_title, at, kind, text, quote, stamp, wait_for, due, status, created) "
                           "VALUES ('meeting',?,?, '2026-11-20T10:00:00', ?, ?, '', '', NULL, NULL, 'open', '2026-11-20')", (sid, title, kind, text))
        db.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_general_rules_and_the_experience_on_this_kind_of_work_come_in_full(self):
        from agent_matrix import eventbrief
        got = "\n".join(eventbrief._experience(self.root, "写会员切换模型的需求文档"))
        self.assertIn("先给结论", got)
        self.assertIn("会员规则先查现有权益文档", got)
        self.assertNotIn("从 main 切分支", got, "experience about other work stays in the index")
        self.assertNotIn("\n## ", got, "the brief's own headings are not broken by the memory's")

    def test_what_earlier_meetings_settled_about_the_same_thing_comes_along(self):
        from agent_matrix import eventbrief
        got = eventbrief._earlier(self.root, "m2", "会员对话次数和切换模型的规则")
        self.assertTrue(any("Pro 每天 100 句" in g and "周会" in g for g in got), got)
        self.assertFalse(any("小组件" in g for g in got), "not about this")
        self.assertFalse(any("排入 2.4" in g for g in got), "this meeting's own is in 会上已经定下的")
