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


if __name__ == "__main__":
    unittest.main()
