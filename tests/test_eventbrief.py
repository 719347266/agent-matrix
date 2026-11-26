"""A piece of work handed to an AI comes as one brief that stands on its own: nothing to dig for."""
from pathlib import Path
import tempfile
import unittest

from agent_matrix import eventbrief, memory, thread

MID = "7000000000000000009"
QUOTE = ('[00:10:05] <cite type="user" uid-ref="u2" user-name="张总"></cite> 00:10:05  \n要不小王先定个方案吧。\n\n\n\n'
         '<cite type="user" uid-ref="u4" user-name="王小明"></cite> 00:10:20  \n好，我看看。')


def item(**fields):
    base = {"record_id": "rec1", "编号": 7, "标题": "定积分奖励方案", "下一步动作": "王小明拟定积分奖励方案，和李四确认",
            "完成标准": "各档数额已确认", "截止日": "2026-11-25T00:00:00.000+08:00", "来源": "会议",
            "来源链接": f"https://example.test/open?meetingId={MID}", "原话引用": QUOTE, "状态": ["进行中"]}
    return {**base, **fields}


class Brief(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "state/meeting-transcripts" / MID).mkdir(parents=True)
        (self.root / "state/meeting-transcripts" / MID / "transcript.md").write_text(
            '<cite user-name="张总"></cite> 00:10:05\n要不小王先定个方案吧。\n'
            '<cite user-name="王小明"></cite> 00:10:20\n好，我看看。\n'
            '<cite user-name="张总"></cite> 00:11:00\n前面不给，做完第一个任务给一点，越往后给越多。\n'
            '<cite user-name="李四"></cite> 00:30:00\n首页换个颜色。\n', encoding="utf-8")
        memory._insert(self.root, "meeting", MID, "周会", "2026-11-23T10:00:00+08:00", [
            ("requirement", "前面不给积分，第一个任务给一点，越往后给越多", "前面不给", "00:11:00", None, None),
            ("decision", "积分奖励跟下个版本一起上线", "一起上", "00:13:00", None, None),
            ("decision", "首页换成蓝色", "换个颜色", "00:30:00", None, None),             # same meeting, other topic, far away
            ("action", "定积分奖励方案", "先定个方案", "00:10:05", None, None)])        # the item itself: not repeated

    def build(self, row):
        return eventbrief.build(self.root, row["record_id"], [row])

    def test_the_brief_carries_what_was_settled_with_who_said_it_and_keeps_speakers_in_the_quote(self):
        text = self.build(item())
        self.assertIn("要求：前面不给积分，第一个任务给一点，越往后给越多（张总 00:11:00）", text)
        self.assertIn("定了：积分奖励跟下个版本一起上线", text)
        self.assertNotIn("首页换成蓝色", text)                 # far from the promise and about something else
        self.assertNotIn("- ：", text)
        self.assertIn("张总（00:10:05）：要不小王先定个方案吧。", text)
        self.assertIn("王小明（00:10:20）：好，我看看。", text)
        self.assertNotIn("<cite", text)
        self.assertIn("从头做", text)

    def test_the_owners_later_words_come_after_and_win_and_the_next_step_follows_the_state(self):
        thread.add(self.root, "rec1", "ai", "ask", "李四确认过数额吗？", at="2026-11-23T14:43:00+08:00")
        thread.add(self.root, "rec1", "me", "say", "按会上说的做就行", at="2026-11-23T15:07:00+08:00")
        text = self.build(item())
        self.assertIn("本人后来的要求（比上面都新，冲突时以这里为准）", text)
        self.assertIn("按会上说的做就行", text)
        self.assertIn("（本人已在后面回答）", text)
        self.assertIn("照本人的回答接着做完", text)
        thread.add_deliverable(self.root, "rec1", summary="第一档 3 颗", url="https://example.test/doc/1", at="2026-11-23T15:22:00+08:00")
        text = self.build(item(状态=["待验收"]))
        self.assertIn("已交第 1 版", text)
        self.assertIn("等本人验收：没有新要求就不要重做", text)    # the answer came before this version
        text = self.build(item(状态=["进行中"], 退回意见="第三档再多给一点"))
        self.assertIn("按退回意见逐条改", text)

    def test_handing_it_to_cindy_sends_the_brief_itself_not_a_file_to_go_and_read(self):
        from unittest.mock import patch
        from agent_matrix import cindy_pickup, local_sessions
        folder = self.root / "work"
        folder.mkdir()
        (self.root / "state/cindy-pickup.json").write_text('{"session": "s1", "schedule": "b1"}', encoding="utf-8")
        with patch("agent_matrix.itemwork.workdir_for", return_value=folder), \
             patch.object(eventbrief, "_row", lambda root, rid, rows=None: (item(), [item()])), \
             patch.object(local_sessions, "_gpt_said", return_value=[]):
            local_sessions.write_context(self.root, {"record_id": "rec1", "n": 7, "title": "定积分奖励方案"}, buddy=True)
            cindy_pickup.enqueue(self.root, {"session": "s1"}, {"record_id": "rec1", "n": 7, "title": "定积分奖励方案"}, folder, "")
        self.assertIn("会上已经定下的", (folder / "事件.md").read_text(encoding="utf-8"))
        self.assertIn("会上已经定下的", (folder / "AGENTS.md").read_text(encoding="utf-8"))
        from contextlib import closing
        with closing(cindy_pickup.db_for(self.root)) as db:
            message = db.execute("SELECT message FROM pickup").fetchone()[0]
        self.assertIn("前面不给积分", message)
        self.assertNotIn("先读", message)


if __name__ == "__main__":
    unittest.main()
