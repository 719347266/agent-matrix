"""心响 wakes the AI it chose when work waits, stands in with a local AI when it does not come, and steps back after."""
import base64
from datetime import datetime, timedelta
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_matrix import aidrivers, drive, engines, inbox, wakeup, workenv
from agent_matrix.models import Config
from agent_matrix.thread import set_setting, setting
from agent_matrix.timeutil import TZ


class Waking(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "state").mkdir()
        self.at = datetime(2026, 11, 23, 15, 0, tzinfo=TZ)
        self.woken, self.kicked = [], []
        self.came = False
        patch.object(wakeup, "came_back", lambda root, value, since: self.came).start()
        patch.object(wakeup, "diagnose", lambda root, value, at=None, since=None: "Cindy 没开着").start()
        patch.object(wakeup, "_taking", lambda root, name: False).start()
        self.addCleanup(patch.stopall)

    def watch(self, minutes, work="meeting", ok=True):
        def wake(hook, kind):
            self.woken.append((hook or {}).get("url"))
            return {"ok": ok} if ok else {"ok": False, "why": "叫醒地址失效了"}
        return wakeup.watch(self.root, None, self.at + timedelta(minutes=minutes), work_for=lambda name: work,
                            wake=wake, kick=lambda root, config: self.kicked.append(1))

    def said(self):
        return [m.get("title") or "" for m in inbox.messages(self.root)]

    def test_work_waiting_for_cindy_wakes_it_then_a_local_ai_stands_in_and_steps_back_when_it_returns(self):
        set_setting(self.root, "engine", "cindy"); set_setting(self.root, "engine_sort", "cindy")
        aidrivers.set_hook(self.root, "cindy", "https://hooks.example.test/s/1")
        self.assertEqual(self.watch(0), [("Cindy", "woke")])
        self.assertEqual(self.watch(2), [])                                   # not again before REWAKE
        self.assertEqual(self.watch(6), [("Cindy", "woke")])                  # still waiting: woken again
        self.assertEqual(self.woken, ["https://hooks.example.test/s/1"] * 2)
        self.assertNotIn("cindy", wakeup.standing_in(self.root))
        self.assertEqual(self.watch(9), [("Cindy", "standin")])               # GRACE passed: GPT stands in
        stand = wakeup.standing_in(self.root)["cindy"]
        self.assertEqual((stand["engine"], stand["why"]), ("codex", "Cindy 没开着"))
        self.assertEqual(self.kicked, [1])
        self.assertTrue(any("本机 GPT 先替 Cindy 做了「整理会议」" in t for t in self.said()))
        # the built-in driver now takes Cindy's sorting and items, with the fallback AI
        self.assertIn("meeting", drive.local_kinds(self.root))
        self.assertTrue(drive.item_filter(self.root, "本机 GPT", builtin=True)("rec1"))
        self.assertIs(engines.engine(self.root, "sort"), engines.CodexRun)
        self.came = True
        self.assertEqual(self.watch(12, work=None), [("Cindy", "back")])
        self.assertEqual(wakeup.standing_in(self.root), {})
        self.assertNotIn("meeting", drive.local_kinds(self.root))
        self.assertFalse(drive.item_filter(self.root, "本机 GPT", builtin=True)("rec1"))

    def test_nobody_to_stand_in_says_why_once_with_an_alarm(self):
        set_setting(self.root, "engine_sort", "cindy"); set_setting(self.root, "engine_fallback", "")
        self.watch(0, ok=False)
        self.assertEqual(self.watch(9, ok=False), [("Cindy", "no-wake"), ("Cindy", "stuck")])   # one more try first
        self.watch(11, ok=False)
        alarms = [t for t in self.said() if t.startswith("⚠")]
        self.assertEqual(len(alarms), 1)
        self.assertIn("Cindy 没开着", alarms[0]); self.assertIn("叫醒地址失效了", alarms[0])
        self.assertEqual(self.kicked, [])

    def test_no_work_or_a_local_ai_means_nothing_to_wake(self):
        set_setting(self.root, "engine", "codex")
        self.assertEqual(self.watch(0), [])                                    # local AIs are started by 心响 itself
        set_setting(self.root, "engine", "cindy")
        self.assertEqual(self.watch(0, work=None), [])
        self.assertEqual(self.woken, [])

    def test_an_outside_ai_without_a_wake_address_is_stood_in_for_after_grace(self):
        aidrivers.hello(self.root, "Codex", app="Cindy", take="sort")
        self.assertEqual(self.watch(0, ok=False), [("Codex", "no-wake")])
        self.assertEqual(self.watch(9, ok=False), [("Codex", "no-wake"), ("Codex", "standin")])
        self.assertIn("driver:Codex", wakeup.standing_in(self.root))


