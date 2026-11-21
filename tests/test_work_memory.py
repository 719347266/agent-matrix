"""The work memory: verdicts become scoped rules with a record, and work is kept as project → version → items."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_matrix import aidrivers, drive, memory, projects, versions

ROWS = [{"record_id": f"r{n}", "编号": n, "标题": t, "状态": [s], "执行方": ["AI"]}
        for n, t, s in ((1, "定会员积分规则", "完成"), (2, "做会员页面", "进行中"), (3, "写上线通知", "未开始"))]


class WorkMemory(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "state").mkdir()
        aidrivers.hello(self.root, "Antigravity")

    def test_verdicts_are_kept_and_rules_hold_where_they_belong(self):
        pid = projects.create(self.root, "观己 App")
        projects.assign(self.root, ["r1"], pid)
        (self.root / "state/work/r1").mkdir(parents=True)
        (self.root / "state/work/r1/结果.json").write_text(json.dumps({"template": "方案"}), encoding="utf-8")
        memory.record_outcome(self.root, "r1", "退回", "别写过程，只要结论")
        memory.record_outcome(self.root, "r1", "通过")
        got = memory.outcomes(self.root)
        self.assertEqual([(o["verdict"], o["template"], o["project"]) for o in got], [("退回", "方案", "观己 App"), ("通过", "方案", "观己 App")])
        memory.add_rule(self.root, "给客户的方案不写内部代号", source="AI 学的", scope=f"project:{pid}")
        memory.add_rule(self.root, "周报先给结论", source="AI 学的", scope="type:周报")
        other = projects.create(self.root, "心响")
        self.assertIn("不写内部代号（项目「观己 App」）", memory.rules_text(self.root, pid))
        self.assertNotIn("不写内部代号", memory.rules_text(self.root, other), "another project's rule stays out")
        self.assertIn("周报先给结论（做「周报」时）", memory.rules_text(self.root, None))
        rule = next(r for r in memory.rules(self.root) if r["rule"] == "周报先给结论")
        memory.credit(self.root, rule["id"], True); memory.credit(self.root, rule["id"], False); memory.credit(self.root, rule["id"], True)
        rule = next(r for r in memory.rules(self.root) if r["rule"] == "周报先给结论")
        self.assertEqual((rule["wins"], rule["losses"], rule["scope_name"]), (2, 1, "做「周报」时"))

    def test_the_learning_job_reads_verdicts_and_keeps_score(self):
        memory.record_outcome(self.root, "r1", "退回", "太长了")
        with patch("agent_matrix.snapshot._snapshot", return_value={"rows": ROWS}):
            job = drive._prepare_learn(self.root, None, "Antigravity")
        self.assertIn("1 次评审", job["title"])
        verdicts = json.loads((Path(job["folder"]) / "outcomes.json").read_text(encoding="utf-8"))
        self.assertEqual((verdicts[0]["n"], verdicts[0]["item"], verdicts[0]["reason"]), (1, "定会员积分规则", "太长了"))
        seed = memory.rules(self.root)[0]["id"]
        (Path(job["folder"]) / "learned.json").write_text(json.dumps(
            {"add": [{"rule": "方案控制在一屏", "why": "#1 退回：太长了", "scope": "type:方案"},
                     {"rule": "乱填的范围", "why": "x", "scope": "project:999"}], "wins": [seed], "losses": [12345]}), encoding="utf-8")
        drive._open(self.root, job, "Antigravity")
        self.assertEqual(drive.submit(self.root, None, job["id"])["status"], "done")
        rules = {r["rule"]: r for r in memory.rules(self.root)}
        self.assertEqual(rules["方案控制在一屏"]["scope"], "type:方案")
        self.assertEqual(rules["乱填的范围"]["scope"], "", "a scope naming nothing known holds everywhere")
        self.assertEqual(next(r for r in memory.rules(self.root) if r["id"] == seed)["wins"], 1)

    def test_the_ai_sorts_work_into_versions_and_the_owner_has_the_last_word(self):
        versions.ask_organize(self.root)
        self.assertIn("organize", drive.kinds_for(self.root, "Antigravity"))
        with patch("agent_matrix.snapshot._snapshot", return_value={"rows": ROWS}):
            job = drive.next_job(self.root, None, "Antigravity", kinds=["organize"])
            self.assertEqual(job["kind"], "organize")
            self.assertIn("需求池", job["instructions"])
            items = json.loads((Path(job["folder"]) / "items.json").read_text(encoding="utf-8"))
            self.assertEqual([i["n"] for i in items], [1, 2, 3])
            (Path(job["folder"]) / "organize.json").write_text(json.dumps({
                "versions": [{"project": "观己 App", "name": "2.3", "status": "当前", "note": "会员改版"},
                             {"project": "观己 App", "name": "2.4", "status": "下一版"}],
                "items": [{"n": 1, "project": "观己 App", "version": "2.3"}, {"n": 2, "project": "观己 App", "version": "2.3"},
                          {"n": 99, "project": "观己 App", "version": "2.3"}],
                "links": [{"earlier": 1, "later": 2}, {"earlier": 2, "later": 2}],
                "expected": [{"project": "观己 App", "version": "2.3", "title": "上线前验收", "why": "每个版本都要"}]}), encoding="utf-8")
            with patch("agent_matrix.inbox.add"):
                out = drive.submit(self.root, None, job["id"])
            self.assertEqual((out["status"], out["versions"], out["items"], out["links"], out["expected"]), ("done", 2, 2, 1, 1))
            self.assertIsNone(versions.organize_asked(self.root))
            v23 = next(v for v in versions.all(self.root) if v["name"] == "2.3")
            self.assertEqual((v23["status"], sorted(x["rid"] for x in v23["items"])), ("current", ["r1", "r2"]))
            self.assertEqual(projects.chosen(self.root).get("r1"), v23["project_id"], "putting it in a version puts it in the project")
            text = versions.context_text(self.root, "r2", {r["record_id"]: r for r in ROWS})
            for part in ("观己 App · 2.3（当前）", "会员改版", "定会员积分规则（完成）", "上线前验收"):
                self.assertIn(part, text)
            v24 = next(v for v in versions.all(self.root) if v["name"] == "2.4")
            versions.place(self.root, "r2", v24["id"], by=versions.OWNER)
            self.assertFalse(versions.place(self.root, "r2", v23["id"]), "the AI does not move what the owner placed")
            self.assertEqual(versions.of(self.root, "r2")["name"], "2.4")
            versions.upsert(self.root, "观己 App", "2.4", "当前")
            self.assertEqual({v["name"]: v["status"] for v in versions.all(self.root)}, {"2.3": "done", "2.4": "current"})
            drive._close(self.root, job, "done")
            item = drive._open(self.root, {"id": "item-r2-x", "kind": "item", "title": "做会员页面", "folder": str(self.root),
                                           "ref": {"record_id": "r2", "version": "v"}, "output": [], "instructions": "做"}, "Antigravity")
        self.assertIn("这件事属于：观己 App · 2.4", item["instructions"])


if __name__ == "__main__":
    unittest.main()
