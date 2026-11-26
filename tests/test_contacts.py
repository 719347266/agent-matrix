"""通讯录: everyone the owner deals with, found in the local records; what 心响 understands about each, written only
from what was said, kept, and brought into the work."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from agent_matrix import contacts, eventbrief, people
from agent_matrix.timeutil import TZ

SELF = {"我", "本人", "王小明"}
TRANSCRIPT = """# 文字记录：周会

<cite type="user" uid-ref="u1" user-name="杨奕菲"></cite> 00:00:52
那我们开始吧，先过一下 2.1 的版本。

<cite type="user" uid-ref="u2" user-name="王小明"></cite> 00:01:30
日历做完了，我晚点贴一下需求。

说话人 1 用 <cite type="user" uid-ref="u3" user-name="许佳婷"></cite> 的设备 00:02:10
今天上午走查完。

<cite type="user" uid-ref="u1" user-name="杨奕菲"></cite> 00:03:00
需求先磨清楚再排。
"""
GOOD = "## 是谁\n- 产品负责人，管版本排期（11-25 周会）。\n\n## 在意什么\n- 需求先磨清楚再排进版本（11-25 周会）。"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "state/meeting-transcripts/m1").mkdir(parents=True)
        (self.root / "state/meeting-transcripts/m1/transcript.md").write_text(TRANSCRIPT, encoding="utf-8")
        db = sqlite3.connect(self.root / "state/state.db")
        db.execute("CREATE TABLE meeting_queue (meeting_id TEXT, metadata TEXT)")
        db.execute("CREATE TABLE chat_candidates (message_id TEXT, chat_id TEXT, chat_name TEXT, chat_mode TEXT, sender_id TEXT,"
                   " sender_name TEXT, sent_at TEXT, text TEXT, context TEXT, kind TEXT, status TEXT, batch TEXT)")
        db.execute("CREATE TABLE kv (key TEXT, value TEXT)")
        db.execute("INSERT INTO meeting_queue VALUES ('m1', ?)", (json.dumps({"topic": "11-25 | 周会", "start_time": "2026-11-25 10:00",
                                                                              "source_url": "https://x.test/m1"}),))
        chats = [("oc_p", "陈娈娇", "p2p", "陈娈娇", "2026-11-25 09:45", "你直接跟她说一声就行"),
                 ("oc_p", "陈娈娇", "p2p", "我", "2026-11-25 09:50", "好的我来说"),
                 ("oc_g", "产品组", "group", "杨奕菲", "2026-11-26 12:18", "2.2 明天测"),
                 ("oc_x", "曼朗外部群", "group", "王客户", "2026-11-24 10:00", "报价什么时候给")]
        for k, c in enumerate(chats):
            db.execute("INSERT INTO chat_candidates (message_id, chat_id, chat_name, chat_mode, sender_name, sent_at, text) VALUES (?,?,?,?,?,?,?)", (f"m{k}", *c))
        db.execute("INSERT INTO kv VALUES ('chats:external', '[\"oc_x\"]')")
        db.commit()
        db.close()
        self.now = datetime(2026, 11, 26, 15, 0, tzinfo=TZ)

    def tearDown(self):
        self.tmp.cleanup()

    def person(self, name):
        return next(p for p in contacts.directory(self.root, SELF) if p["name"] == name)


class Directory(Base):
    def test_everyone_dealt_with_is_found_in_chats_and_meetings_and_the_owner_is_not(self):
        found = {p["name"]: p for p in contacts.directory(self.root, SELF, extra=["李瑞轩"])}
        self.assertEqual(set(found), {"陈娈娇", "杨奕菲", "王客户", "许佳婷", "李瑞轩"})
        self.assertEqual(found["陈娈娇"]["messages"], 2, "in a private chat the owner's words count for the other person")
        self.assertEqual((found["杨奕菲"]["messages"], found["杨奕菲"]["meetings"], found["杨奕菲"]["chats"]), (1, ["m1"], ["产品组"]))
        self.assertEqual(found["许佳婷"]["meetings"], ["m1"], "on their device: they were there")
        self.assertEqual((found["王客户"]["kind"], found["陈娈娇"]["kind"]), ("客户", "同事"), "an external chat is a client")
        people.set_kind(self.root, "陈娈娇", "客户")
        self.assertEqual((self.person("陈娈娇")["kind"], self.person("陈娈娇")["guessed"]), ("客户", False))

    def test_meetings_list_who_spoke_and_the_words_of_an_unnamed_speaker_are_nobodys(self):
        m = contacts.meetings(self.root)[0]
        self.assertEqual((m["topic"], m["people"], m["url"]), ("周会", ["杨奕菲", "王小明", "许佳婷"], "https://x.test/m1"))
        said = contacts._speech(self.root / "state/meeting-transcripts/m1/transcript.md")
        self.assertIn(("", "今天上午走查完。"), said)
        self.assertNotIn(("许佳婷", "今天上午走查完。"), said)

    def test_who_said_the_words_a_meeting_item_came_from(self):
        self.assertEqual(contacts.speaker_at(self.root, "m1", "[00:03:00] 需求先磨清楚再排。"), "杨奕菲")
        self.assertEqual(contacts.speaker_at(self.root, "m1", "[00:02:10] 今天上午走查完。"), "", "someone on another's device")
        self.assertEqual(contacts.speaker_at(self.root, "../x", "[00:03:00] x"), "")


class Understanding(Base):
    def test_who_is_due_enough_dealings_never_written_or_much_new_or_asked(self):
        ppl = contacts.directory(self.root, SELF)
        self.assertEqual([p["name"] for p in contacts.due(self.root, ppl, self.now)], [], "too little to write about anyone yet")
        contacts.ask(self.root, "陈娈娇", self.now)
        self.assertEqual([p["name"] for p in contacts.due(self.root, ppl, self.now)], ["陈娈娇"], "the owner asked")
        busy = {**self.person("杨奕菲"), "messages": 30}
        self.assertEqual([p["name"] for p in contacts.due(self.root, [busy], self.now)], ["杨奕菲"])
        self.assertIsNone(contacts.accept(self.root, busy, GOOD, self.now))
        self.assertEqual(contacts.due(self.root, [busy], self.now + timedelta(days=5)), [], "nothing new since")
        more = {**busy, "messages": 40}
        self.assertEqual(contacts.due(self.root, [more], self.now + timedelta(days=1)), [], "not again so soon")
        self.assertEqual(len(contacts.due(self.root, [more], self.now + timedelta(days=4))), 1)
        contacts.mark_tried(self.root, ["杨奕菲"], self.now + timedelta(days=4))
        self.assertEqual(contacts.due(self.root, [more], self.now + timedelta(days=4, hours=1)), [], "a round that did not come back waits")

    def test_what_the_ai_reads_and_what_it_hands_in(self):
        contacts.set_note(self.root, "杨奕菲", "产品负责人，要结论先行")
        text = contacts.evidence(self.root, self.person("杨奕菲"), [{"编号": "93", "标题": "验收杨奕菲的 2.1 需求", "状态": ["未开始"]}])
        for part in ("本人写的（以这里为准）", "要结论先行", "#93 验收杨奕菲的 2.1 需求", "需求先磨清楚再排。", "[2026-11-26 12:18 群「产品组」 杨奕菲] 2.2 明天测"):
            self.assertIn(part, text)
        self.assertNotIn("今天上午走查完", text, "not someone else's words")
        self.assertIn("是谁", contacts.check("## 在意什么\n很多字很多字很多字很多字很多字很多字很多字很多字很多字很多字很多字"))
        self.assertIsNotNone(contacts.check("## 是谁\n短"))
        self.assertIsNone(contacts.accept(self.root, self.person("杨奕菲"), "# 杨奕菲\n\n" + GOOD, self.now))
        self.assertEqual(contacts.understanding(self.root, "杨奕菲").count("# 杨奕菲"), 1)
        self.assertTrue(self.person("杨奕菲")["understood"])

    def test_the_job_keeps_what_holds_up_and_asks_for_a_fix_once(self):
        from agent_matrix import drive
        folder = self.root / "job"
        folder.mkdir()
        picked = [self.person("杨奕菲"), self.person("陈娈娇")]
        (folder / "people.json").write_text(json.dumps([{"name": "杨奕菲", "text": GOOD}, {"name": "陈娈娇", "text": "## 是谁\n短"},
                                                        {"name": "陌生人", "text": GOOD}], ensure_ascii=False), encoding="utf-8")
        job = drive._open(self.root, {"id": "people-1", "kind": "people", "title": "t", "folder": str(folder), "ref": {"people": picked},
                                      "output": ["people.json"], "instructions": "x"}, "本机 GPT")
        out = drive.submit(self.root, None, job["id"])
        self.assertEqual(out["status"], "fix")
        self.assertIn("陈娈娇", out["problem"])
        self.assertTrue(contacts.understanding(self.root, "杨奕菲"), "the good one is kept already")
        self.assertFalse((self.root / "state/people/陌生人.md").exists(), "only the people it was given")
        (folder / "people.json").write_text(json.dumps([{"name": "杨奕菲", "text": GOOD}]), encoding="utf-8")
        self.assertEqual(drive.submit(self.root, None, job["id"])["status"], "done")


class InTheWork(Base):
    def test_the_brief_brings_who_said_it_and_who_is_named_with_the_owners_words_first(self):
        contacts.accept(self.root, {**self.person("杨奕菲"), "messages": 30}, GOOD, self.now)
        contacts.set_note(self.root, "许佳婷", "测试，负责走查")
        who = eventbrief._people(self.root, "[产品组 2026-11-26 12:18 杨奕菲] 2.2 明天测", "让许佳婷走查 2.2；杨奕菲在等")
        self.assertEqual([w.splitlines()[0] for w in who], ["### 杨奕菲", "### 许佳婷"])
        self.assertIn("本人写的：测试，负责走查", who[1])
        self.assertIn("#### 是谁", who[0], "their headings sit under the person")
        self.assertEqual(eventbrief._people(self.root, "", "和陈娈娇说一声"), [], "nothing on them: nothing brought")


if __name__ == "__main__":
    unittest.main()
