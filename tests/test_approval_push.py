"""开发模式 on the phone: 批准/拒绝 commands, Feishu card buttons, Slack push + taps, the websocket framing, expiry."""
import json
from pathlib import Path
import socket
import tempfile
import unittest

from agent_matrix import approval_push, approvals
from agent_matrix.models import Config


class FakeState:
    def __init__(self):
        self.kv = {}

    def get(self, key, default=None):
        return self.kv.get(key, default)

    def set(self, key, value):
        self.kv[key] = value


class FakeSlack:
    def __init__(self):
        self.calls = []

    def post(self, tok, method, **params):
        self.calls.append((tok, method, params))
        return {"ok": True, "channel": "D1", "ts": str(len(self.calls))}

    def call(self, tok, method, **params):
        return {"ok": True, "user_id": "UOWNER"}


CONFIG = Config(slack_token="xoxp-u", slack_bot_token="xoxb-b", slack_app_token="xapp-a")


class ApprovalPushTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_command(self):
        a = approvals.ask(self.root, "rec", "Bash", {"command": "git push"}, "收尾")
        b = approvals.ask(self.root, "rec", "Bash", {"command": "npm publish"})
        self.assertIsNone(approvals.command(self.root, "3 过"))
        self.assertIn("找不到", approvals.command(self.root, "批准 000000000000"))
        self.assertEqual(approvals.command(self.root, f"批准 {a}"), "✓ 已批准：git push")
        self.assertIn("已经处理过了（已批准）", approvals.command(self.root, f"拒绝 {a}"))
        self.assertTrue(approvals.command(self.root, f"拒绝 {b} 先别发版").startswith("✓ 已拒绝"))
        self.assertEqual(approvals.get(self.root, b)["note"], "先别发版")

    def test_feishu_card_buttons_run_the_command(self):
        a = approvals.ask(self.root, "rec", "Bash", {"command": "git push"})
        card = json.dumps(approval_push.feishu_card(approvals.get(self.root, a)), ensure_ascii=False)
        self.assertIn(f'"cmd": "批准 {a}"', card)
        self.assertIn(f'"cmd": "拒绝 {a}"', card)

    def test_feishu_push_once_and_retry_only_unsent(self):
        from agent_matrix.models import MatrixError
        from agent_matrix.state import State
        state = State(self.root / "state.db")
        try:
            a = approvals.ask(self.root, "rec", "Bash", {"command": "git push"})
            cards = []

            class Client:
                root, can_message_self = self.root, True

                def send_card(self, card, key, channel=None):
                    if not cards:
                        cards.append(None)
                        exc = MatrixError("dry-run failed"); exc.not_sent = True
                        raise exc
                    cards.append((key, channel))
                    return {"message_id": "om_1"}
            config = Config(owner_open_id="ou_x")
            with self.assertRaises(MatrixError):
                approval_push.push_feishu(config, Client(), state)
            self.assertEqual(approval_push.push_feishu(config, Client(), state), 1)   # surely unsent → retried
            self.assertEqual(approval_push.push_feishu(config, Client(), state), 0)
            self.assertEqual(cards[-1], ("approval-" + a, "feishu"))   # to Feishu even when notices stay local
        finally:
            state.close()

    def test_slack_push_once_then_close(self):
        fake, state = FakeSlack(), FakeState()
        a = approvals.ask(self.root, "rec", "Bash", {"command": "git push"})
        self.assertEqual(approval_push.push_slack(self.root, CONFIG, state, api=fake.post, call=fake.call), 1)
        self.assertEqual(approval_push.push_slack(self.root, CONFIG, state, api=fake.post, call=fake.call), 0)
        tok, method, params = fake.calls[0]
        self.assertEqual((tok, method, params["channel"]), ("xoxb-b", "chat.postMessage", "UOWNER"))
        self.assertIn('"action_id": "approve"', params["blocks"])
        approvals.decide(self.root, a, True)   # decided in 心响
        approval_push.push_slack(self.root, CONFIG, state, api=fake.post, call=fake.call)
        self.assertEqual(fake.calls[-1][1], "chat.update")
        self.assertNotIn("actions", fake.calls[-1][2]["blocks"])
        self.assertEqual(approval_push.push_slack(self.root, CONFIG, state, api=fake.post, call=fake.call), 0)

    def test_slack_tap_only_from_owner(self):
        fake, state = FakeSlack(), FakeState()
        a = approvals.ask(self.root, "rec", "Bash", {"command": "git push"})
        tap = lambda user, action: approval_push.on_slack_action(
            self.root, CONFIG, state, {"type": "block_actions", "user": {"id": user}, "channel": {"id": "D1"},
                                       "message": {"ts": "9"}, "actions": [{"action_id": action, "value": a}]},
            api=fake.post, call=fake.call)
        self.assertEqual(tap("USTRANGER", "approve"), "ignored")
        self.assertEqual(approvals.get(self.root, a)["status"], "pending")
        self.assertTrue(tap("UOWNER", "deny").startswith("✓ 已拒绝"))
        self.assertEqual(approvals.get(self.root, a)["status"], "denied")
        self.assertEqual(fake.calls[-1][1], "chat.update")
        self.assertTrue(state.get("approval:slack")[a]["closed"])

    def test_websocket_frames(self):
        here, there = socket.socketpair()
        with here, there:
            big = json.dumps({"envelope_id": "e", "x": "长" * 30000}).encode()
            import threading

            def server():
                approval_push.ws_send(here, b"ping!", op=9)      # answered with a pong, not returned
                approval_push.ws_send(here, big)                 # 64-bit length
                approval_push.ws_send(here, b"", op=8)
            writer = threading.Thread(target=server)
            writer.start()
            stream = there.makefile("rb")
            self.assertEqual(approval_push.ws_recv(there, stream), big.decode())
            self.assertIsNone(approval_push.ws_recv(there, stream))
            writer.join()
            pong = here.recv(2)
            self.assertEqual(pong[0] & 0x0F, 10)

    def test_unanswered_request_expires(self):
        from unittest.mock import patch
        from agent_matrix.engines import ClaudeRun
        a = approvals.ask(self.root, "rec", "Bash", {"command": "git push"})
        entry = approvals.get(self.root, a)
        approvals._write(self.root, {**entry, "at": "2020-01-01T00:00:00+08:00"})
        run = ClaudeRun(Config(), self.root, guard={"root": self.root, "mode": "risky"})
        run.waiting = {a: ("r1", {"command": "git push"})}
        sent = []
        with patch.object(ClaudeRun, "_reply", lambda self, rid, resp: sent.append((rid, resp))):
            run._poll()
        self.assertEqual(approvals.get(self.root, a)["status"], "expired")
        self.assertEqual((sent[0][0], sent[0][1]["behavior"]), ("r1", "deny"))
        self.assertIn("小时没回", sent[0][1]["message"])

    def test_connect_approvals_checks_before_saving(self):
        from agent_matrix.config import load
        from agent_matrix.models import MatrixError
        from agent_matrix.sources import slack
        (self.root / "config.yaml").write_text("slack_token: xoxp-u\n")
        with self.assertRaises(MatrixError):
            slack.connect_approvals(self.root, "xoxp-wrong", "xapp-a")

        def closed(tok, method, **_):
            raise MatrixError("Slack apps.connections.open 失败：invalid_auth")
        with self.assertRaises(MatrixError):
            slack.connect_approvals(self.root, "xoxb-b", "xapp-a", api=lambda *a, **k: {"user": "xinxiang"}, opener=closed)
        self.assertEqual(load(self.root / "config.yaml").slack_bot_token, "")
        out = slack.connect_approvals(self.root, "xoxb-b", "xapp-a", api=lambda *a, **k: {"user": "xinxiang"},
                                      opener=lambda *a, **k: {"url": "wss://x"})
        self.assertEqual(out, {"bot": "xinxiang"})
        self.assertTrue(approval_push.slack_ready(load(self.root / "config.yaml")))
        slack.disconnect_approvals(self.root)
        self.assertFalse(approval_push.slack_ready(load(self.root / "config.yaml")))

    def test_items_waiting_on_owner(self):
        row = lambda rid, n, status, who, **kw: {"record_id": rid, "编号": n, "标题": f"事{n}", "状态": status, "执行方": who, **kw}
        rows = [row("a", 1, "待确认", "我"), row("b", 2, "待确认", "别人"), row("c", 3, "卡住", "AI", 卡点="缺报价单"),
                row("d", 4, "卡住", "我"), row("e", 5, "待验收", "AI", 交付物链接="https://x.test/doc"),
                row("f", 6, "进行中", "AI"), row("g", 7, "待确认", "我", 排除本人提醒=True)]
        self.assertEqual(sorted(approval_push.attention(rows)), ["a", "c", "e"])
        fake, state = FakeSlack(), FakeState()
        self.assertEqual(approval_push.push_items(self.root, CONFIG, state, rows, api=fake.post, call=fake.call), 3)
        self.assertEqual(approval_push.push_items(self.root, CONFIG, state, rows, api=fake.post, call=fake.call), 0)
        blocks = {json.loads(c[2]["blocks"])[0]["text"]["text"]: c[2]["blocks"] for c in fake.calls}
        self.assertIn('"a|1 我来"', blocks["#1 事1"])
        self.assertIn('"a|1 AI做"', blocks["#1 事1"])
        self.assertIn('"a|1 不做"', blocks["#1 事1"])
        self.assertIn('"c|3 继续"', blocks["#3 事3"])
        self.assertIn("缺报价单", blocks["#3 事3"])
        self.assertIn('"e|5 过"', blocks["#5 事5"])
        self.assertIn("https://x.test/doc", blocks["#5 事5"])
        rows[0] = row("a", 1, "进行中", "AI")   # handled in 心响 → that message shows the outcome, no new one
        rows[2] = row("c", 3, "待验收", "AI")   # AI finished after being stuck → a new message to accept it
        fake.calls.clear()
        approval_push.push_items(self.root, CONFIG, state, rows, api=fake.post, call=fake.call)
        self.assertEqual(sorted(c[1] for c in fake.calls), ["chat.postMessage", "chat.update", "chat.update"])
        self.assertTrue(any("现在：进行中" in c[2]["text"] for c in fake.calls if c[1] == "chat.update"))
        self.assertEqual(state.get("attention:slack")["c"]["status"], "待验收")

    def test_item_tap_runs_the_reply_command(self):
        fake, state, ran = FakeSlack(), FakeState(), []
        state.set("attention:slack", {"a": {"status": "待确认", "channel": "D1", "ts": "9"}})

        def handle(config, client, st, cmd, *, source, message_ref):
            ran.append((cmd, source))
            return "#1 事1 → 交给 AI ✓"
        tap = lambda user: approval_push.on_item_tap(
            CONFIG, None, state, {"user": {"id": user}, "channel": {"id": "D1"},
                                  "message": {"ts": "9", "blocks": [{"type": "header"}, {"type": "actions"}]},
                                  "actions": [{"action_id": "item-1", "value": "a|1 AI做", "action_ts": "1.2"}]},
            handle=handle, api=fake.post, call=fake.call)
        self.assertEqual(tap("USTRANGER"), "ignored")
        self.assertEqual(ran, [])
        self.assertEqual(tap("UOWNER"), "#1 事1 → 交给 AI ✓")
        self.assertEqual(ran, [("1 AI做", "slack")])
        self.assertNotIn("actions", fake.calls[-1][2]["blocks"])   # buttons gone, outcome shown
        self.assertTrue(state.get("attention:slack")["a"]["closed"])

    def test_typed_reply_in_dm(self):
        fake, state, ran = FakeSlack(), FakeState(), []
        state.set("attention:slack", {"a": {"status": "待验收", "channel": "D1", "ts": "100.1", "n": 32}})

        def handle(config, client, st, text, *, source, message_ref):
            ran.append(text)
            return None if text == "你好" else f"{text} ✓"
        say = lambda text, **kw: approval_push.on_text(
            CONFIG, None, state, {"type": "message", "channel_type": "im", "channel": "D1", "user": "UOWNER",
                                  "text": text, "ts": kw.pop("ts", "1"), **kw}, handle=handle, api=fake.post, call=fake.call)
        self.assertEqual(say("32 退 少了定价", ts="2"), "32 退 少了定价 ✓")
        self.assertEqual(say("退 少了定价", ts="3", thread_ts="100.1"), "32 退 少了定价 ✓")   # number from the thread
        self.assertEqual(fake.calls[-1][2]["thread_ts"], "100.1")                              # answered in that thread
        self.assertEqual(say("拒绝 0123456789ab 先别推", ts="4", thread_ts="100.1"), "拒绝 0123456789ab 先别推 ✓")
        self.assertIn("没看懂", say("你好", ts="5"))
        self.assertEqual(say("32 过", ts="2"), "seen")                                          # Slack resent it
        self.assertEqual(say("32 过", ts="6", user="USTRANGER"), "ignored")
        self.assertEqual(say("32 过", ts="7", bot_id="B1"), "ignored")                          # the bot's own answers
        self.assertEqual(len(ran), 4)
        ran.clear()
        self.assertEqual(say("过", ts="8"), "32 过 ✓")          # main DM, one item waiting → that one
        state.set("attention:slack", {**state.get("attention:slack"), "b": {"status": "待确认", "channel": "D1", "ts": "100.2", "n": 40}})
        self.assertIn("#32、#40", say("过", ts="9"))           # two waiting → asks which, runs nothing
        self.assertIn("没看懂", say("你好", ts="10"))         # chat is never taken as about an item
        self.assertEqual(ran, ["32 过", "你好"])                # chat goes through as typed, untouched


if __name__ == "__main__":
    unittest.main()
