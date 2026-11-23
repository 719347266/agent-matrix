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

    def test_wanting_slack_is_a_step_and_the_token_never_goes_through_the_ai(self):
        thread.set_setting(self.root, "want_slack", True)
        by = {x["key"]: x for x in self.report(BARE)["items"]}
        self.assertTrue(by["slack"]["need"] and not by["slack"]["ok"] and by["me"]["need"])
        steps = "\n".join(by["slack"]["steps"])
        for words in ("api.slack.com/apps", "search:read", "xoxp-", "不要让他把 Token 发到对话里"):
            self.assertIn(words, steps)
        by = {x["key"]: x for x in self.report({**BARE, "slack": {"team": "T"}})["items"]}
        self.assertTrue(by["slack"]["ok"])
        self.assertEqual(setupcheck.parse(["want_slack=true"]), {"want_slack": True})

    def test_wechat_wanted_reading_behind_or_broken(self):
        thread.set_setting(self.root, "want_wechat", True)
        by = {x["key"]: x for x in self.report(BARE)["items"]}
        steps = "\n".join(by["wechat"]["steps"])
        self.assertTrue(by["wechat"]["need"] and by["me"]["need"])
        for words in ("npm install -g @canghe_ai/wechat-cli", "wechat-cli init", "同意再做", "set wechat_enabled=true"):
            self.assertIn(words, steps)
        on = {**BARE, "wechat": {"installed": True, "enabled": True, "status": {"ok": True, "behind": True}}}
        w = {x["key"]: x for x in self.report(on)["items"]}["wechat"]
        self.assertFalse(w["need"])                                          # it works, only less well: not a blocker
        self.assertIn("最后一条", w["say"]); self.assertIn("guide wechat", w["steps"][0]); self.assertIn("wechat-cli init", w["steps"][1])
        broken = {**BARE, "wechat": {"installed": True, "enabled": True, "status": {"ok": False, "error": "微信没有打开"}}}
        w = {x["key"]: x for x in self.report(broken)["items"]}["wechat"]
        self.assertTrue(w["need"] and "微信没有打开" in w["say"])
        fine = {**BARE, "wechat": {"installed": True, "enabled": True, "status": {"ok": True}}}
        self.assertTrue({x["key"]: x for x in self.report(fine)["items"]}["wechat"]["ok"])
        self.assertEqual(setupcheck.parse(["wechat_enabled=true"]), {"wechat_enabled": True})

    def test_feishu_connected_but_unreadable_is_a_step_with_the_reason(self):
        (self.root / "config.yaml").write_text("intake_mode: all\nowner_name: 王小明\n", encoding="utf-8")
        thread.set_setting(self.root, "engine_test:codex", {"ok": True})
        broken = {**READY, "record": {"feishu_ready": True, "feishu_broken": {"why": "飞书登录过期了，要重新登录一次", "since": "11-20 14:36"}}}
        rep = self.report(broken)
        item = {x["key"]: x for x in rep["items"]}["feishu"]
        self.assertFalse(rep["ready"])
        self.assertTrue(item["need"] and not item["ok"])
        self.assertIn("飞书登录过期了", item["say"]); self.assertIn("11-20 14:36", item["say"])
        self.assertIn("lark-cli auth login", "\n".join(item["steps"]))
        self.assertIn("【要处理】飞书", setupcheck.text(rep))

    def test_what_keeps_failing_on_windows_becomes_steps_the_ai_can_follow(self):
        from agent_matrix import activity
        from agent_matrix.feishu import FeishuError
        (self.root / "config.yaml").write_text("intake_mode: all\nowner_name: 王小明\n", encoding="utf-8")
        thread.set_setting(self.root, "engine_test:codex", {"ok": True})
        scope = FeishuError("missing_scope", detail="required scopes: vc:meeting:readonly minutes:minutes:readonly")
        proxy = FeishuError("network", detail='Post "https://accounts.feishu.cn": proxyconnect tcp: dial tcp 127.0.0.1:7890: connectex')
        for _ in range(5):   # every minute the same: one line in the log
            activity.log(self.root, "error", "会议 check 出错", detail=str(scope), status="failed")
        activity.log(self.root, "error", "后台定时任务出错：snapshot", detail=f"FeishuError {proxy}", status="failed")
        activity.log(self.root, "error", "代做 check 出错", detail="事项表缺少必要字段：预做凭据、预做指令", status="failed")
        import sqlite3
        db = sqlite3.connect(next((self.root / "state").glob("activity*.db")))
        self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='error'").fetchone()[0], 3)
        db.close()
        found = activity.problems(self.root)
        self.assertEqual(found, {"proxy": "127.0.0.1:7890", "scopes": ["minutes:minutes:readonly", "vc:meeting:readonly"], "fields": "预做凭据、预做指令"})
        rep = self.report({**READY, "record": {"feishu_ready": True}, "problems": found})
        items = {x["key"]: x for x in rep["items"]}
        self.assertFalse(rep["ready"])
        self.assertIn("127.0.0.1:7890", items["proxy"]["say"]); self.assertIn("不要替他改系统网络设置", "\n".join(items["proxy"]["steps"]))
        self.assertIn('lark-cli auth login --scope "minutes:minutes:readonly vc:meeting:readonly"', "\n".join(items["feishu_scope"]["steps"]))
        self.assertIn("setup-fields", "\n".join(items["feishu_fields"]["steps"]))
        self.assertNotIn("feishu_scope", {x["key"] for x in self.report({**READY, "record": {"feishu_ready": False}, "problems": found})["items"]})

    def test_any_ai_that_says_hello_does_the_work_and_is_missed_when_it_stops_asking(self):
        from datetime import datetime, timedelta
        from agent_matrix import aidrivers
        from agent_matrix.timeutil import TZ
        by = {x["key"]: x for x in self.report(BARE)["items"]}
        self.assertIn("你自己来当心响的 AI", by["engine"]["steps"][0])            # no local AI: the simplest way out comes first
        aidrivers.hello(self.root, "Antigravity", app="Antigravity", model="gemini-3")
        self.assertEqual((thread.setting(self.root, "engine", ""), thread.setting(self.root, "engine_sort", "")), ("driver:Antigravity", "driver:Antigravity"))
        by = {x["key"]: x for x in self.report(BARE)["items"]}
        self.assertEqual((by["engine"]["ok"], by["engine"]["title"]), (True, "做事的 AI：Antigravity"))
        self.assertIn("按定时任务来领活", by["engine"]["say"])
        known = thread.setting(self.root, aidrivers.KEY, None)
        known["Antigravity"]["seen"] = (datetime.now(TZ) - timedelta(minutes=30)).isoformat()
        thread.set_setting(self.root, aidrivers.KEY, known)
        item = {x["key"]: x for x in self.report(BARE)["items"]}["engine"]
        self.assertFalse(item["ok"]); self.assertIn("现在不在", item["say"])
        self.assertIn('next --driver "Antigravity"', item["steps"][0]); self.assertIn("定时任务", item["steps"][0])
        self.assertNotIn("--wait", "\n".join(item["steps"]))
        self.assertIn("hello --name", item["steps"][1])                          # or another AI takes over

    def test_the_desktop_client_and_another_ai(self):
        thread.set_setting(self.root, "engine", "gemini")
        system = {**BARE, "here": {**BARE["here"], "platform": "windows", "client": True}}
        by = {x["key"]: x for x in self.report(system)["items"]}
        self.assertIn("重新打开「心响」", by["listener"]["steps"][0])
        self.assertIn("winget install OpenJS.NodeJS.LTS", "\n".join(by["engine"]["steps"]))
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
        for words in ("/x/bin/xinxiang-setup doctor", "不要让我自己去敲", "不要在对话里问我要密码", "hello --name", "next --driver",
                      "gate --driver", "定时任务", "不要用 --wait 守着"):
            self.assertIn(words, said)
        self.assertNotIn("--wait 600", said)                                    # nobody is asked to stay on the line
        self.assertEqual(setupcheck.parse(["owner_name=王小明", "owner_aliases=小王，明哥", "want_feishu=true", "engine=claude"]),
                         {"owner_name": "王小明", "owner_aliases": ["小王", "明哥"], "want_feishu": True, "engine": "claude"})
        with self.assertRaises(ValueError):
            setupcheck.parse(["worker_gemini_api_key=abc"])                  # secrets do not go through the AI
        with self.assertRaises(ValueError):
            setupcheck.parse(["owner_name"])


if __name__ == "__main__":
    unittest.main()


class GuideTests(unittest.TestCase):
    def test_any_ai_can_read_how_to_connect_wechat(self):
        import io, tempfile
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as d, redirect_stdout(io.StringIO()) as out:
            (Path(d) / "config.yaml").write_text("item_store: device\n", encoding="utf-8")
            self.assertEqual(setupcheck.main(["guide", "wechat", "--config", str(Path(d) / "config.yaml")]), 0)
        text = out.getvalue()
        for part in ("github.com/freestylefly/wechat-cli", "npm install -g @canghe_ai/wechat-cli", "完全磁盘访问权限", "管理员", "init --force", "sessions --limit 3", "重新签名"):
            self.assertIn(part, text)
