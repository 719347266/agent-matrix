"""心响: conversations per item, the local inbox, the local send channel and the local server."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from agent_matrix.models import Config


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_notebook_lines_become_the_conversation_once(self):
        from agent_matrix import thread
        from agent_matrix.prework import add_note
        add_note(self.root, "rec1", "本人补充：加上竞品")
        add_note(self.root, "rec1", "进度：2/4 查文档")
        add_note(self.root, "rec1", "卡住：缺收入导出；试过：搜了文档")
        add_note(self.root, "rec1", "已交付 v1：https://x.feishu.cn/docx/abc；还差竞品")
        self.assertEqual(thread.backfill(self.root), 0)      # already there: the import adds nothing twice
        msgs = thread.history(self.root)["rec1"]["messages"]
        self.assertEqual([(m["from"], m["kind"]) for m in msgs],
                         [("me", "feedback"), ("ai", "step"), ("ai", "ask"), ("ai", "deliver")])
        self.assertEqual(msgs[0]["text"], "加上竞品")
        self.assertEqual(msgs[3]["url"], "https://x.feishu.cn/docx/abc")

    def test_versions_count_up_and_the_same_document_is_one_version(self):
        from agent_matrix import thread
        v1 = thread.add_deliverable(self.root, "rec1", summary="初稿", url="https://x/docx/a", assumptions=["预算按 800"])
        v2 = thread.add_deliverable(self.root, "rec1", summary="改版", url="https://x/docx/b")
        again = thread.add_deliverable(self.root, "rec1", summary="初稿", url="https://x/docx/a")
        self.assertEqual((v1, v2, again), (1, 2, 1))
        dv = thread.history(self.root)["rec1"]["deliverables"]
        self.assertEqual([d["v"] for d in dv], [1, 2])
        self.assertEqual(dv[0]["assumptions"], ["预算按 800"])

    def test_assumptions_are_read_from_bullets_or_a_paragraph(self):
        from agent_matrix.thread import assumptions_of
        self.assertEqual(assumptions_of("# x\n## 我做的假设\n- 预算按 800\n- 只看 iOS\n## 别的\n- 不要"), ["预算按 800", "只看 iOS"])
        self.assertEqual(assumptions_of("## 我做的假设\n\n沿用原报告第 24 版。"), ["沿用原报告第 24 版。"])
        self.assertEqual(assumptions_of("没有这一节"), [])


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_text_and_cards_are_stored_once_with_actions(self):
        from agent_matrix import inbox
        from agent_matrix.brief_v1 import _block, _row_item, card
        a = inbox.add(self.root, "k1", "陈和我的通话 · 新增 1 件：你 #49\n回复：展开 49 看原话")
        self.assertEqual(inbox.add(self.root, "k1", "重复")["message_id"], a["message_id"])
        c = card("AI 做完了，请你验收", "", "green", [_block("做完了", "green", [
            _row_item(("#44 投放方案", "转化率 2.12%", "grey", None), "https://x/docx/a", cmd="44 过", cmd_label="通过")])], "hint", None)
        inbox.add(self.root, "k2", c)
        msgs = inbox.messages(self.root)
        self.assertEqual(len(msgs), 2)
        card_msg = next(m for m in msgs if m["kind"] == "done")
        row = card_msg["blocks"][0]["rows"][0]
        self.assertEqual((row["url"], row["actions"], card_msg["refs"]), ("https://x/docx/a", [{"label": "通过", "cmd": "44 过"}], [44]))
        self.assertEqual(next(m for m in msgs if m["kind"] == "new")["refs"], [49])
        self.assertEqual(inbox.unread(self.root), 2)
        inbox.mark_read(self.root, [card_msg["id"]])
        self.assertEqual(inbox.unread(self.root), 1)
        inbox.mark_read(self.root)
        self.assertEqual(inbox.unread(self.root), 0)

    def test_read_only_brief_is_kept_whole_in_the_inbox(self):
        from datetime import date
        from agent_matrix import inbox
        from agent_matrix.brief_v1 import build_card
        rows = [{"record_id": "r26", "编号": 26, "标题": "核对清单", "状态": ["待验收"], "执行方": ["AI"],
                 "交付物链接": "https://x.feishu.cn/docx/a", "预做结果摘要": "清单已整理"},
                {"record_id": "r24", "编号": 24, "标题": "定发布日期", "状态": ["待确认"], "执行方": ["我"]},
                {"record_id": "r9", "编号": 9, "标题": "写周报", "状态": ["进行中"], "执行方": ["我"], "截止日": "2026-11-17"}]
        events = [{"summary": "周会", "start_time": {"datetime": "2026-11-17T10:00:00+08:00"},
                   "end_time": {"datetime": "2026-11-17T11:00:00+08:00"}, "self_rsvp_status": "accept"}]
        c = build_card(Config(base_url="https://x.feishu.cn/base/b"), rows, events, date(2026, 11, 17), "morning")
        inbox.add(self.root, "feishu:brief:morning:2026-11-17", c, kind="brief")
        m = inbox.messages(self.root)[0]
        self.assertEqual((m["kind"], m["refs"]), ("brief", [9, 26, 24]))
        sections = [b for b in m["blocks"] if b["type"] == "section"]
        self.assertEqual([s["title"] for s in sections], ["今天的会 · 1 场", "今天你要做 · 1 件", "AI 做好了，等你看 · 1 件", "需要你定 · 1 件"])
        self.assertIn("周会", sections[0]["more"])
        self.assertEqual([(r["head"], r["hint"], r["url"], r["actions"]) for s in sections[1:] for r in s["rows"]],
                         [("#9 写周报", "今天到期", None, []), ("#26 核对清单", "清单已整理", "https://x.feishu.cn/docx/a", []),
                          ("#24 定发布日期", "还没定什么时候做完", None, [])])
        flat = json.dumps(m["blocks"], ensure_ascii=False)
        self.assertNotIn("只读", flat)                         # the Feishu footer is not repeated inside 心响
        self.assertNotIn("看板", flat)

    def test_local_channel_never_calls_feishu(self):
        from agent_matrix import inbox
        from agent_matrix.feishu import Feishu
        client = Feishu.__new__(Feishu)
        client.root, client.config, client.can_message_self = self.root, Config(owner_open_id="ou_me"), True
        client._exec = lambda *a, **k: self.fail("Feishu was called")
        receipt = client.send_self("聊天 · 新增 1 件：你 #47", "chat-1")
        self.assertTrue(receipt["message_id"].startswith("local_"))
        self.assertEqual(inbox.messages(self.root)[0]["refs"], [47])
        from agent_matrix.brief_v1 import verify_delivery
        self.assertTrue(verify_delivery(client, receipt)["verified_by_readback"])

    def test_only_the_briefs_go_to_feishu_with_a_local_copy(self):
        from agent_matrix import inbox
        from agent_matrix.feishu import Feishu
        client = Feishu.__new__(Feishu)
        client.root, client.config, client.can_message_self = self.root, Config(owner_open_id="ou_me"), True
        sent = []
        client._exec = lambda argv, **k: sent.append(argv) or {"data": {"message_id": "om_x1"}}
        receipt = client.send_card({"header": {"title": {"content": "11/18 早报"}}, "body": {"elements": []}}, "brief:morning", channel="feishu")
        self.assertEqual(receipt["data"]["message_id"], "om_x1")
        self.assertEqual(len(sent), 2)                       # dry run + real send
        self.assertEqual(inbox.messages(self.root)[0]["kind"], "brief")


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "state").mkdir()
        rows = [{"record_id": "rec44", "编号": "44", "标题": "投放方案", "执行方": "AI", "状态": "待验收", "截止日": "2026-11-18",
                 "交付物链接": "https://x.feishu.cn/docx/a", "原话引用": "[00:12] <cite>我来写</cite>", "来源": "会议", "创建时间": "2026-11-17T10:35:07"},
                {"record_id": "rec1", "编号": "1", "标题": "验收头像", "执行方": "我", "状态": "未开始"}]
        (self.root / "state/tasks.json").write_text(json.dumps({"at": "2026-11-17T17:00:00+08:00", "rows": rows}, ensure_ascii=False))
        from agent_matrix import thread
        thread.add(self.root, "rec44", "me", "feedback", "加上竞品")
        from agent_matrix.app import make_server
        self.server = make_server(self.root, 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def get(self, path, headers=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": host or f"127.0.0.1:{self.port}", **(headers or {})}
        conn.request("GET", path, headers=h)
        r = conn.getresponse()
        return r.status, r.read().decode()

    def test_the_ai_work_switch_is_saved_to_config_and_shown(self):
        from unittest.mock import patch
        from agent_matrix.config import load
        token = self.server.token
        def post(body):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            conn.request("POST", "/api/settings", json.dumps(body), {"Host": f"127.0.0.1:{self.port}", "X-Xinxiang-Token": token,
                                                                     "Content-Type": "application/json"})
            return conn.getresponse().status
        with patch("agent_matrix.workenv.kick") as kick:
            self.assertEqual(post({"prework_enabled": True}), 200)
        self.assertTrue(load(self.root / "config.yaml").prework_enabled)
        self.assertTrue(json.loads(self.get("/api/state", {"X-Xinxiang-Token": token})[1])["settings"]["prework_enabled"])
        self.assertEqual(post({"prework_enabled": False}), 200)
        self.assertFalse(load(self.root / "config.yaml").prework_enabled)
        self.assertEqual(post({"chat_skip_chats": [" bot-alerts ", "bot-alerts", ""], "chat_skip_words": ["部署通知"]}), 200)
        saved = load(self.root / "config.yaml")
        self.assertEqual((saved.chat_skip_chats, saved.chat_skip_words), (["bot-alerts"], ["部署通知"]))

    def test_only_the_page_it_serves_gets_the_token(self):
        status, page = self.get("/")
        self.assertEqual(status, 200)
        token = self.server.token
        self.assertIn(json.dumps(token), page)
        self.assertEqual(self.get("/api/state")[0], 403)
        self.assertEqual(self.get("/api/state", {"X-Xinxiang-Token": "wrong"})[0], 403)
        self.assertEqual(self.get("/api/state", {"X-Xinxiang-Token": token}, host="evil.example:80")[0], 403)
        status, body = self.get("/api/state", {"X-Xinxiang-Token": token})
        self.assertEqual(status, 200)
        data = json.loads(body)
        item = next(i for i in data["items"] if i["n"] == 44)
        self.assertEqual((item["quote"], item["url"]), ("[00:12] 我来写", "https://x.feishu.cn/docx/a"))
        kinds = [(m["from"], m["kind"]) for m in data["threads"]["44"]]
        self.assertEqual(kinds[:2], [("system", "say"), ("me", "feedback")])
        self.assertEqual(data["deliverables"]["44"][0]["v"], 1)   # delivered before versions were kept
        self.assertFalse(data["demo"])

    def test_inbox_messages_come_with_their_whole_content(self):
        from agent_matrix import inbox
        from agent_matrix.brief_v1 import _block, _read_row, card
        inbox.add(self.root, "brief", card("11/17 周二 · 早报", "你要做 1 件", "blue", [_block("今天你要做 · 1 件", "blue", [
            _read_row(("#1 验收头像", "今天到期", "orange", None))])], "只读", None), kind="brief")
        inbox.add(self.root, "old", "11/16 早报\n你要做\n#1 验收头像 · 今天到期\n其余0件 · 回复：1 过 / 1 退 意见")
        _, body = self.get("/api/state", {"X-Xinxiang-Token": self.server.token})
        box = {m["title"]: m for m in json.loads(body)["inbox"]}
        section = box["11/17 周二 · 早报"]["blocks"][1]
        self.assertEqual((section["title"], section["rows"][0]["head"]), ("今天你要做 · 1 件", "#1 验收头像"))
        self.assertEqual(box["11/16 早报"]["blocks"], [{"type": "text", "text": "你要做\n#1 验收头像 · 今天到期\n其余0件 · 回复：1 过 / 1 退 意见"}])


if __name__ == "__main__":
    unittest.main()


class TriageTests(unittest.TestCase):
    def test_only_new_unjudged_items_of_mine_are_judged(self):
        from agent_matrix.worker import _pending_new
        rows = [{"record_id": "r47", "编号": "47", "标题": "补齐审核材料", "状态": ["待确认"], "执行方": ["我"]},
                {"record_id": "r49", "编号": 49, "标题": "发送招聘JD", "状态": "待确认", "执行方": "我"},
                {"record_id": "r50", "编号": 50, "标题": "对方的事", "状态": "待确认", "执行方": "别人"},
                {"record_id": "r51", "编号": 51, "标题": "已经定了", "状态": "未开始", "执行方": "我"},
                {"record_id": "r52", "编号": 52, "标题": "不提醒", "状态": "待确认", "执行方": "我", "排除本人提醒": True}]
        self.assertEqual([i["n"] for i in _pending_new(rows, {})], [47, 49])
        self.assertEqual([i["n"] for i in _pending_new(rows, {"r47": {}})], [49])

    def test_switch_and_judgments_are_kept(self):
        from agent_matrix import thread
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(thread.setting(tmp, "auto_assign", False))
            thread.set_setting(tmp, "auto_assign", True)
            self.assertTrue(thread.setting(tmp, "auto_assign", False))
            thread.judge(tmp, "r49", "ai", "起草 JD，AI 能独立完成")
            self.assertEqual(thread.judgments(tmp)["r49"]["decision"], "ai")


class NotMineTests(unittest.TestCase):
    """Things that are not provably the owner's never land on the owner (or on the AI working for them)."""

    def setUp(self):
        from hashlib import sha256
        from agent_matrix.meeting_review import Action, Review
        self.transcript = "00:10 阿杰：这些我回头再优化一下。\n00:20 老周：小林你约个时间。"
        a = dict(text="t", quote="这些我回头再优化一下", timestamp="00:10", reference=self.transcript.splitlines()[0],
                 commitment_phrase="我回头再优化", confidence=0.95)
        self.Action, self.a = Action, a
        self.review = Review(meeting_id="1" * 19, transcript_sha256=sha256(self.transcript.encode()).hexdigest(),
                             actions=[Action(title="更新FAQ", owner_name="小王", direction="mine", **a)])
        self.meta = {"meeting_id": "1" * 19, "start_time": "2026-11-18 09:16", "owner_name": "我"}
        self.config = Config(owner_name="我", owner_open_id="ou_me")

    def test_an_owner_the_words_do_not_support_is_someone_else_and_hidden(self):
        from agent_matrix.meeting_review import repair_review, task_fields, validate_review
        review, notes = repair_review(self.review, self.transcript, self.meta)
        validate_review(review, self.transcript, self.meta)
        fields = task_fields(review.actions[0], self.config, self.meta)
        self.assertEqual((fields["执行方"], fields.get("排除本人提醒")), (["别人"], True))
        self.assertIn("不列给你", notes[0])

    def test_unknown_owner_is_not_mine_unless_it_is_clearly_mine(self):
        from agent_matrix.meeting_review import task_fields
        unknown = self.Action(title="补知识库", owner_name=None, direction="unknown", **self.a)
        self.assertEqual(task_fields(unknown, self.config, self.meta)["执行方"], ["别人"])
        self.assertTrue(task_fields(unknown, self.config, self.meta).get("排除本人提醒"))
        mine = self.Action(title="补知识库", owner_name=None, direction="mine", **self.a)
        self.assertEqual(task_fields(mine, self.config, self.meta)["执行方"], ["我"])


class PageVersionTests(unittest.TestCase):
    """An open window learns that the page file changed and reloads itself (the desktop app never reloads on its own)."""

    def test_state_and_page_carry_the_same_version_and_it_follows_the_file(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from agent_matrix import app
        html = app.PAGE.read_text(encoding="utf-8")
        self.assertIn('const PAGE = /*__PAGE__*/"";', html)
        first = app.page_version()
        self.assertRegex(first, r"^[0-9a-f]{12}$")
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "xinxiang.html"
            copy.write_text(html + "<!-- changed -->", encoding="utf-8")
            with patch.object(app, "PAGE", copy):
                self.assertNotEqual(app.page_version(), first)
