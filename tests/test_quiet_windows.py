"""On Windows nothing 心响 starts may flash a terminal window; and the sign-in check is not asked over and over."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_matrix
from agent_matrix import feishu
from agent_matrix.models import Config


class QuietWindowsTests(unittest.TestCase):
    def test_every_program_is_started_without_a_window_unless_the_caller_chose_flags(self):
        self.assertEqual(agent_matrix.quiet_flags({}, windows=True), {"creationflags": 0x08000000})
        self.assertEqual(agent_matrix.quiet_flags({"creationflags": 0}, windows=True), {"creationflags": 0x08000000})
        self.assertEqual(agent_matrix.quiet_flags({"creationflags": 0x208}, windows=True), {"creationflags": 0x208})   # a detached updater keeps its own
        self.assertEqual(agent_matrix.quiet_flags({"cwd": "."}, windows=False), {"cwd": "."})                         # elsewhere nothing changes

    def test_the_sign_in_check_is_asked_once_for_timers_firing_together(self):
        answer = {"ok": True, "data": {"appId": "cli_x", "identities": {
            "user": {"status": "ready", "available": True, "verified": True, "openId": "ou_1", "userName": "王小明"},
            "bot": {"status": "ready", "available": True, "verified": True}}}}
        calls = []

        def run(argv, **kwargs):
            calls.append(argv[1:3])
            return subprocess.CompletedProcess(argv, 0, json.dumps(answer), "")
        with tempfile.TemporaryDirectory() as tmp, patch.object(feishu.subprocess, "run", run):
            feishu._AUTH.clear()
            config = Config(item_store="feishu", base_token="b", tables={"事项表": "t"})
            first = feishu.Feishu(config, Path(tmp)).auth()
            second = feishu.Feishu(config, Path(tmp)).auth()          # another timer, a moment later
            self.assertEqual(first, second)
            self.assertEqual(len(calls), 1)
            with patch.object(feishu, "AUTH_KEEP_SECONDS", 0):         # later: asked again
                feishu.Feishu(config, Path(tmp)).auth()
            self.assertEqual(len(calls), 2)
            feishu._AUTH.clear()
            answer["data"]["identities"]["user"]["status"] = "expired"  # a bad answer is never kept
            feishu.Feishu(config, Path(tmp)).auth(_retry=False)
            feishu.Feishu(config, Path(tmp)).auth(_retry=False)
            self.assertEqual(len(calls), 4)
            feishu._AUTH.clear()


class FeishuHealthTests(unittest.TestCase):
    """Connected on paper is not connected: the page must say when Feishu cannot actually be read, and why."""

    def test_failed_sign_in_checks_are_counted_and_one_good_check_clears_them(self):
        from agent_matrix.models import MatrixError
        from agent_matrix.state import State
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "state").mkdir()
            state = State(Path(tmp) / "state/state.db")
            try:
                feishu.note_health(state)
                self.assertTrue(state.get("feishu:health")["ok"])
                for _ in range(3):
                    feishu.note_health(state, feishu.FeishuError("user_needs_refresh", authorization=True))
                health = state.get("feishu:health")
                self.assertEqual((health["ok"], health["count"], health["why"]), (False, 3, "飞书登录过期了，要重新登录一次"))
                self.assertTrue(health["since"])
                feishu.note_health(state, MatrixError("飞书机器人授权不可用。"))
                self.assertEqual((state.get("feishu:health")["count"], state.get("feishu:health")["why"]), (4, "飞书机器人授权不可用。"))
                feishu.note_health(state)
                self.assertEqual(state.get("feishu:health")["ok"], True)
            finally:
                state.close()
        self.assertEqual(feishu.why_not(feishu.FeishuError("cli_unavailable")), "找不到飞书命令行（lark-cli）")
        self.assertIn("99991663", feishu.why_not(feishu.FeishuError(99991663)))


if __name__ == "__main__":
    unittest.main()
