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
            job = take(self.root)
        self.assertEqual(job["kind"], "tidy")
        (Path(job["folder"]) / "tidy.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        with snap():
            return job, drive.submit(self.root, None, job["id"])

    def test_it_runs_only_when_something_is_new_and_at_most_once_a_day(self):
        self.assertIn("tidy", drive.kinds_for(self.root, "Antigravity"))
        with snap([]):
            self.assertIsNone(tidy.due(self.root), "nothing new: no tidy")
            self.assertIsNone(take(self.root))
        memory.record_outcome(self.root, "r1", "退回", "太长了")
        with snap():
            self.assertEqual(tidy.due(self.root), {"verdicts": 1, "moments": 0, "items": 3})
            job = take(self.root)
            for name in ("记忆/目录.md", "moments.json", "versions.json", "profile.md", "outcomes.json", "unplaced.json"):
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
            # A second verdict arrived after the input snapshot. Complete the next round to consume it.
            next_job = take(self.root)
            (Path(next_job["folder"]) / "tidy.json").write_text('{"changes":[]}')
            self.assertEqual(drive.submit(self.root, None, next_job["id"])["status"], "done")
            self.assertIsNone(tidy.due(self.root, now=later), "both verdicts have now been seen")

    def test_old_evidence_cannot_create_a_fresh_profile_change(self):
        memory.set_profile(self.root, "偏好简短")
        plan = {"changes": [{"type": "profile", "old": "简短", "new": "先列结论", "why": "较早的评审", "evidence": "outcome:1"}]}
        ok, problems = tidy.check(self.root, plan, profile_evidence=["outcome:2"])
        self.assertFalse(ok)
        self.assertTrue(problems)
        plan["changes"][0]["evidence"] = "outcome:2"
        self.assertEqual(len(tidy.check(self.root, plan, profile_evidence=["outcome:2"])[0]), 1)

    def test_submit_checks_the_plan_and_keeps_it_waiting_without_changing_the_memory(self):
        from agent_matrix import library
        memory.record_outcome(self.root, "r1", "退回", "太长了")
        before = library.get(self.root, library.GENERAL)["raw"]
        job, out = self.job({"changes": [{"type": "memory", "mode": "revise", "name": library.GENERAL, "old_text": "没有这句", "body": "x", "why": "w"},
                                          {"type": "memory", "mode": "append", "name": "没有这份", "body": "## a\nb", "why": "w"}]})
        self.assertEqual(out["status"], "fix")
        self.assertIn("第 1 条", out["problem"]); self.assertIn("第 2 条", out["problem"])
        (Path(job["folder"]) / "tidy.json").write_text("not json", encoding="utf-8")
        with snap():
            self.assertEqual(drive.submit(self.root, None, job["id"])["status"], "fix")
        (Path(job["folder"]) / "tidy.json").write_text(json.dumps({"changes": [
            {"type": "memory", "mode": "append", "name": library.GENERAL, "body": "## 少问多找\n先自己查。", "why": "两条常一起用"},
            {"type": "bogus", "why": "x"}]}, ensure_ascii=False), encoding="utf-8")
        with snap():
            out = drive.submit(self.root, None, job["id"])   # out of fix rounds: the good ones are kept
        self.assertEqual((out["status"], out["proposed"]), ("done", 1))
        self.assertEqual(library.get(self.root, library.GENERAL)["raw"], before, "nothing changes before the owner says 用")
        [u] = tidy.updates(self.root)
        self.assertEqual((u["label"], u["state"], u["why"]), ("经验库", "待定", "两条常一起用"))
        self.assertIn("加上：少问多找", u["content"])
        self.assertEqual(tidy.save(self.root, [(u["type"], u["content"], u["why"], u["data"])]), 0, "the same one is not added twice")

    def test_use_applies_each_kind_and_drop_changes_nothing(self):
        pid = projects.create(self.root, "示例 App")
        v22 = versions.upsert(self.root, "示例 App", "2.2", "当前")
        versions.upsert(self.root, "示例 App", "2.3", "下一版")
        versions.place(self.root, "r1", v22)
        memory.set_profile(self.root, "## 岗位\n产品经理\n\n## 在做的事\n会员体系")
        from agent_matrix import library
        seeded = library.get(self.root, library.GENERAL)["sections"][2][1]   # 没有人明确承接的话不算待办
        old = self.moment("年卡先不做", at=datetime.now(TZ).isoformat(timespec="seconds"))
        new = self.moment("年卡这周做", at=datetime.now(TZ).isoformat(timespec="seconds"))
        _, out = self.job({"changes": [
            {"type": "memory", "mode": "append", "name": library.GENERAL, "body": "## 对外文案先给两版让我挑\n先给两版。", "why": "重复退回"},
            {"type": "memory", "mode": "remove", "name": library.GENERAL, "old_text": seeded, "why": "本人改主意了"},
            {"type": "stale_moment", "id": old, "by": new, "why": "改主意了"},
            {"type": "place_item", "n": 2, "project": "示例 App", "version": "2.3", "why": "会员页在 2.3"},
            {"type": "place_item", "n": 3, "project": "心响", "why": "上线通知"},
            {"type": "version_status", "project": "示例 App", "version": "2.2", "status": "已完成", "why": "上线了"},
            {"type": "profile", "old": "## 在做的事\n会员体系", "new": "## 在做的事\n会员体系改版，下一步年卡", "why": "最近都在谈年卡", "evidence": f"moment:{new}"},
            {"type": "profile", "old": "", "new": "## 常一起干活的人\n张三（设计）", "why": "会上常出现", "evidence": f"moment:{new}"},
        ]})
        self.assertEqual((out["status"], out["proposed"]), ("done", 8))
        ups = {(u["type"], u["data"].get("n")): u["id"] for u in tidy.updates(self.root)}
        self.assertEqual(memory.profile(self.root)["text"].strip(), "## 岗位\n产品经理\n\n## 在做的事\n会员体系")
        tidy.drop(self.root, [ups[("place_item", 3)]])
        self.assertIsNone(projects.chosen(self.root).get("r3"), "不要: nothing changes")
        used, failed = tidy.use(self.root, [u["id"] for u in tidy.updates(self.root)])
        self.assertEqual((len(used), failed), (7, {}))
        titles = [t for t, _ in library.get(self.root, library.GENERAL)["sections"]]
        self.assertIn("对外文案先给两版让我挑", titles)
        self.assertNotIn("没有人明确承接的话不算待办，不要硬凑", titles)
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
        from agent_matrix import library
        draft = lambda title: ("memory",) + (lambda m, c, d: (c, d["why"], d))(*library.check_draft(
            self.root, {"mode": "append", "name": library.GENERAL, "body": f"## {title}\n做法", "why": "退回多"}))
        tidy.save(self.root, [draft("写满三页")])
        with snap():
            view = app.memory_view(self.root, [])["memory"]
        self.assertEqual([(u["label"], u["content"], u["why"]) for u in view["updates"]], [("经验库", f"「{library.GENERAL}」加上：写满三页", "退回多")])
        self.assertIn("加上：\n## 写满三页", view["updates"][0]["detail"])      # the owner reads the text itself before 用
        tidy.save(self.root, [draft("方案先给结论")])
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
        titles = [t for t, _ in library.get(self.root, library.GENERAL)["sections"]]
        self.assertIn("方案先给结论", titles); self.assertNotIn("写满三页", titles)


if __name__ == "__main__":
    unittest.main()


def take(root):
    """The tidy job alone (in next_job it waits behind any other work: see TidyWaitsForRealWork)."""
    job = drive._prepare_tidy(root, None, "Antigravity")
    return drive._open(root, job, "Antigravity") if job else None


class TidyWaitsForRealWork(unittest.TestCase):
    def test_an_item_waiting_goes_first_and_tidying_comes_when_idle(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from agent_matrix import aidrivers, drive
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "state").mkdir()
            aidrivers.hello(root, "Antigravity")
            order = []
            fake_item = lambda root, config, driver, **k: order.append("item") or None
            fake_tidy = lambda root, config, driver: order.append("tidy") or None
            with patch.object(drive, "_prepare_item", fake_item), patch.object(drive, "_prepare_tidy", fake_tidy), \
                 patch.object(drive, "_prepare_triage", lambda *a: None), patch.object(drive, "_prepare_meeting", lambda *a: None), \
                 patch.object(drive, "_prepare_chat", lambda *a: None), patch.object(drive, "_prepare_learn", lambda *a: None):
                drive.next_job(root, None, "Antigravity")
            self.assertEqual(order, ["item", "tidy"])


class PlaceMatchesExisting(unittest.TestCase):
    def test_a_near_project_name_lands_in_the_existing_project_and_settled_proposals_close(self):
        import tempfile
        from agent_matrix import projects, versions
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "state").mkdir()
            pid = projects.create(root, "示例 App")
            self.assertEqual(projects.find(root, "示例")["id"], pid)
            self.assertIsNone(projects.find(root, "心响"))
            tidy._apply(root, "place_item", {"rid": "r1", "project": "示例", "version": ""})
            self.assertEqual(projects.chosen(root)["r1"], pid)
            self.assertEqual([p["name"] for p in projects.all(root)], ["示例 App"], "no second project")
            tidy.save(root, [("place_item", "#2 x → 示例", "y", {"n": 2, "rid": "r2", "project": "示例", "version": ""}),
                             ("place_item", "#3 x → 示例", "y", {"n": 3, "rid": "r3", "project": "示例", "version": ""})], "tidy-1")
            vid = versions.upsert(root, "示例 App", "2.3", "当前")
            versions.place(root, "r2", vid)   # 整理项目和版本 placed it meanwhile
            self.assertEqual([u["data"]["rid"] for u in tidy.updates(root)], ["r3"])
