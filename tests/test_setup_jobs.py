"""接入飞书 / Slack / 微信 with one click: the AI that drives 心响 gets it as a job, says what the owner must do by hand,
and 心响 itself checks that it really works before the ask goes away."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_matrix import aidrivers, drive, setupasks
from agent_matrix.thread import setting


class SetupJobs(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "state").mkdir()
        aidrivers.hello(self.root, "Antigravity", model="gemini-3")

    def test_only_the_driving_ai_gets_it_and_before_anything_else(self):
        setupasks.ask(self.root, "feishu")
        self.assertEqual(drive.kinds_for(self.root, "Antigravity")[0], "setup")
        aidrivers.hello(self.root, "Cursor", take="sort")
        self.assertNotIn("setup", drive.kinds_for(self.root, "Cursor"))
        with patch.object(drive, "_prepare_triage", return_value=None):
            job = drive.next_job(self.root, None, "Antigravity")
        self.assertEqual((job["kind"], job["title"]), ("setup", "接入飞书"))
        self.assertIn("doctor --only feishu", job["instructions"])
        self.assertIn("say --job", drive.public(job)["instructions"])
        self.assertIsNone(drive._prepare_setup(self.root, None, "Antigravity"), "one at a time")

    def test_what_it_says_is_shown_and_it_is_done_only_when_it_really_works(self):
        setupasks.ask(self.root, "wechat")
        job = drive.next_job(self.root, None, "Antigravity", kinds=["setup"])
        self.assertEqual(drive.say(self.root, job["id"], "请在弹出的窗口里输一次电脑密码")["ok"], True)
        self.assertEqual(setupasks.pending(self.root)["wechat"]["said"], "请在弹出的窗口里输一次电脑密码")
        with patch("agent_matrix.workenv._cli", return_value=(2, {"ready": False, "left": ["微信"], "text": "【要处理】微信"})):
            out = drive.submit(self.root, None, job["id"])
        self.assertEqual(out["status"], "fix"); self.assertIn("微信", out["problem"])
        self.assertIn("wechat", setupasks.pending(self.root))
        with patch("agent_matrix.workenv._cli", return_value=(0, {"ready": True, "left": [], "text": ""})) as cli, \
             patch("agent_matrix.inbox.add") as said:
            self.assertEqual(drive.submit(self.root, None, job["id"])["status"], "done")
        self.assertEqual(cli.call_args.args[1:4], ("agent_matrix.setupcheck", "status", "--only"))
        self.assertEqual(setupasks.pending(self.root), {})
        self.assertIn("接入微信", said.call_args.args[2])

    def test_giving_up_says_why_and_clears_the_ask(self):
        setupasks.ask(self.root, "slack")
        job = drive.next_job(self.root, None, "Antigravity", kinds=["setup"])
        drive.say(self.root, job["id"], "你的工作区不允许自建 App")
        drive.release(self.root, None, job["id"])
        self.assertEqual(setupasks.pending(self.root), {})
        self.assertEqual(setting(self.root, "setup:gave_up:slack")["why"], "你的工作区不允许自建 App")

    def test_one_tool_only_counts_what_it_needs(self):
        from agent_matrix.setupcheck import only
        rep = {"ready": False, "items": [{"key": "listener", "need": True, "ok": True}, {"key": "engine", "need": True, "ok": False},
                                         {"key": "me", "need": True, "ok": True}, {"key": "feishu", "need": False, "ok": False},
                                         {"key": "slack", "need": True, "ok": False}]}
        one = only(rep, "feishu")
        self.assertEqual([x["key"] for x in one["items"]], ["listener", "me", "feishu"])
        self.assertFalse(one["ready"]); self.assertTrue(one["items"][-1]["need"])


if __name__ == "__main__":
    unittest.main()