class TheWakeRequest(unittest.TestCase):
    def test_it_is_signed_the_standard_webhooks_way_and_says_only_the_kind_of_work(self):
        seen = {}

        class Answer:
            status = 202
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def opener(request, timeout):
            seen.update(url=request.full_url, headers={k.lower(): v for k, v in request.header_items()}, body=request.data.decode())
            return Answer()
        secret = aidrivers.new_secret()
        out = aidrivers.send_wake({"url": "https://hooks.example.test/s/1", "secret": secret}, "item", opener=opener)
        self.assertTrue(out["ok"])
        h = seen["headers"]
        key = base64.b64decode(secret.split("_", 1)[1])
        want = base64.b64encode(hmac.new(key, f"{h['webhook-id']}.{h['webhook-timestamp']}.{seen['body']}".encode(), hashlib.sha256).digest()).decode()
        self.assertEqual(h["webhook-signature"], "v1," + want)
        self.assertEqual(json.loads(seen["body"]), {"type": "xinxiang.work", "work": "item"})

    def test_a_refusal_says_why_in_plain_words(self):
        import urllib.error
        def refuse(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 429, "limit", {}, None)
        out = aidrivers.send_wake({"url": "https://hooks.example.test/s/1", "secret": "s"}, "item", opener=refuse)
        self.assertFalse(out["ok"]); self.assertIn("每日上限", out["why"])
        self.assertIn("只能等它自己的定时任务", aidrivers.send_wake(None, "item")["why"])

    def test_an_address_must_be_https_and_the_secret_given_first_is_kept(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "state").mkdir()
            secret = aidrivers.pending_secret(d, "cindy")
            self.assertEqual(aidrivers.set_hook(d, "cindy", "https://hooks.example.test/s/1")["secret"], secret)
            with self.assertRaises(ValueError):
                aidrivers.set_hook(d, "cindy", "http://plain.example.test")
            self.assertIsNone(aidrivers.set_hook(d, "cindy", ""))


class Reasons(unittest.TestCase):
    def test_errors_from_the_other_app_become_something_the_owner_can_act_on(self):
        self.assertIn("登录失效", wakeup._error_words("401 Unauthorized: token expired"))
        self.assertIn("额度用完", wakeup._error_words("429 Too Many Requests: usage limit reached"))
        self.assertIn("模型现在用不了", wakeup._error_words('Model "x" is not available'))

    def test_cindy_not_running_is_said_before_anything_else(self):
        with tempfile.TemporaryDirectory() as d, patch.object(wakeup, "_app_running", return_value=False), \
             patch("agent_matrix.local_sessions.CINDY_APP", Path(d)):
            self.assertEqual(wakeup.diagnose(d, "cindy"), "Cindy 没开着")


class LocalAIs(unittest.TestCase):
    def test_the_model_api_runs_claude_on_the_owners_own_provider_without_touching_their_claude(self):
        config = Config(worker_api_provider="deepseek", worker_api_key="sk-test")
        with tempfile.TemporaryDirectory() as d, patch.object(engines, "claude_binary", return_value="/x/claude"):
            run = engines.ApiRun(config, Path(d))
            argv, env = run.argv(), run.env()
        self.assertEqual(argv[argv.index("--model") + 1], "deepseek-chat")
        self.assertEqual((env["ANTHROPIC_BASE_URL"], env["ANTHROPIC_AUTH_TOKEN"]), ("https://api.deepseek.com/anthropic", "sk-test"))
        self.assertTrue(env["CLAUDE_CONFIG_DIR"].endswith("claude-api"))
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        with tempfile.TemporaryDirectory() as d, patch.object(engines, "claude_binary", return_value="/x/claude"):
            with self.assertRaisesRegex(Exception, "模型接口还没填好"):
                engines.ApiRun(Config(), Path(d)).argv()

    def test_a_broken_node_is_skipped_for_one_that_starts(self):
        with tempfile.TemporaryDirectory() as d:
            broken, good = Path(d) / "broken", Path(d) / "good"
            for folder, script in ((broken, "#!/bin/sh\nexit 6\n"), (good, "#!/bin/sh\necho v22.0.0\n")):
                folder.mkdir()
                (folder / "node").write_text(script); (folder / "node").chmod(0o755)
            workenv._NODE.clear()
            self.addCleanup(workenv._NODE.clear)
            with patch.object(workenv, "node_candidates", return_value=[str(broken), str(good)]):
                self.assertEqual(workenv.working_node_dir(), str(good))
                self.assertTrue(workenv._env(Config())["PATH"].startswith(str(good)))


if __name__ == "__main__":
    unittest.main()
