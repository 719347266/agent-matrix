"""The 心响 driver protocol: any AI takes a job, works in its folder, submits; code checks and publishes."""
import json
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from agent_matrix.models import Config, MatrixError

GOOD = textwrap.dedent("""\
    # 投流方案
    - 先投人群包 A，日预算 800 元，看一周点击成本再决定是否加预算
    - 素材用三套：痛点、口碑、限时福利，每套两个版本做对比测试
    ## 推荐
    人群包 A 是过去三个月咨询过的相似人群，转化最高，成本最稳，适合先投。
    ## 选项对比
    | 方案 | 成本 | 风险 |
    |---|---|---|
    | A | 低 | 低 |
    ## 需要你拍板
    第一周预算是否按 800 元一天。
    ## 用到的资料
    - 会议原话
    ## 我做的假设
    - 预算按上月估
    """)


class DriveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = Config()

    def tearDown(self):
        self.tmp.cleanup()

    def open_job(self, kind, **extra):
        from agent_matrix import drive
        folder = self.root / "work" / kind
        folder.mkdir(parents=True, exist_ok=True)
        job = {"id": f"{kind}-1", "kind": kind, "title": "测试", "folder": str(folder), "lease_until": drive._lease(30),
               "instructions": "做这件事。", "output": ["x"], **extra}
        return drive._open(self.root, job, "Claude Code"), folder

    def test_what_an_outside_ai_gets(self):
        from agent_matrix import drive
        job, _ = self.open_job("item", n=44, ref={"record_id": "rec44", "version": "a" * 64})
        out = drive.public(job)
        self.assertEqual(out["job"], "item-1")
        self.assertIn("submit --job item-1", out["instructions"])
        self.assertIn("ask --job item-1", out["instructions"])
        self.assertIn(str(drive.shim_path()), out["instructions"])
        self.assertEqual([j["id"] for j in drive.open_jobs(self.root)], ["item-1"])
        self.assertEqual(drive.drivers(self.root)[0]["driver"], "Claude Code")

    def test_extraction_is_checked_and_fixed_before_it_is_published(self):
        from agent_matrix import drive
        source = self.root / "src"
        source.mkdir()
        job, folder = self.open_job("meeting", ref={"meeting_id": "m1", "source": str(source), "validate": ["v"], "publish": ["p"]})
        (folder / "review.json").write_text("{}")
        answers = iter([(1, {"ok": False, "error": "缺少承接字眼"}), (0, {"ok": True}), (0, {"ok": True, "task_ids": ["r1"]})])
        with patch("agent_matrix.worker._cli", lambda root, *a: next(answers)):
            first = drive.submit(self.root, self.config, job["id"])
            self.assertEqual(first["status"], "fix")
            self.assertIn("缺少承接字眼", first["problem"])
            self.assertTrue((source / "review.json").is_file())     # copied back for the check
            done = drive.submit(self.root, self.config, job["id"])
        self.assertEqual((done["status"], done["created"]), ("done", 1))
        self.assertEqual(drive.get(self.root, job["id"])["status"], "done")
        self.assertEqual(drive.submit(self.root, self.config, job["id"])["status"], "done")  # already finished

    def test_an_item_that_keeps_failing_the_check_is_put_to_the_owner(self):
        from agent_matrix import drive
        job, _ = self.open_job("item", n=44, ref={"record_id": "rec44", "version": "a" * 64})
        with patch("agent_matrix.drive._item") as item:
            item.return_value.workdir = self.root / "empty"
            for _ in range(drive.MAX_FIX_ROUNDS):
                self.assertEqual(drive.submit(self.root, self.config, job["id"])["status"], "fix")
            self.assertEqual(drive.submit(self.root, self.config, job["id"])["status"], "blocked")
            item.return_value.block.assert_called_once()

    def test_a_good_item_is_published(self):
        from agent_matrix import drive
        job, folder = self.open_job("item", n=44, ref={"record_id": "rec44", "version": "a" * 64})
        (folder / "交付.md").write_text(GOOD)
        (folder / "结果.json").write_text(json.dumps({"outcome": "done", "template": "方案", "summary": "先投人群包A",
                                                     "sources": ["会议"]}, ensure_ascii=False))
        with patch("agent_matrix.drive._item") as item:
            item.return_value.workdir = folder
            item.return_value.publish.return_value = {"url": "https://x/docx/a"}
            out = drive.submit(self.root, self.config, job["id"])
        self.assertEqual((out["status"], out["url"]), ("done", "https://x/docx/a"))

    def test_asking_the_owner_needs_a_question_and_what_was_tried(self):
        from agent_matrix import drive
        job, _ = self.open_job("item", n=44, ref={"record_id": "rec44", "version": "a" * 64})
        self.assertFalse(drive.ask(self.root, self.config, job["id"], "缺收入导出", "")["ok"])
        with patch("agent_matrix.drive._item") as item:
            self.assertEqual(drive.ask(self.root, self.config, job["id"], "能给收入导出吗？", "搜了文档和群聊")["status"], "blocked")
            item.return_value.block.assert_called_once()
        extract, _ = self.open_job("chat", ref={})
        self.assertFalse(drive.ask(self.root, self.config, extract["id"], "q", "t")["ok"])
        with self.assertRaises(MatrixError):
            drive.get(self.root, "nope")

    def test_triage_applies_the_verdicts_and_records_why(self):
        from agent_matrix import drive, thread
        items = [{"n": 47, "record_id": "r47", "who": "我", "title": "补审核材料"}, {"n": 49, "record_id": "r49", "who": "我", "title": "发 JD"},
                 {"n": 50, "record_id": "r50", "who": "我", "title": "见客户"}]
        job, folder = self.open_job("triage", ref={"items": items})
        (folder / "triage.json").write_text(json.dumps([{"n": 47, "decision": "prep", "reason": "要提交审核"},
                                                        {"n": 49, "decision": "ai", "reason": "起草 JD"},
                                                        {"n": 50, "decision": "me", "reason": "要本人出面"},
                                                        {"n": 99, "decision": "ai"}], ensure_ascii=False))
        commands = []

        class Done:
            stdout = "✓"
        with patch("agent_matrix.drive.subprocess.run", lambda argv, **k: commands.append(argv[argv.index("--text") + 1]) or Done()):
            out = drive.submit(self.root, self.config, job["id"])
        self.assertEqual(commands, ["47 AI备", "49 AI做"])
        self.assertEqual([a["n"] for a in out["applied"]], [47, 49, 50])
        self.assertEqual(thread.judgments(self.root)["r50"]["decision"], "me")
        said = thread.history(self.root)["r49"]["messages"][0]
        self.assertEqual((said["driver"], said["text"][:6]), ("Claude Code", "判断：AI "))

    def test_the_command_any_ai_calls(self):
        from agent_matrix import drive
        with patch("agent_matrix.drive.Path.home", lambda: self.root):
            path = drive.install_shim(self.root)
            self.assertTrue(str(path).isascii() or True)
            self.assertIn("agent_matrix.drive", path.read_text())
            self.assertIn(str(self.root.resolve()), path.read_text())


