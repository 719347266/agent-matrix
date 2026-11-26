"""效果统计（stats.py）：一次通过率、几轮才通过、退回原因、分组、卡住/没做成、缓存。"""
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from agent_matrix import db as database, stats
from agent_matrix.timeutil import TZ

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=TZ)   # a Saturday; this week starts Monday 11-16


def ago(days):
    return (NOW - timedelta(days=days)).isoformat(timespec="seconds")


class StatsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        stats._cache.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, sql, rows):
        db = database.open(self.root, "xinxiang")
        try:
            with db:
                db.executemany(sql, rows)
        finally:
            db.close()

    def verdicts(self, *rows):
        self.put("INSERT INTO outcomes (record_id, at, verdict, reason, version, template, project_id) VALUES (?,?,?,?,?,?,?)", rows)

    def job(self, jid, rid, driver, status, days):
        self.put("INSERT INTO jobs (id, kind, ref, driver, status, created, updated) VALUES (?,?,?,?,?,?,?)",
                 [(jid, "item", json.dumps({"record_id": rid}), driver, status, ago(days), ago(days))])

    def test_no_data(self):
        out = stats.compute(self.root, NOW)
        self.assertFalse(out["has"])
        self.assertEqual(len(out["weeks"]), 8)
        self.assertIsNone(out["recent"]["rate"])
        self.assertIsNone(out["rounds"])
        self.assertEqual((out["reasons"], out["by_type"], out["blocked"], out["failed"]), ([], [], 0, 0))

    def test_first_pass_rate_rounds_and_weeks(self):
        self.put("INSERT INTO projects (name, created) VALUES (?,?)", [("示例 App", ago(60))])
        self.verdicts(
            ("a", ago(1), "通过", "", 1, "方案", 1),                     # this week, first time
            ("b", ago(2), "退回", "写得太长了，一屏看不完", 1, "方案", 1),   # this week, came back, then passed at v2
            ("b", ago(1), "通过", "", 2, "方案", 1),
            ("c", ago(3), "通过", "", 3, "报告", None),                   # passed, but only at v3: not first time
            ("d", ago(40), "要改", "方向不对，不是我要的", 1, "方案", None),  # 5 weeks ago
            ("d", ago(39), "要改", "缺数据", 2, "方案", None),
            ("d", ago(38), "通过", "", 3, "方案", None),
            ("e", ago(41), "通过", "", 1, "", None),
            ("old", ago(200), "通过", "", 1, "方案", None))               # outside the 8 weeks
        out = stats.compute(self.root, NOW)
        self.assertTrue(out["has"])
        last = out["weeks"][-1]
        self.assertEqual((last["start"], last["total"], last["first"], last["rate"]), ("11-16", 3, 1, 33))
        self.assertEqual(out["recent"], {"total": 3, "first": 1, "rate": 33})
        self.assertEqual(out["before"], {"total": 2, "first": 1, "rate": 50})
        self.assertEqual(last["rounds"], 1.0)                       # a:0, b:1, c:2 → 1
        self.assertEqual(out["rounds"], 1.0)                        # + d:2, e:0 → 5/5
        kinds = {r["kind"]: r["count"] for r in out["reasons"]}
        self.assertEqual(kinds, {"太长/格式": 1, "方向不对": 1, "缺数据/背景": 1})
        types = {g["name"]: g for g in out["by_type"]}
        self.assertEqual((types["方案"]["total"], types["方案"]["first"], types["方案"]["rate"], types["方案"]["few"]), (3, 1, 33, False))
        self.assertTrue(types["报告"]["few"])
        self.assertIn("没标类型", types)
        projects = {g["name"]: g["total"] for g in out["by_project"]}
        self.assertEqual(projects, {"示例 App": 2, "没归项目": 3})

    def test_by_ai_and_blocked_failed(self):
        for rid in "xyz":
            self.verdicts((rid, ago(2), "通过", "", 1, "方案", None))
        self.verdicts(("w", ago(2), "退回", "", 1, "方案", None))
        for i, rid in enumerate("xyz"):
            self.job(f"j{i}", rid, "Claude", "done", 3)
        self.job("j9", "w", "Codex", "done", 3)
        self.job("b1", "w", "Codex", "blocked", 4)
        self.job("b2", "q", "Codex", "blocked", 20)
        self.job("f1", "q", "Codex", "failed", 5)
        self.job("f0", "q", "Codex", "failed", 100)   # too long ago
        out = stats.compute(self.root, NOW)
        ai = {g["name"]: g for g in out["by_ai"]}
        self.assertEqual((ai["Claude"]["rate"], ai["Claude"]["few"]), (100, False))
        self.assertEqual((ai["Codex"]["rate"], ai["Codex"]["few"]), (0, True))
        self.assertEqual((out["blocked"], out["failed"]), (2, 1))
        self.assertEqual(out["weeks"][-1]["blocked"], 1)
        self.assertEqual(out["reasons"], [{"kind": "其他", "count": 1, "example": ""}])

    def test_reason_kinds(self):
        self.assertEqual(stats.reason_kind("格式乱"), "太长/格式")
        self.assertEqual(stats.reason_kind("没有背景"), "缺数据/背景")
        self.assertEqual(stats.reason_kind("跑偏了"), "方向不对")
        self.assertEqual(stats.reason_kind(""), "其他")

    def test_cache_until_new_verdict(self):
        self.assertFalse(stats.cached(self.root)["has"])
        first = stats.cached(self.root)
        self.assertIs(stats.cached(self.root), first)                 # within 10 minutes: the same numbers
        self.verdicts(("a", datetime.now(TZ).isoformat(timespec="seconds"), "通过", "", 1, "方案", None))
        self.assertTrue(stats.cached(self.root)["has"])               # a new verdict: worked out again

    def test_page_gets_effect(self):
        from agent_matrix.app import memory_view
        self.verdicts(("a", datetime.now(TZ).isoformat(timespec="seconds"), "通过", "", 1, "方案", None))
        effect = memory_view(self.root, [])["memory"]["effect"]
        self.assertTrue(effect["has"])
        self.assertEqual(effect["recent"]["rate"], 100)


if __name__ == "__main__":
    unittest.main()
