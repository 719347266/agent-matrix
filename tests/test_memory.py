"""记忆层: moments from meetings, waiting items that come back, recall, and the owner's rules."""
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from agent_matrix.meeting_review import Action, Grounded, Review, Waiting, repair_review, validate_review

T1 = ("00:10 老周：投流方案先保留，等周五的数据出来再定。\n"
      "00:20 老周：标签体系就按现在这版定了。\n00:30 小王：我来写投放复盘。")
T2 = "00:05 王总：投流方案上周说等数据，现在数据出来了，投流方案怎么定？"


def review(mid, text, **parts):
    return Review(meeting_id=mid, transcript_sha256=sha256(text.encode()).hexdigest(), **parts)


def line(text, n):
    return text.splitlines()[n]


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.first = review("m1", T1,
                            waiting=[Waiting(text="投流方案先保留", quote="投流方案先保留，等周五的数据出来再定", timestamp="00:10",
                                             reference=line(T1, 0), wait_for="周五的投放数据", due_date="2026-11-20", due_phrase="周五")],
                            decisions=[Grounded(text="标签体系按现在这版定了", quote="标签体系就按现在这版定了", timestamp="00:20",
                                                reference=line(T1, 1))],
                            actions=[Action(title="写投放复盘", text="写复盘", quote="我来写投放复盘", timestamp="00:30",
                                            reference=line(T1, 2), owner_name="小王", commitment_phrase="我来写", confidence=0.9)])

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_meeting_leaves_what_was_decided_and_what_waits(self):
        from agent_matrix import memory
        new = memory.record_meeting(self.root, self.first, {"meeting_id": "m1", "topic": "AI项目例会", "start_time": "2026-11-17 09:16"})
        self.assertEqual(sorted(m["kind"] for m in new), ["action", "decision", "waiting"])
        self.assertEqual(memory.record_meeting(self.root, self.first, {"meeting_id": "m1"}), [])   # stored once
        lines = memory.recall_lines(self.root, "投流方案怎么定")
        self.assertIn("等条件再定", lines[0])
        self.assertIn("周五的投放数据", lines[0])

    def test_a_waiting_item_comes_back_on_its_day_once(self):
        from agent_matrix import inbox, memory
        memory.record_meeting(self.root, self.first, {"meeting_id": "m1", "topic": "AI项目例会", "start_time": "2026-11-17 09:16"})
        self.assertEqual(memory.waiting_due(self.root, date(2026, 11, 19)), [])
        self.assertEqual(len(memory.waiting_due(self.root, date(2026, 11, 20))), 1)
        self.assertEqual(memory.waiting_due(self.root, date(2026, 11, 21)), [])          # told once
        msg = inbox.messages(self.root)[0]
        self.assertEqual(msg["kind"], "pending")
        self.assertIn("周五的投放数据", msg["title"] + json.dumps(msg["blocks"], ensure_ascii=False))

    def test_the_same_topic_in_a_new_meeting_brings_back_what_it_waited_for(self):
        from agent_matrix import inbox, memory
        memory.record_meeting(self.root, self.first, {"meeting_id": "m1", "topic": "AI项目例会", "start_time": "2026-11-17 09:16"})
        second = review("m2", T2, open_questions=[Grounded(text="投流方案怎么定", quote="投流方案怎么定", timestamp="00:05",
                                                            reference=line(T2, 0))])
        memory.record_meeting(self.root, second, {"meeting_id": "m2", "topic": "周会", "start_time": "2026-11-21 10:00"})
        recalls = [m for m in inbox.messages(self.root) if m["kind"] == "recall"]
        self.assertEqual(len(recalls), 1)
        self.assertIn("周五的投放数据", recalls[0]["summary"] + recalls[0]["title"])
        memory.record_meeting(self.root, second, {"meeting_id": "m2", "topic": "周会"})
        self.assertEqual(len([m for m in inbox.messages(self.root) if m["kind"] == "recall"]), 1)

    def test_rules_come_seeded_can_change_and_head_every_job(self):
        from agent_matrix import drive, memory
        self.assertTrue(any("不是本人负责" in r["rule"] for r in memory.rules(self.root)))
        memory.add_rule(self.root, "对外文案先给两版让我挑")
        rid = next(r["id"] for r in memory.rules(self.root) if r["rule"].startswith("对外文案"))
        job = drive._open(self.root, {"id": "x-1", "kind": "chat", "instructions": "做事。", "folder": "f"}, "Claude Code")
        self.assertTrue(job["instructions"].startswith("## 本人的规矩"))
        self.assertIn("对外文案先给两版让我挑", job["instructions"])
        memory.remove_rule(self.root, rid)
        self.assertFalse(any(r["rule"].startswith("对外文案") for r in memory.rules(self.root)))

    def test_learning_adds_at_most_three_rules_and_cannot_remove_the_owners(self):
        from agent_matrix import drive, memory
        folder = self.root / "learn"
        folder.mkdir()
        mine = memory.rules(self.root)[0]["id"]
        (folder / "learned.json").write_text(json.dumps({"add": [{"rule": f"规矩{k}", "why": "本人说过"} for k in range(5)],
                                                         "retire": [mine]}, ensure_ascii=False))
        job = drive._open(self.root, {"id": "learn-1", "kind": "learn", "instructions": "", "folder": str(folder)}, "本机 GPT")
        out = drive.submit(self.root, None, job["id"])
        self.assertEqual(out["added"], ["规矩0", "规矩1", "规矩2"])
        self.assertTrue(any(r["id"] == mine for r in memory.rules(self.root)))

    def test_waiting_items_keep_the_evidence_rules(self):
        from agent_matrix.models import MatrixError
        meta = {"meeting_id": "m1", "start_time": "2026-11-17 09:16", "owner_name": "我"}
        with self.assertRaises(MatrixError):          # "周五" alone could be this week or next: no date is guessed
            validate_review(self.first.model_copy(deep=True), T1, meta)
        repaired, _ = repair_review(self.first.model_copy(deep=True), T1, meta)
        self.assertEqual(len(repaired.waiting), 1)   # the waiting item is kept, only its date is cleared
        self.assertIsNone(repaired.waiting[0].due_date)
        validate_review(repaired, T1, meta)


if __name__ == "__main__":
    unittest.main()