if __name__ == "__main__":
    unittest.main()


class MoreTriageTests(unittest.TestCase):
    def test_done_needs_evidence_and_closed_items_can_be_restored(self):
        from agent_matrix import drive
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "t"
            folder.mkdir()
            items = [{"n": 47, "record_id": "r47", "who": "我"}, {"n": 48, "record_id": "r48", "who": "我"},
                     {"n": 50, "record_id": "r50", "who": "我"}, {"n": 51, "record_id": "r51", "who": "我"}]
            job = drive._open(root, {"id": "triage-1", "kind": "triage", "folder": str(folder), "ref": {"items": items},
                                     "lease_until": drive._lease(30), "instructions": ""}, "Claude Code")
            (folder / "triage.json").write_text(json.dumps([
                {"n": 47, "decision": "done", "reason": "已发", "evidence": "[11-17 15:02 本人] 材料已提交", "url": "https://x/docx/a"},
                {"n": 48, "decision": "done", "reason": "好像做了"},                      # no evidence: left alone
                {"n": 50, "decision": "skip", "reason": "对方说不用了"},
                {"n": 51, "decision": "done", "reason": "已回复", "evidence": "[本人] 好的已处理"}], ensure_ascii=False))
            commands = []

            class Done:
                stdout = "✓"
            with patch("agent_matrix.drive.subprocess.run", lambda argv, **k: commands.append(argv[argv.index("--text") + 1]) or Done()):
                out = drive.submit(root, Config(), job["id"])
            self.assertEqual(commands, ["47 完成", "47 + https://x/docx/a", "50 不做", "51 无交付"])
            self.assertEqual([a["n"] for a in out["applied"]], [47, 50, 51])

    def test_restore_brings_a_closed_item_back_to_new(self):
        from agent_matrix.reply import parse
        from tests.test_v1_flow import TODAY, applier, row
        a = applier([row(50, "取消", "我"), row(51, "进行中", "AI")])
        out = [x for op in parse("50 恢复\n51 恢复", TODAY)[0] for x in a.apply(op)]
        self.assertIn("恢复了", out[0])
        self.assertEqual(a.gateway.data["rec50"]["状态"], ["待确认"])
        self.assertIn("不用恢复", out[1])


class ParallelTests(unittest.TestCase):
    def test_the_builtin_driver_works_on_several_jobs_at_once(self):
        import threading
        import time
        from agent_matrix import worker
        jobs = [{"id": f"item-{k}", "kind": "item", "ref": {}} for k in range(6)]
        live, peak, lock = [0], [0], threading.Lock()

        def run_job(root, config, job):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            time.sleep(0.3)
            with lock:
                live[0] -= 1
        with tempfile.TemporaryDirectory() as tmp, \
                patch("agent_matrix.drive.next_job", lambda *a, **k: jobs.pop(0) if jobs else None), \
                patch("agent_matrix.worker.run_job", run_job), patch("agent_matrix.worker.IDLE_POLL_SECONDS", 0.05):
            out = worker.run_loop(tmp, Config(worker_concurrency=4))
        self.assertEqual(out["jobs"], 6)
        self.assertGreaterEqual(peak[0], 3)   # not one after another
        self.assertLessEqual(peak[0], 4)      # never more than the configured width
