"""整理记忆: once a day, only when something is new; the AI proposes, nothing changes until the owner says 用."""
from datetime import datetime, timedelta
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_matrix import aidrivers, drive, memory, projects, tidy, versions
from agent_matrix import db as database
from agent_matrix.thread import set_setting
from agent_matrix.timeutil import TZ

ROWS = [{"record_id": f"r{n}", "编号": n, "标题": t, "状态": [s], "执行方": ["AI"]}
        for n, t, s in ((1, "定会员积分规则", "完成"), (2, "做会员页面", "进行中"), (3, "写上线通知", "未开始"))]


def snap(rows=ROWS):
    return patch("agent_matrix.snapshot._snapshot", return_value={"rows": rows})


class Tidy(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "state").mkdir()
        aidrivers.hello(self.root, "Antigravity")

    def moment(self, text, kind="decision", at="2026-11-10T10:00:00+08:00"):
        db = database.open(self.root, "xinxiang")
        try:
            with db:
                cur = db.execute("INSERT INTO moments (source, source_id, source_title, at, kind, text, created) VALUES (?,?,?,?,?,?,?)",
                                 ("meeting", text, "周会", at, kind, text, datetime.now(TZ).isoformat(timespec="seconds")))
                return cur.lastrowid
        finally:
            db.close()

    def job(self, plan):
        with snap():
            job = drive.next_job(self.root, None, "Antigravity", kinds=["tidy"])
        self.assertEqual(job["kind"], "tidy")
        (Path(job["folder"]) / "tidy.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        with snap():
            return job, drive.submit(self.root, None, job["id"])

    def test_it_runs_only_when_something_is_new_and_at_most_once_a_day(self):
        self.assertIn("tidy", drive.kinds_for(self.root, "Antigravity"))
        with snap([]):
            self.assertIsNone(tidy.due(self.root), "nothing new: no tidy")
            self.assertIsNone(drive.next_job(self.root, None, "Antigravity", kinds=["tidy"]))
        memory.record_outcome(self.root, "r1", "退回", "太长了")
        with snap():
            self.assertEqual(tidy.due(self.root), {"verdicts": 1, "moments": 0, "items": 3})
            job = drive.next_job(self.root, None, "Antigravity", kinds=["tidy"])
            for name in ("rules.json", "moments.json", "versions.json", "profile.md", "outcomes.json", "unplaced.json"):
                self.assertTrue((Path(job["folder"]) / name).is_file(), name)
            self.assertIn("第几步/共几步", job["instructions"])
            self.assertIn("tidy.json", job["instructions"])
            self.assertIsNone(drive._prepare_tidy(self.root, None, "Antigravity"), "one at a time")
        (Path(job["folder"]) / "tidy.json").write_text('{"changes": []}', encoding="utf-8")
        with snap():
            self.assertEqual(drive.submit(self.root, None, job["id"])["proposed"], 0)
            set_setting(self.root, "tidied_at", (datetime.now(TZ) - timedelta(minutes=1)).isoformat(timespec="seconds"))
            memory.record_outcome(self.root, "r2", "通过")
            self.assertIsNone(tidy.due(self.root), "within a day: not again")
            later = datetime.now(TZ) + timedelta(hours=25)
            self.assertGreaterEqual(tidy.due(self.root, now=later)["verdicts"], 1, "a day later, with a new verdict: again")
            set_setting(self.root, "tidy_tried_at", (datetime.now(TZ) - timedelta(hours=25)).isoformat(timespec="seconds"))
            set_setting(self.root, "tidied_at", datetime.now(TZ).isoformat(timespec="seconds"))
            self.assertIsNone(tidy.due(self.root), "a day passed, but nothing new since")

    def test_submit_checks_the_plan_and_keeps_it_waiting_without_changing_the_memory(self):
        memory.record_outcome(self.root, "r1", "退回", "太长了")
        before = {r["id"] for r in memory.rules(self.root)}
        job, out = self.job({"changes": [{"type": "retire_rule", "id": min(before), "why": "seed 不能退休"},
                                          {"type": "merge_rules", "ids": sorted(before)[:2], "rule": "少问多找"}]})
        self.assertEqual(out["status"], "fix")
        self.assertIn("第 1 条", out["problem"]); self.assertIn("第 2 条", out["problem"])
        (Path(job["folder"]) / "tidy.json").write_text("not json", encoding="utf-8")
        with snap():
            self.assertEqual(drive.submit(self.root, None, job["id"])["status"], "fix")
        a, b = sorted(before)[:2]
        (Path(job["folder"]) / "tidy.json").write_text(json.dumps({"changes": [
            {"type": "merge_rules", "ids": [a, b], "rule": "只管自己的事，交付先给结论", "why": "两条常一起用"},
            {"type": "bogus", "why": "x"}]}, ensure_ascii=False), encoding="utf-8")
        with snap():
            out = drive.submit(self.root, None, job["id"])   # out of fix rounds: the good ones are kept
        self.assertEqual((out["status"], out["proposed"]), ("done", 1))
        self.assertEqual({r["id"] for r in memory.rules(self.root)}, before, "nothing changes before the owner says 用")
        [u] = tidy.updates(self.root)
        self.assertEqual((u["label"], u["state"], u["why"]), ("合并规矩", "待定", "两条常一起用"))
        self.assertIn("合并成「只管自己的事，交付先给结论」", u["content"])
        self.assertEqual(tidy.save(self.root, [(u["type"], u["content"], u["why"], u["data"])]), 0, "the same one is not added twice")

    def test_use_applies_each_kind_and_drop_changes_nothing(self):
        pid = projects.create(self.root, "观己 App")
        v22 = versions.upsert(self.root, "观己 App", "2.2", "当前")
        versions.upsert(self.root, "观己 App", "2.3", "下一版")
        versions.place(self.root, "r1", v22)
        memory.set_profile(self.root, "## 岗位\n产品经理\n\n## 在做的事\n会员体系")
        memory.add_rule(self.root, "先给两版文案", "x", source="AI 学的")
        memory.add_rule(self.root, "文案给两个版本挑", "y", source="AI 学的")
        memory.add_rule(self.root, "方案写满三页", "z", source="AI 学的")
        rules = {r["rule"]: r["id"] for r in memory.rules(self.root)}
        old = self.moment("年卡先不做", at=datetime.now(TZ).isoformat(timespec="seconds"))
        new = self.moment("年卡这周做", at=datetime.now(TZ).isoformat(timespec="seconds"))
        _, out = self.job({"changes": [
            {"type": "merge_rules", "ids": [rules["先给两版文案"], rules["文案给两个版本挑"]], "rule": "对外文案先给两版让我挑", "why": "重复"},
            {"type": "retire_rule", "id": rules["方案写满三页"], "why": "退回 3 次"},
            {"type": "stale_moment", "id": old, "by": new, "why": "改主意了"},
            {"type": "place_item", "n": 2, "project": "观己 App", "version": "2.3", "why": "会员页在 2.3"},
            {"type": "place_item", "n": 3, "project": "心响", "why": "上线通知"},
            {"type": "version_status", "project": "观己 App", "version": "2.2", "status": "已完成", "why": "上线了"},
            {"type": "profile", "old": "## 在做的事\n会员体系", "new": "## 在做的事\n会员体系改版，下一步年卡", "why": "最近都在谈年卡"},
            {"type": "profile", "old": "", "new": "## 常一起干活的人\n张三（设计）", "why": "会上常出现"},
        ]})
        self.assertEqual((out["status"], out["proposed"]), ("done", 8))
        ups = {(u["type"], u["data"].get("n")): u["id"] for u in tidy.updates(self.root)}
        self.assertEqual(memory.profile(self.root)["text"].strip(), "## 岗位\n产品经理\n\n## 在做的事\n会员体系")
        tidy.drop(self.root, [ups[("place_item", 3)]])
        self.assertIsNone(projects.chosen(self.root).get("r3"), "不要: nothing changes")
        used, failed = tidy.use(self.root, [u["id"] for u in tidy.updates(self.root)])
        self.assertEqual((len(used), failed), (7, {}))
        texts = {r["rule"]: r for r in memory.rules(self.root)}
        self.assertIn("对外文案先给两版让我挑", texts)
        self.assertFalse(texts["对外文案先给两版让我挑"]["fresh"], "the owner said yes to it")
        for gone in ("先给两版文案", "文案给两个版本挑", "方案写满三页"):
            self.assertNotIn(gone, texts)
        self.assertEqual(next(m for m in memory.moments(self.root) if m["id"] == old)["status"], "stale")
        self.assertNotIn("年卡先不做", [m["text"] for m in memory.recall(self.root, "年卡先不做")])
        self.assertEqual(versions.of(self.root, "r2")["name"], "2.3")
        self.assertFalse(versions.place(self.root, "r2", v22), "placed with the owner's yes: the AI does not move it")
        self.assertEqual({v["name"]: v["status"] for v in versions.all(self.root)}, {"2.2": "done", "2.3": "next"})
        text = memory.profile(self.root)["text"]
        self.assertIn("会员体系改版，下一步年卡", text); self.assertTrue(text.strip().endswith("张三（设计）"))
        self.assertEqual(tidy.updates(self.root), [])
        self.assertEqual({u["state"] for u in tidy.updates(self.root, status=None)}, {"已用", "不要"})
        self.assertEqual(pid, versions.all(self.root)[0]["project_id"])

    def test_a_profile_change_that_no_longer_fits_stays_waiting(self):
        memory.set_profile(self.root, "## 在做的事\n会员体系")
        tidy.save(self.root, [("profile", "## 在做的事\n年卡", "谈得多", {"old": "## 在做的事\n会员体系", "new": "## 在做的事\n年卡"})])
        memory.set_profile(self.root, "## 在做的事\n别的")
        [u] = tidy.updates(self.root)
        used, failed = tidy.use(self.root, [u["id"]])
        self.assertEqual(used, []); self.assertIn("对不上", failed[u["id"]])
        self.assertEqual(len(tidy.updates(self.root)), 1)

    def test_the_page_lists_them_and_the_owner_decides(self):
        from agent_matrix import app
        tidy.save(self.root, [("retire_rule", "「x」不再用", "退回多", {"id": 999})])
        with snap():
            view = app.memory_view(self.root, [])["memory"]
        self.assertEqual([(u["label"], u["content"], u["why"]) for u in view["updates"]], [("退休规矩", "「x」不再用", "退回多")])
        memory.add_rule(self.root, "方案写满三页", "z", source="AI 学的")
        rid = next(r["id"] for r in memory.rules(self.root) if r["rule"] == "方案写满三页")
        tidy.save(self.root, [("retire_rule", "「方案写满三页」不再用", "退回多", {"id": rid})])
        import http.client, threading
        server = app.make_server(self.root, 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        def post(body):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            conn.request("POST", "/api/memory/updates", json.dumps(body), {"Host": f"127.0.0.1:{server.server_address[1]}",
                                                                           "X-Xinxiang-Token": server.token, "Content-Type": "application/json"})
            return json.loads(conn.getresponse().read())
        first = tidy.updates(self.root)[0]["id"]
        self.assertEqual(post({"drop": first})["left"], 1)
        out = post({"use": "all"})
        self.assertEqual((out["ok"], out["used"], out["left"]), (True, 1, 0))
        self.assertNotIn("方案写满三页", [r["rule"] for r in memory.rules(self.root)])


if __name__ == "__main__":
    unittest.main()
