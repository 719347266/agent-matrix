"""Setup without commands for the owner: one message to their AI, and a doctor that tells that AI what to do."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_matrix import setupcheck, thread

BARE = {"listener": "没在运行", "engines": {"codex": False, "claude": False, "gemini": False}, "slack": None,
        "record": {"feishu_ready": False}, "here": {"platform": "mac", "npm": False, "lark_cli": False, "client": False, "python": "/x/python"}}
READY = {"listener": "运行中", "engines": {"codex": True, "claude": False, "gemini": False}, "slack": None,
         "record": {"feishu_ready": True}, "here": {"platform": "mac", "npm": True, "lark_cli": True, "client": False, "python": "/x/python"}}


class SetupCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / "state").mkdir()
        (self.root / "config.yaml").write_text("intake_mode: all\n", encoding="utf-8")
        shim = patch("agent_matrix.setupshim.install_shim", return_value=Path("/x/bin/xinxiang-setup"))
        shim.start()
        self.addCleanup(shim.stop)

    def report(self, system):
        with patch("agent_matrix.app.system_info", return_value=system):
            return setupcheck.report(self.root)

    def test_a_new_computer_gets_every_step_spelled_out_for_the_ai(self):
        rep = self.report(BARE)
        by = {x["key"]: x for x in rep["items"]}
        self.assertFalse(rep["ready"])
        self.assertEqual([x["key"] for x in rep["items"] if x["need"] and not x["ok"]], ["listener", "engine"])
        steps = "\n".join(by["engine"]["steps"])
        for words in ("Node.js", "npm install -g @openai/codex", "codex login", "/x/bin/xinxiang-setup test codex"):
            self.assertIn(words, steps)
        self.assertIn("install-listen.sh", by["listener"]["steps"][0])
        self.assertFalse(by["me"]["need"])                                # nothing is read yet: the name can wait
        self.assertTrue(by["feishu"]["ok"])                               # not wanted: not a step
        said = setupcheck.text(rep)
        self.assertIn("【要处理】做事的 AI：本机 GPT", said)
        self.assertIn("/x/bin/xinxiang-setup doctor", said)

    def test_wanting_feishu_makes_it_a_step_and_asks_for_the_name(self):
        thread.set_setting(self.root, "want_feishu", True)
        by = {x["key"]: x for x in self.report(BARE)["items"]}
        self.assertTrue(by["feishu"]["need"] and not by["feishu"]["ok"])
        steps = "\n".join(by["feishu"]["steps"])
        for words in ("npm install -g @larksuite/cli", "lark-cli auth login", "agent_matrix setup", "brief_migration apply", "set meeting_watch_enabled=true"):
            self.assertIn(words, steps)
        self.assertTrue(by["me"]["need"])
        self.assertIn("set owner_name=", by["me"]["steps"][0])

    def test_the_desktop_client_and_another_ai(self):
        thread.set_setting(self.root, "engine", "gemini")
        system = {**BARE, "here": {**BARE["here"], "platform": "windows", "client": True}}
        by = {x["key"]: x for x in self.report(system)["items"]}
        self.assertIn("重新打开「心响」", by["listener"]["steps"][0])
        self.assertIn("winget install OpenJS.NodeJS.LTS", by["engine"]["steps"][0])
        self.assertIn("@google/gemini-cli", "\n".join(by["engine"]["steps"]))

    def test_everything_in_place_is_ready(self):
        (self.root / "config.yaml").write_text("intake_mode: all\nowner_name: 王小明\n", encoding="utf-8")
        thread.set_setting(self.root, "engine_test:codex", {"ok": True})
        rep = self.report(READY)
        self.assertTrue(rep["ready"])
        self.assertIn("全部就绪", setupcheck.text(rep))
        thread.set_setting(self.root, "engine_test:codex", {"ok": False, "error": "Not logged in"})
        by = {x["key"]: x for x in self.report(READY)["items"]}
        self.assertIn("Not logged in", by["engine"]["say"])
        self.assertNotIn("npm install", "\n".join(by["engine"]["steps"]))   # installed: only sign in and test again

    def test_the_message_for_the_owners_ai_and_what_it_may_set(self):
        said = setupcheck.prompt(self.root)
        for words in ("/x/bin/xinxiang-setup doctor", "不要让我自己去敲", "不要在对话里问我要密码"):
            self.assertIn(words, said)
        self.assertEqual(setupcheck.parse(["owner_name=王小明", "owner_aliases=小王，明哥", "want_feishu=true", "engine=claude"]),
                         {"owner_name": "王小明", "owner_aliases": ["小王", "明哥"], "want_feishu": True, "engine": "claude"})
        with self.assertRaises(ValueError):
            setupcheck.parse(["worker_gemini_api_key=abc"])                  # secrets do not go through the AI
        with self.assertRaises(ValueError):
            setupcheck.parse(["owner_name"])


if __name__ == "__main__":
    unittest.main()
