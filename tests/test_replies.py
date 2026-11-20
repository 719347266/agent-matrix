"""Replies the AI drafts for the person who asked: shown in 心响, sent only when the owner presses 发送."""
import json
import tempfile
import unittest
from pathlib import Path

from agent_matrix import replies, thread
from agent_matrix.models import Config, MatrixError

SLACK = "https://acme.slack.com/archives/{c}/p1795085477898239"


class ReplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "state").mkdir()

    def test_where_a_reply_goes_comes_from_the_chat_message_the_item_came_from(self):
        t = lambda key: replies.target({"去重键": key})
        self.assertEqual(t("chat:om_abc123:9f8e7d6c5b"), {"kind": "lark", "message_id": "om_abc123"})
        self.assertEqual(t(f"chat:{SLACK.format(c='D0B45')}:9f8e7d6c5b"),            # a DM: a plain answer
                         {"kind": "slack", "channel": "D0B45", "thread_ts": None})
        self.assertEqual(t(f"chat:{SLACK.format(c='C06T7')}:9f8e7d6c5b"),            # a channel: under the message
                         {"kind": "slack", "channel": "C06T7", "thread_ts": "1795085477.898239"})
        self.assertEqual(t(f"chat:{SLACK.format(c='C06T7')}?thread_ts=1795000000.000100&cid=C06T7:9f8e7d6c5b")["thread_ts"],
                         "1795000000.000100")                                        # already in a thread: that thread
        for other in ("chat:wechat:abc:9f8e", "meeting:xyz", "feishu-task:guid", "", None):
            self.assertIsNone(t(other))

    def test_nothing_is_sent_until_the_owner_presses_send_and_then_only_once(self):
        row = {"record_id": "recA1", "去重键": "chat:om_abc123:9f8e7d6c5b"}
        calls = []
        lark = type("L", (), {"_exec": lambda self, argv: calls.append(argv) or {"message_id": "om_sent"}})()
        replies.save(self.root, "recA1", "@张三 弹窗的问题修好了，链接在这")
        self.assertEqual(replies.get(self.root, "recA1")["status"], "draft")
        self.assertEqual(calls, [])                                                   # saving a draft sends nothing
        out = replies.send(self.root, Config(), row, "@张三 弹窗修好了（我改了一下措辞）", lark=lark)
        self.assertEqual(out["where"], "lark")
        argv = calls[0]
        self.assertEqual(argv[:4], ["im", "+messages-reply", "--message-id", "om_abc123"])
        # the owner's edited words go out, as JSON content: "@…" at the start must never be read as a file name
        self.assertNotIn("--text", argv)
        self.assertEqual(json.loads(argv[argv.index("--content") + 1]), {"text": "@张三 弹窗修好了（我改了一下措辞）"})
        self.assertEqual(argv[argv.index("--as") + 1], "user")
        self.assertEqual(replies.get(self.root, "recA1")["status"], "sent")
        with self.assertRaises(MatrixError):                                          # never twice
            replies.send(self.root, Config(), row, "再发一次", lark=lark)
        self.assertIsNone(replies.save(self.root, "recA1", "新一版"))                 # a later version does not reopen it
        self.assertIn("已回复对方", thread.history(self.root, ["recA1"])["recA1"]["messages"][-1]["text"])

    def test_slack_reply_goes_into_the_message_thread_and_dropped_drafts_stay_unsent(self):
        posted = []
        row = {"record_id": "recB2", "去重键": f"chat:{SLACK.format(c='C06T7')}:9f8e7d6c5b"}
        replies.save(self.root, "recB2", "已合入 dev")
        replies.send(self.root, Config(slack_token="xoxp-1"), row, "已合入 dev",
                     slack_post=lambda tok, method, **p: posted.append((method, p)) or {"ts": "1.2"})
        self.assertEqual(posted, [("chat.postMessage", {"channel": "C06T7", "text": "已合入 dev", "thread_ts": "1795085477.898239"})])
        replies.save(self.root, "recC3", "不想发的")
        self.assertTrue(replies.discard(self.root, "recC3"))
        with self.assertRaises(MatrixError):
            replies.send(self.root, Config(slack_token="xoxp-1"), {**row, "record_id": "recC3"}, "不想发的",
                         slack_post=lambda *a, **k: self.fail("a dropped draft must not be sent"))

    def test_an_item_from_somewhere_else_cannot_be_sent_and_bad_ids_are_refused(self):
        replies.save(self.root, "recD4", "会上说的那份材料发你了")
        with self.assertRaisesRegex(MatrixError, "没法直接回复"):
            replies.send(self.root, Config(), {"record_id": "recD4", "去重键": "meeting:xyz"}, "会上说的那份材料发你了")
        self.assertEqual(replies.get(self.root, "recD4")["status"], "draft")          # still there to copy
        self.assertIsNone(replies.get(self.root, "../../etc/passwd"))

    def test_the_result_file_may_carry_a_reply_only_as_text(self):
        from agent_matrix.itemwork import check_result
        work = self.root / "work"; work.mkdir()
        (work / "结果.json").write_text(json.dumps({"outcome": "done", "template": "其他", "summary": "x", "sources": ["a"],
                                                   "reply": {"text": "不是文字"}}), encoding="utf-8")
        self.assertIn("reply", check_result(work, None)[1])


if __name__ == "__main__":
    unittest.main()
