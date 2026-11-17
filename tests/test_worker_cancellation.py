"""Owner cancellation must win over in-flight and stale AI callbacks."""
from contextlib import contextmanager
from datetime import timedelta
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent_matrix import drive, worker
from agent_matrix.models import Config, MatrixError


class CancellationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = Config()

    def job(self, rid="rec44", **extra):
        return drive._open(self.root, {
            "id": "item-" + rid, "kind": "item", "title": "测试", "n": 44,
            "folder": str(self.root), "ref": {"record_id": rid, "version": "a" * 64},
            "lease_until": drive._lease(30), "instructions": "", **extra,
        }, "test")

    def test_every_callback_respects_owner_cancellation(self):
        j = self.job()
        drive.stop_record(self.root, "rec44")
        with patch("agent_matrix.drive._item") as item:
            for call in [
                lambda: drive.step(self.root, self.config, j["id"], "3/4"),
                lambda: drive.say(self.root, j["id"], "晚到的回复"),
                lambda: drive.ask(self.root, self.config, j["id"], "缺资料", "已经找过"),
                lambda: drive.submit(self.root, self.config, j["id"]),
                lambda: drive.release(self.root, self.config, j["id"]),
            ]:
                self.assertEqual(call()["status"], "cancelled")
            item.assert_not_called()
        self.assertEqual(drive.get(self.root, j["id"])["status"], "cancelled")

    def test_stale_progress_and_close_cannot_revive_a_cancelled_job(self):
        j = self.job()
        drive.stop_record(self.root, "rec44")
        j.update(step="迟到的进度")
        drive._save(self.root, j)
        self.assertEqual(drive.get(self.root, j["id"])["status"], "cancelled")
        drive._close(self.root, j, "done")
        self.assertEqual(drive.get(self.root, j["id"])["status"], "cancelled")

    def test_stop_includes_expired_jobs_and_does_not_touch_another_item(self):
        j = self.job(lease_until=(drive.now() - timedelta(minutes=1)).isoformat())
        other = self.job("rec45")
        self.assertEqual(drive.stop_record(self.root, "rec44"), [j["id"]])
        self.assertEqual(drive.get(self.root, other["id"])["status"], "open")

    def test_reopened_job_has_a_new_identity_and_old_callbacks_stay_cancelled(self):
        old = self.job()
        drive.stop_record(self.root, "rec44")
        new = self.job()
        self.assertNotEqual(old["id"], new["id"])
        self.assertEqual(drive.submit(self.root, self.config, old["id"])["status"], "cancelled")
        self.assertEqual(drive.get(self.root, new["id"])["status"], "open")

    def test_cancellation_during_check_prevents_publish_or_block(self):
        j = self.job()
        def checked(*args):
            drive.stop_record(self.root, "rec44")
            return {"outcome": "blocked", "question": "q", "tried": "t"}, None
        with patch("agent_matrix.drive._item") as item, patch("agent_matrix.worker.check_result", checked):
            item.return_value.workdir = self.root
            result = drive.submit(self.root, self.config, j["id"])
            self.assertEqual(result["status"], "cancelled")
            item.return_value.block.assert_not_called()
            item.return_value.publish.assert_not_called()

    def test_cancel_and_complete_stop_only_after_successful_table_write(self):
        from agent_matrix.reply import parse
        from tests.test_v1_flow import TODAY, applier, row
        for command, status in [("44 不做", "取消"), ("44 完成", "完成")]:
            with self.subTest(command=command):
                a = applier([row(44, "进行中")])
                a.client = SimpleNamespace(root=self.root)
                j = self.job()
                a.apply(parse(command, TODAY)[0][0])
                self.assertEqual(a.gateway.data["rec44"]["状态"], [status])
                self.assertEqual(drive.get(self.root, j["id"])["status"], "cancelled")
                j = self.job()
                a = applier([row(44, "进行中")])
                a.client = SimpleNamespace(root=self.root)
                with patch.object(a.gateway, "update", side_effect=MatrixError("写入失败")):
                    out = a.apply(parse(command, TODAY)[0][0])
                self.assertIn("没改成", out[0])
                self.assertEqual(drive.get(self.root, j["id"])["status"], "open")
                drive.stop_record(self.root, "rec44")

    def test_a_late_step_does_not_write_to_a_closed_table_item(self):
        from agent_matrix.prework import report_progress
        from tests.test_v1_flow import FakeGateway, row
        gateway = FakeGateway([row(44, "完成")])
        with patch("agent_matrix.workflow.Gateway", return_value=gateway):
            with self.assertRaises(MatrixError):
                report_progress(self.config, SimpleNamespace(root=self.root), None, "rec44", "迟到的进度")
        self.assertEqual(gateway.writes, [])

    def test_restoring_an_item_changes_inputs_and_clears_the_old_judgment(self):
        from agent_matrix import thread
        from agent_matrix.reply import parse
        from tests.test_v1_flow import TODAY, applier, row
        thread.judge(self.root, "rec44", "skip", "无需再做")
        a = applier([row(44, "取消", 补充材料="已有资料", 卡点="旧卡点", 验收结果=["通过"])])
        a.client = SimpleNamespace(root=self.root)
        out = a.apply(parse("44 恢复", TODAY)[0][0])
        self.assertIn("恢复了", out[0])
        restored = a.gateway.data["rec44"]
        self.assertEqual(restored["状态"], ["待确认"])
        self.assertIn("已有资料", restored["补充材料"])
        self.assertIn("本人恢复", restored["补充材料"])
        self.assertIsNone(restored["卡点"])
        self.assertIsNone(restored["验收结果"])
        self.assertNotIn("rec44", thread.judgments(self.root))

    def test_cancellation_while_background_loads_prevents_a_new_run(self):
        @contextmanager
        def local_project(*args, **kwargs):
            yield None, None
        job = {"id": "item-rec44", "kind": "item", "ref": {"record_id": "rec44", "version": "v1"}}
        with patch("agent_matrix.drive._prepare_triage", return_value=None), \
             patch("agent_matrix.drive._prepare_meeting", return_value=None), \
             patch("agent_matrix.drive._prepare_chat", return_value=None), \
             patch("agent_matrix.drive._prepare_item", return_value=job), \
             patch("agent_matrix.worker.project", local_project), \
             patch("agent_matrix.prework.current", side_effect=MatrixError("事项已取消")), \
             patch("agent_matrix.drive._open") as opened:
            with self.assertRaises(MatrixError):
                drive.next_job(self.root, self.config, "test")
            opened.assert_not_called()



