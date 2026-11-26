import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from agent_matrix import thread
from agent_matrix.reply_timing import measure


def msg(minute, who, kind, text="", day="2026-11-17"):
    return dict(iso_at=f"{day}T17:{minute:02}:00+08:00", **{"from": who}, kind=kind, text=text)


def stats(*messages):
    return measure({"r": {"messages": messages}}, datetime.fromisoformat("2026-11-18T12:00:00+08:00"))


class ReplyTiming(unittest.TestCase):
    def test_first_observed_work_not_a_reply_and_multiple_followups_count_once(self):
        m, summary = stats(msg(30, "me", "feedback"), msg(37, "me", "feedback"),
                           msg(40, "me", "note"), msg(46, "system", "say", "Claude 开始处理"),
                           msg(46,"ai","step"), msg(47,"ai","deliver"), msg(48,"ai","say"))
        self.assertEqual(summary["replies"], 1)
        self.assertEqual((summary["median_pickup_seconds"], summary["median_reply_seconds"]), (540, 600))
        self.assertEqual(m["r"]["reply_at"], "2026-11-17T17:47:00+08:00")

    def test_no_reply_or_no_pickup_is_not_zero(self):
        m, summary = stats(msg(1,"me","say"), msg(2,"ai","step"))
        self.assertIsNone(m["r"]["reply_at"])
        self.assertEqual(summary["replies"], 0)
        self.assertIsNone(summary["median_reply_seconds"])
        _, summary = stats(msg(1,"me","say"), msg(2,"ai","ask"))
        self.assertEqual(summary["pickups"], 0)
        self.assertEqual(summary["median_reply_seconds"], 60)

    def test_closed_notes_admin_and_old_round_do_not_distort_reference(self):
        for source in [msg(1,"me","note"),msg(1,"me","feedback","（整理自旧事项）"),
                       msg(1,"me","feedback","改期到 周五")]:
            self.assertEqual(stats(source,msg(2,"ai","deliver"))[1]["replies"], 0)
        self.assertEqual(stats(msg(1,"me","feedback"),msg(2,"system","closed"),msg(3,"ai","say"))[1]["replies"],0)
        latest, ref = stats(msg(1,"me","feedback",day="2026-10-01"),msg(10,"ai","deliver",day="2026-10-01"))
        self.assertTrue(latest["r"]["reply_at"])
        self.assertEqual(ref["replies"],0)

    def test_owner_revision_from_old_notebook_does_not_impersonate_ai(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            thread.add(root,"r","ai","say","本人要改：换张图",dedupe="note:r:test",driver="本机 GPT")
            thread.add(root,"r","ai","say","本人要改：原话引用",driver="Cindy")
            messages=thread.history(root)["r"]["messages"]
        self.assertEqual((messages[0]["from"],messages[0]["kind"],messages[0]["text"]),("me","feedback","换张图"))
        self.assertIsNone(messages[0]["driver"])
        self.assertEqual(messages[1]["from"],"ai")
        self.assertEqual(thread.classify("本人要改：再调整"),("me","feedback","再调整",None))
