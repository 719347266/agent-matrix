"""M3 事项关系补全: triage also proposes what comes before / after a new item and what its version will bring;
expected things with a date are told in 心响 the day before and on the day, once each."""
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_matrix import drive, inbox, itemlinks, thread, versions
from agent_matrix.models import Config


class Done:
    stdout = "✓"


class TriageRelations(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "state").mkdir()
        self.vid = versions.upsert(self.root, "观己 App", "2.3", "当前")
        versions.place(self.root, "r60", self.vid)

    def submit(self, verdicts, ref):
        folder = self.root / "t"
        folder.mkdir(exist_ok=True)
        job = drive._open(self.root, {"id": "triage-1", "kind": "triage", "folder": str(folder), "ref": ref,
                                      "lease_until": drive._lease(30), "instructions": ""}, "Claude Code")
        (folder / "triage.json").write_text(json.dumps(verdicts, ensure_ascii=False), encoding="utf-8")
        with patch("agent_matrix.drive.subprocess.run", lambda argv, **k: Done()):
            return drive.submit(self.root, Config(), job["id"])

    def test_links_and_expected_are_written_and_wrong_numbers_dropped(self):
        items = [{"n": 60, "record_id": "r60", "who": "我"}, {"n": 61, "record_id": "r61", "who": "我"},
                 {"n": 62, "record_id": "r62", "who": "我"}]
        numbers = {"31": "r31", "52": "r52", "60": "r60", "61": "r61", "62": "r62"}
        out = self.submit([
            {"n": 60, "decision": "ai", "reason": "起草", "before": [31, 999, 60], "after": ["#52"],
             "expected": [{"title": "上线前验收", "why": "每版都要", "due": "2026-11-28"}, {"why": "没有标题"}]},
            {"n": 61, "decision": "prep", "reason": "要拍板", "before": "60",
             "expected": [{"title": "不在版本里，跳过"}]},
            {"n": 62, "decision": "skip", "reason": "对方说不用了", "before": [31]},   # closed: nothing linked
        ], {"items": items, "numbers": numbers})
        self.assertEqual(out["status"], "done")
        self.assertEqual(set(itemlinks.pairs(self.root)), {("r31", "r60"), ("r60", "r52"), ("r60", "r61")})
        self.assertEqual(set(itemlinks.made_by(self.root).values()), {"AI"})
        exp = next(v for v in versions.all(self.root) if v["id"] == self.vid)["expected"]
        self.assertEqual([(e["title"], e["due"]) for e in exp], [("上线前验收", "2026-11-28")])
        self.assertEqual({a["n"]: (a.get("links"), a.get("expected")) for a in out["applied"]},
                         {60: (2, 1), 61: (1, None), 62: (None, None)})
        self.assertIn("串上了 2 件前后的事", thread.history(self.root)["r60"]["messages"][0]["text"])

    def test_old_format_and_old_jobs_still_go_through(self):
        items = [{"n": 60, "record_id": "r60", "who": "我"}, {"n": 61, "record_id": "r61", "who": "我"}]
        out = self.submit([{"n": 60, "decision": "ai", "reason": "起草"},
                           {"n": 61, "decision": "me", "reason": "本人出面", "before": [60, 31]}],   # 31 unknown to an old job
                          {"items": items})
        self.assertEqual([a["n"] for a in out["applied"]], [60, 61])
        self.assertEqual(itemlinks.pairs(self.root), [("r60", "r61")])
        self.assertEqual(thread.judgments(self.root)["r60"]["decision"], "ai")

    def test_the_items_the_ai_sees(self):
        rows = [{"record_id": "r1", "编号": 1, "标题": "定规则", "状态": ["完成"]},
                {"record_id": "r60", "编号": 60, "标题": "做页面", "状态": ["进行中"]},
                {"record_id": "r61", "编号": "61", "标题": "写通知", "状态": "待确认"},
                {"record_id": "rx", "编号": None, "标题": "没编号"}]
        others, numbers = drive._triage_others(self.root, rows)
        self.assertEqual([(o["n"], o["version"]) for o in others], [(60, "观己 App · 2.3"), (61, "")])
        self.assertEqual(numbers, {"60": "r60", "61": "r61"})


class ExpectedDue(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "state").mkdir()
        self.vid = versions.upsert(self.root, "观己 App", "2.3", "当前")

    def test_told_the_day_before_and_on_the_day_once_each(self):
        versions.expect(self.root, self.vid, "上线前验收", "每版都要", "11-28")
        versions.expect(self.root, self.vid, "以后的事", due="2026-12-20")
        versions.expect(self.root, self.vid, "不写日子")
        self.assertEqual(versions.expected_due(self.root, date(2026, 11, 26)), [])
        self.assertEqual(len(versions.expected_due(self.root, date(2026, 11, 27))), 1)
        self.assertEqual(versions.expected_due(self.root, date(2026, 11, 27)), [], "same day again: not told twice")
        self.assertEqual(len(versions.expected_due(self.root, date(2026, 11, 28))), 1)
        self.assertEqual(versions.expected_due(self.root, date(2026, 11, 28)), [])
        self.assertEqual(versions.expected_due(self.root, date(2026, 11, 29)), [], "past due: not told again")
        titles = [m["title"] for m in inbox.messages(self.root)]
        self.assertEqual(sorted(titles), ["预计会遇到的事今天到期：上线前验收", "预计会遇到的事明天到期：上线前验收"])

    def test_done_or_dropped_things_are_not_told_and_dates_are_read_loosely(self):
        versions.expect(self.root, self.vid, "对外通知", due="2026年11月28日")
        eid = next(v for v in versions.all(self.root) if v["id"] == self.vid)["expected"][0]["id"]
        self.assertEqual(next(v for v in versions.all(self.root) if v["id"] == self.vid)["expected"][0]["due"], "2026-11-28")
        versions.set_expected(self.root, eid, "dropped")
        self.assertEqual(versions.expected_due(self.root, date(2026, 11, 28)), [])
        self.assertEqual(versions.due_date("1-5", date(2026, 11, 21)), date(2027, 1, 5))
        self.assertIsNone(versions.due_date("下周", date(2026, 11, 21)))
        self.assertIsNone(versions.due_date("2026-02-30"))

    def test_saying_it_again_fills_in_the_date(self):
        versions.expect(self.root, self.vid, "上线前验收")
        versions.expect(self.root, self.vid, "上线前验收", "每版都要", "2026-11-28")
        exp = next(v for v in versions.all(self.root) if v["id"] == self.vid)["expected"]
        self.assertEqual([(e["title"], e["why"], e["due"]) for e in exp], [("上线前验收", "每版都要", "2026-11-28")])


if __name__ == "__main__":
    unittest.main()
