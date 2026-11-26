"""新进来 groups and 按人: where an item came from and who it is about. 看起来办完了: only with words that are there."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from agent_matrix import donecheck, people
from agent_matrix.timeutil import TZ

SELF = {"我", "本人", "王小明"}
CHAT = "https://applink.feishu.cn/client/chat/open?openChatId="
MEETING = {"m1": {"topic": "观己需求对齐会", "at": "2026-11-25 14:00"}}
CHATS = {"oc_p2p": {"name": "陈娈娇", "mode": "p2p"}, "oc_grp": {"name": "曼朗外部群", "mode": "group", "external": True},
         "oc_team": {"name": "产品组", "mode": "group"}}


class Origin(unittest.TestCase):
    def test_meeting_topics_come_without_the_date_in_front(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "state").mkdir()
            db = sqlite3.connect(Path(d) / "state/state.db")
            db.execute("CREATE TABLE meeting_queue (meeting_id TEXT, metadata TEXT)")
            db.execute("CREATE TABLE chat_candidates (chat_id TEXT, chat_name TEXT, chat_mode TEXT)")
            db.execute("CREATE TABLE kv (key TEXT, value TEXT)")
            db.execute("INSERT INTO meeting_queue VALUES ('m9', ?)", (json.dumps({"topic": "11-25 | 观己需求对齐会", "start_time": "2026-11-25 14:01"}),))
            db.execute("INSERT INTO kv VALUES ('chats:external', '[\"oc_x\"]')")
            db.commit(); db.close()
            meetings, chats = people.known(d)
            self.assertEqual(meetings["m9"]["topic"], "观己需求对齐会")
            self.assertTrue(chats["oc_x"]["external"])

    def origin(self, row, url, tags=None):
        return people.origin(row, url, MEETING, CHATS, SELF, tags)

    def test_a_meeting_item_is_grouped_under_the_meeting_it_came_from(self):
        o = self.origin({"来源": ["会议"]}, "https://applink.feishu.cn/client/vctab/open?meetingId=m1")
        self.assertEqual((o["kind"], o["group"], o["person"], o["at"]), ("会议", "观己需求对齐会", None, "2026-11-25 14:00"))

    def test_in_a_private_chat_the_other_person_is_who_it_is_about_even_when_the_owner_spoke(self):
        said = self.origin({"来源": ["聊天"], "原话引用": "[陈娈娇 2026-11-26 09:45 陈娈娇] 你直接跟她说"}, CHAT + "oc_p2p")
        mine = self.origin({"来源": ["聊天"], "原话引用": "[陈娈娇 2026-11-26 09:50 我] 好的我来"}, CHAT + "oc_p2p")
        self.assertEqual((said["person"], mine["person"], said["at"]), ("陈娈娇", "陈娈娇", "2026-11-26 09:45"))

    def test_in_a_group_it_is_the_speaker_and_an_external_group_is_a_client(self):
        o = self.origin({"来源": ["聊天"], "原话引用": "[曼朗外部群 2026-11-26 10:00 王客户] 发票开一下"}, CHAT + "oc_grp")
        self.assertEqual((o["group"], o["person"], o["client"]), ("曼朗外部群", "王客户", True))
        own = self.origin({"来源": ["聊天"], "原话引用": "[产品组 2026-11-26 10:00 王小明] 我来改"}, CHAT + "oc_team")
        self.assertEqual((own["group"], own["person"], own["client"]), ("产品组", None, False))

    def test_wechat_is_a_client_until_the_owner_says_otherwise(self):
        row = {"来源": ["聊天"], "原话引用": "[李老板 2026-11-26 10:00 李老板] 方案呢"}
        self.assertTrue(self.origin(row, None)["client"])
        self.assertFalse(self.origin(row, None, {"李老板": "同事"})["client"])
        self.assertTrue(self.origin({"来源": ["会议"]}, "x?meetingId=m1", {"观己需求对齐会": "客户"})["client"])

    def test_the_owners_word_is_kept_and_can_be_taken_back(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "state").mkdir()
            self.assertEqual(people.set_kind(d, " 王客户 ", "客户"), {"王客户": "客户"})
            self.assertEqual(people.set_kind(d, "王客户", ""), {})
            with self.assertRaises(ValueError):
                people.set_kind(d, "王客户", "朋友")


class DoneCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "state").mkdir()
        self.now = datetime(2026, 11, 26, 12, 0, tzinfo=TZ)

    def tearDown(self):
        self.tmp.cleanup()

    def row(self, rid, **kw):
        return {"record_id": rid, "执行方": ["我"], "状态": ["未开始"], "创建时间": "2026-11-25T10:00:00+08:00", **kw}

    def test_only_the_owners_open_items_and_not_again_too_soon(self):
        rows = [self.row("a"), self.row("b", 执行方=["AI"]), self.row("c", 状态=["完成"]), self.row("d", 排除本人提醒=True),
                self.row("e", 创建时间=(self.now - timedelta(minutes=5)).isoformat())]
        self.assertEqual([r["record_id"] for r in donecheck.due(self.root, rows, self.now)], ["a"])
        donecheck.mark_checked(self.root, ["a"], self.now)
        later = lambda h: [r["record_id"] for r in donecheck.due(self.root, rows, self.now + timedelta(hours=h))]
        self.assertEqual(later(1), ["e"], "a looks again only after a while; e is old enough by then")
        self.assertEqual(later(7), ["e", "a"], "never checked first")

    def test_words_it_did_not_see_and_cannot_link_to_are_a_guess(self):
        item = {"record_id": "a", "after": ["[11-25 14:02 李瑞轩] 返回按钮加上了，已经发到测试包"]}
        self.assertIsNone(donecheck.accept(self.root, item, {"done": True, "evidence": "应该已经加好了"}, self.now))
        self.assertIsNone(donecheck.accept(self.root, item, {"done": False, "evidence": "返回按钮加上了"}, self.now))
        kept = donecheck.accept(self.root, item, {"done": True, "evidence": "[11-25 14:02 李瑞轩] 返回按钮加上了", "where": "私聊"}, self.now)
        self.assertEqual(kept["where"], "私聊")
        doc = donecheck.accept(self.root, {"record_id": "b", "after": []},
                               {"done": True, "evidence": "第一期投放计划", "url": "https://x.feishu.cn/docx/1"}, self.now)
        self.assertEqual(doc["url"], "https://x.feishu.cn/docx/1")

    def test_not_done_is_remembered_and_the_same_words_are_not_offered_again(self):
        item = {"record_id": "a", "after": ["[11-25 14:02 李瑞轩] 返回按钮加上了"]}
        verdict = {"done": True, "evidence": "返回按钮加上了"}
        donecheck.accept(self.root, item, verdict, self.now)
        self.assertEqual(donecheck.due(self.root, [self.row("a")], self.now), [], "waiting on the owner's tap")
        self.assertTrue(donecheck.dismiss(self.root, "a"))
        self.assertFalse(donecheck.dismiss(self.root, "a"))
        self.assertIsNone(donecheck.accept(self.root, item, verdict, self.now))
        self.assertTrue(donecheck.hints(self.root)["a"]["dismissed"])

    def test_the_job_keeps_only_real_words_and_marks_every_item_looked_at(self):
        from agent_matrix import drive
        folder = self.root / "job"
        folder.mkdir()
        items = [{"n": 1, "record_id": "a", "title": "加返回按钮", "after": ["[11-25 14:02 李瑞轩] 返回按钮加上了"]},
                 {"n": 2, "record_id": "b", "title": "排期", "after": []}]
        (folder / "donecheck.json").write_text(json.dumps([{"n": 1, "done": True, "evidence": "返回按钮加上了"},
                                                           {"n": 2, "done": True, "evidence": "排好了"}], ensure_ascii=False), encoding="utf-8")
        job = drive._open(self.root, {"id": "donecheck-1", "kind": "donecheck", "title": "t", "folder": str(folder),
                                      "ref": {"items": items}, "output": ["donecheck.json"], "instructions": "x"}, "本机 GPT")
        out = drive.submit(self.root, None, job["id"])
        self.assertEqual((out["status"], out["done"]), ("done", [1]))
        self.assertEqual(set(donecheck._get(self.root, donecheck.CHECKED)), {"a", "b"})
        self.assertNotIn("b", donecheck.hints(self.root))


if __name__ == "__main__":
    unittest.main()