class RunnerCancellationTests(unittest.TestCase):
    def test_already_stopped_job_never_launches_a_process(self):
        with tempfile.TemporaryDirectory() as tmp, patch("agent_matrix.worker.subprocess.Popen") as popen:
            run = worker.CodexRun(Config(), tmp).run("开始", should_stop=lambda: True)
        self.assertTrue(run.stopped)
        popen.assert_not_called()

    def test_old_conversation_fallback_also_has_stop_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".thread").write_text("old-thread")
            item = SimpleNamespace(rid="rec44", workdir=root, release=Mock(), mirror_reply=Mock(), show=Mock())
            first = SimpleNamespace(stopped=False, messages=0, timed_out=False, returncode=1)
            second = SimpleNamespace(stopped=True, thread=None, messages=0, timed_out=False, returncode=1)
            with patch("agent_matrix.drive._item", return_value=item), \
                 patch("agent_matrix.drive.get", return_value={"status": "open"}), \
                 patch("agent_matrix.worker.CodexRun") as runner, \
                 patch("agent_matrix.prework.add_note"):
                runner.return_value.run.side_effect = [first, second]
                out = worker.run_item_job(root, Config(), {"id": "j", "instructions": "开始", "builtin_prompt": "续接"})
                self.assertTrue(callable(runner.return_value.run.call_args_list[1].kwargs.get("should_stop")))
                self.assertEqual(out["status"], "cancelled")
                item.release.assert_called_once()
                item.mirror_reply.assert_not_called()

    @unittest.skipIf(os.name == "nt", "POSIX process-group regression; Windows uses taskkill /T")
    def test_stop_terminates_spawned_command_not_only_the_driver(self):
        import time
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "driver.py"
            script.write_text(
                "import subprocess,sys,pathlib,time\n"
                "sys.stdin.read()\n"
                "p=subprocess.Popen([sys.executable,'-c',"
                "'import pathlib,time;time.sleep(2);pathlib.Path(\"late.txt\").write_text(\"ran\")'])\n"
                "pathlib.Path('ready').write_text('ready')\n"
                "time.sleep(15)\n"
            )
            run = worker.CodexRun(Config(), root)
            with patch.object(run, "argv", return_value=[sys.executable, str(script)]):
                run.run("开始", should_stop=lambda: (root / "ready").exists())
            self.assertTrue(run.stopped)
            time.sleep(2.1)
            self.assertFalse((root / "late.txt").exists())
