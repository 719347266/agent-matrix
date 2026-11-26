"""Real-time sources: chat pre-filter, chat extraction rules, Feishu task mapping, listener replies."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
import unittest.mock
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from agent_matrix.chat_review import ChatAction, ChatReview, due_slot, task_fields as chat_fields, validate
from agent_matrix.models import Config, MatrixError
from agent_matrix.sources.chats import classify, owner_words
from agent_matrix.sources.feishu_tasks import fields_for

TZ = ZoneInfo("Asia/Shanghai")
CONFIG = Config(owner_open_id="ou_me", owner_name="王小明", owner_aliases=["小王"])


class ChatFilterTests(unittest.TestCase):
    names = owner_words(CONFIG)

    def test_keeps_only_lines_about_the_owner(self):
        keep = lambda t, **k: classify(t, names=self.names, **{"from_owner": False, "chat_mode": "group", **k})
        # everything the owner's account sees goes to the model; the kind is only what code already knows
        self.assertEqual(keep("好的，这个我来整理，明天给你", from_owner=True), "我说的")
        self.assertEqual(keep("都升级成plus了", from_owner=True), "我说的")      # the model reads "done" from it
        self.assertIsNone(keep("好的", from_owner=True))
        self.assertEqual(keep("小王，周五前帮忙看下这个需求"), "找你的")
        self.assertEqual(keep("会员页积分显示不对，谁帮忙看下"), "群聊")      # a customer's unaddressed question
        self.assertEqual(keep("那我先把app装上，你们帮忙升级一下？"), "群聊")
        self.assertEqual(keep("试了一下还是超时", replied=True), "回你的")   # a reply to the owner's post
        self.assertEqual(keep("今晚 10 点封版", broadcast=True), "全员")
        self.assertIsNone(keep("好的", replied=True))
        self.assertEqual(keep("小王 这个页面样式错位了"), "找你的")   # named, no ask word: the model decides
        self.assertEqual(keep("能不能明天把数据发我", chat_mode="p2p"), "找你的")
        self.assertIsNone(keep("哈哈哈哈哈好", chat_mode="p2p"))
        self.assertEqual(keep("哥们，你们超管后台借我登一下", chat_mode="p2p"), "找你的")  # casual private asks
        self.assertEqual(keep("兄弟，加好了和我说一下", chat_mode="p2p"), "找你的")
        self.assertEqual(keep("网页比例100%时弹窗只展示一点", chat_mode="p2p"), "找你的")  # a report, no ask word
        self.assertEqual(keep("在吗", chat_mode="p2p"), "找你的")
        for chat in ("好的👍", "收到！", "哈哈哈", ":thumbsup:", "ok", "。。。"):
            self.assertIsNone(keep(chat, chat_mode="p2p"), chat)
        self.assertEqual(keep("<http://dreame-netherlands.myshopify.com|dreame-netherlands.myshopify.com>", chat_mode="p2p"), "找你的")
        self.assertIsNone(keep("嗯嗯"))


class ChatReviewTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        folder = self.root / "state/chat-batches/b1"; folder.mkdir(parents=True)
        messages = [{"message_id": "om_1", "chat_id": "oc_1", "chat_name": "运营群", "chat_mode": "group",
                     "sender_id": "ou_x", "sender_name": "陈一凡", "sent_at": "2026-11-17 10:00",
                     "text": "小王，周五前帮忙把投放预算定一下", "context": "[]", "kind": "找你的"}]
        (folder / "candidates.json").write_text(json.dumps({"batch_id": "b1", "messages": messages}, ensure_ascii=False))

    def action(self, **k):
        base = dict(message_id="om_1", title="定投放预算", quote="周五前帮忙把投放预算定一下", commitment_phrase="帮忙把投放预算定一下",
                    owner_name="王小明", due_date="2026-11-20", due_phrase="周五", executor="我", confidence=0.9)
        return ChatAction(**{**base, **k})

    def test_named_owner_and_written_date_pass(self):
        kept = validate(CONFIG, ChatReview(batch_id="b1", actions=[self.action()]), self.root)
        self.assertEqual(len(kept), 1)
        fields = chat_fields(CONFIG, *kept[0])
        self.assertEqual((fields["执行方"], fields["状态"], fields["来源"]), (["我"], ["未开始"], ["聊天"]))
        self.assertTrue(fields["来源链接"].startswith("https://applink.feishu.cn/"))
        lark = chat_fields(CONFIG.model_copy(update={"lark_brand": "lark"}), *kept[0])
        self.assertTrue(lark["来源链接"].startswith("https://applink.larksuite.com/client/chat/open?openChatId=oc_1"))

    def test_every_private_line_needs_a_decision_and_is_kept_at_any_confidence(self):
        from agent_matrix.chat_review import ChatSkip
        folder = self.root / "state/chat-batches/b2"; folder.mkdir(parents=True)
        dm = {"chat_id": "D1", "chat_name": "Slack · cara", "chat_mode": "p2p", "sender_id": "U1", "sender_name": "cara",
              "sent_at": "2026-11-19 18:50", "context": "[]", "kind": "找你的"}
        messages = [{**dm, "message_id": "m1", "text": "网页比例100%时，这个会员弹窗只能展示一点出来"},
                    {**dm, "message_id": "m2", "text": "<http://dreame-us.myshopify.com|dreame-us.myshopify.com>"},
                    {**dm, "message_id": "m3", "chat_id": "D2", "text": "哈哈你那天说的店我去了"}]
        (folder / "candidates.json").write_text(json.dumps({"batch_id": "b2", "messages": messages}, ensure_ascii=False))
        fix = ChatAction(message_id="m1", title="会员弹窗 100% 缩放只露一点", quote="这个会员弹窗只能展示一点出来",
                         commitment_phrase="这个会员弹窗只能展示一点出来", owner_name="王小明", confidence=0.5)
        rest = [ChatSkip(message_id="m2", kind="并入另一条", why="同一个弹窗问题的店铺链接"),
                ChatSkip(message_id="m3", kind="闲聊", why="聊去过的店")]
        kept = validate(CONFIG, ChatReview(batch_id="b2", actions=[fix], skipped=rest), self.root)
        self.assertEqual([a.message_id for a, _ in kept], ["m1"])          # 0.5 is kept: it was written to the owner
        with self.assertRaisesRegex(MatrixError, "没有结论"):                 # silently left out
            validate(CONFIG, ChatReview(batch_id="b2", actions=[fix], skipped=rest[1:]), self.root)
        with self.assertRaisesRegex(MatrixError, "并入另一条"):               # nothing in that chat to go into
            validate(CONFIG, ChatReview(batch_id="b2", skipped=[*rest, ChatSkip(message_id="m1", kind="闲聊", why="x")]),
                     self.root)
        with self.assertRaises(ValueError):                                 # only the listed kinds
            ChatSkip(message_id="m3", kind="不重要", why="x")

    def test_guesses_are_rejected_and_low_confidence_dropped(self):
        for bad in ({"quote": "没说过的话"}, {"commitment_phrase": "我来负责"}, {"owner_name": "李大伟"}):
            with self.assertRaises(MatrixError):
                validate(CONFIG, ChatReview(batch_id="b1", actions=[self.action(**bad)]), self.root)
        # a date that does not follow from the words is never written: the item is kept without one (待确认)
        kept = validate(CONFIG, ChatReview(batch_id="b1", actions=[self.action(due_date="2026-11-21")]), self.root)
        self.assertEqual([a.due_date for a, _ in kept], [None])
        self.assertEqual(chat_fields(CONFIG, *kept[0])["状态"], ["待确认"])
        self.assertNotIn("截止日", chat_fields(CONFIG, *kept[0]))
        self.assertEqual(validate(CONFIG, ChatReview(batch_id="b1", actions=[self.action(confidence=0.5)]), self.root), [])

    def test_review_runs_only_at_set_times(self):
        state = SimpleNamespace(get=lambda key, default=None: None)
        fixed = CONFIG.model_copy(update={"chat_review_every_minutes": 0})
        self.assertIsNone(due_slot(fixed, state, datetime(2026, 11, 17, 10, 0, tzinfo=TZ)))
        self.assertEqual(due_slot(fixed, state, datetime(2026, 11, 17, 15, 0, tzinfo=TZ)), "2026-11-17T14:30")

    def test_review_runs_every_five_minutes_when_lines_wait(self):
        from agent_matrix.chat_review import check, prepare
        from agent_matrix.sources.chats import table
        from agent_matrix.state import State
        state = State(self.root / "state.db")
        table(state)
        at = datetime(2026, 11, 17, 10, 0, tzinfo=TZ)
        self.assertFalse(check(CONFIG, state, at)["run_model"])          # nothing waiting: keep waiting
        state.db.execute("INSERT INTO chat_candidates VALUES ('m1','c','群','group','u','陈','2026-11-17 10:00','麻烦明天发我','[]','找你的','pending',NULL)")
        state.db.commit()
        self.assertTrue(check(CONFIG, state, at)["run_model"])           # due at once: no round yet
        prepare(CONFIG, state, self.root, at)
        state.set("chat-review:open", None)
        state.db.execute("INSERT INTO chat_candidates VALUES ('m2','c','群','group','u','陈','2026-11-17 10:01','麻烦后天发我','[]','找你的','pending',NULL)")
        state.db.commit()
        self.assertFalse(check(CONFIG, state, at + timedelta(minutes=4))["run_model"])  # within 5 minutes of the last round
        self.assertTrue(check(CONFIG, state, at + timedelta(minutes=5))["run_model"])
        state.close()


class FeishuTaskTests(unittest.TestCase):
    def test_no_tasks_at_all_is_an_empty_list(self):
        from agent_matrix.sources import feishu_tasks
        empty = SimpleNamespace(_exec=lambda a: {"has_more": False, "items": None, "page_token": ""})
        self.assertEqual(feishu_tasks._list(empty, complete=False, since=datetime(2026, 11, 6)), [])
        with self.assertRaises(MatrixError):
            feishu_tasks._list(SimpleNamespace(_exec=lambda a: {"weird": [1]}), complete=False, since=datetime(2026, 11, 6))

    def test_task_becomes_owner_item_with_link(self):
        task = {"guid": "g1", "summary": "投放方案撰写", "url": "https://applink.feishu.cn/client/todo/detail?guid=g1",
                "due": {"timestamp": "1795622400000"}}
        f = fields_for(task, CONFIG, TZ)
        self.assertEqual((f["执行方"], f["状态"], f["来源"]), (["我"], ["未开始"], ["飞书任务"]))
        self.assertEqual(f["关联任务链接"], task["url"]); self.assertTrue(f["截止日"].startswith("2026-11-"))


class ListenerReplyTests(unittest.TestCase):
    def test_only_owner_private_messages_are_commands(self):
        from agent_matrix.listen import Listener
        listener = Listener.__new__(Listener)
        store = {}
        state = SimpleNamespace(get=lambda k, d=None: store.get(k, d), set=lambda k, v: store.__setitem__(k, v))
        event = {"chat_type": "group", "sender_id": {"open_id": "ou_me"}, "message_id": "om_9", "content": '{"text":"15 过"}'}
        self.assertEqual(listener.on_message(CONFIG, None, state, event), "ignored")
        event = {**event, "chat_type": "p2p", "sender_id": {"open_id": "ou_other"}}
        self.assertEqual(listener.on_message(CONFIG, None, state, event), "ignored")


if __name__ == "__main__":
    unittest.main()


class LockTests(unittest.TestCase):
    def test_waiting_run_gets_the_lock_once_the_other_finishes(self):
        import threading, time as t
        from agent_matrix.state import exclusive_lock
        from agent_matrix.models import MatrixBusy
        root = Path(tempfile.mkdtemp())
        with exclusive_lock(root):
            with self.assertRaises(MatrixBusy):
                with exclusive_lock(root, wait=0):
                    pass
        hold = threading.Event()
        def holder():
            with exclusive_lock(root):
                hold.set(); t.sleep(1.5)
        threading.Thread(target=holder).start(); hold.wait()
        start = t.monotonic()
        with exclusive_lock(root, wait=10):
            self.assertGreater(t.monotonic() - start, 1)


class MonitorViewTests(unittest.TestCase):
    def test_owner_view_groups_and_labels(self):
        from agent_matrix.monitor import classify
        now = datetime(2026, 11, 17, 11, 0, tzinfo=TZ)
        rows = [
            {"编号": 26, "标题": "备稿", "执行方": "AI", "状态": "待验收"},
            {"编号": 5, "标题": "改过的文案", "执行方": "AI", "状态": "待验收", "验收结果": "退回"},
            {"编号": 3, "标题": "找人确认", "执行方": "我", "状态": "卡住", "卡点": "等#26验收备稿"},
            {"编号": 44, "标题": "备材料", "执行方": "AI", "状态": "卡住", "卡点": "缺数据入口"},
            {"编号": 1, "标题": "验收测试版", "执行方": "我", "状态": "未开始", "截止日": "2026-11-16T00:00:00+08:00"},
            {"编号": 40, "标题": "没日期的事", "执行方": "我", "状态": "未开始"},
            {"编号": 9, "标题": "别人的", "执行方": "别人", "状态": "未开始", "截止日": "2026-11-30T00:00:00+08:00"},
            {"编号": 7, "标题": "已完成", "执行方": "AI", "状态": "完成"},
            {"编号": 50, "标题": "写好的方案", "执行方": "我", "状态": "完成", "交付情况": "待补交付", "完成时间": "2026-11-17T10:00:00+08:00"},
            {"编号": 51, "标题": "打过的电话", "执行方": "我", "状态": "完成", "交付情况": "无交付", "完成时间": "2026-11-17T09:00:00+08:00"},
            {"编号": 52, "标题": "空选项", "执行方": ["我"], "状态": ["完成"], "交付情况": [], "完成时间": "2026-11-17T08:00:00+08:00"},
        ]
        need, moving, backlog, done = classify(rows, now)
        labels = {x["n"]: x["label"] for x in need + moving}
        self.assertEqual(labels[26], "待你验收"); self.assertEqual(labels[5], "待你复查")
        self.assertEqual(labels[44], "卡住"); self.assertIn(44, [x["n"] for x in need])
        self.assertEqual(labels[3], "等 AI 备稿"); self.assertNotIn(3, [x["n"] for x in need])
        self.assertEqual(labels[1], "逾期")
        self.assertEqual([x["n"] for x in backlog], [40])
        self.assertNotIn(9, labels); self.assertNotIn(7, labels)
        self.assertEqual([(x["n"], x["label"]) for x in done], [(50, "待补交付"), (51, "无需交付"), (52, "已完成")])
        self.assertIn((50, "补交付"), [(x["n"], x["label"]) for x in need])


class SameThingTests(unittest.TestCase):
    def rows(self):
        def r(n, title, st="未开始", **k):
            return {"record_id": f"rec{n}", "编号": n, "标题": title, "状态": [st], **k}
        return [r(35, "角色默认动作需要调整为随机，并且在对话过程中需要根据情境调整"),
                r(36, "角色默认动作需要调整为不一致，并且在对话过程中需要根据情境调"),
                r(30, "广告方案撰写"), r(44, "备材料：广告方案撰写", 父事项=[{"id": "rec30"}]),
                r(3, "核对数据看板文档并取得确认"), r(31, "撰写数据看板需求文档"),
                r(8, "刷好评"), r(9, "已经做完的投放方案", st="完成"),
                r(12, "整理周报", 关联任务链接="https://applink.feishu.cn/client/todo/detail?guid=11111111-2222-3333-4444-555555555555")]

    def test_strong_match_merges_weak_one_only_suggests(self):
        from agent_matrix.merge import find_same, duplicate_pairs
        target, score, how = find_same(self.rows(), "角色默认动作需要调整为随机，对话过程中根据情境调整")
        self.assertEqual(target["编号"], 35)
        loose, loose_score, _ = find_same(self.rows(), "角色默认动作调整为随机，对话中按情境调整")
        self.assertIsNone(loose)            # a loose rewording is only suggested, never merged on its own
        self.assertGreater(loose_score, 0.45)
        self.assertIsNone(find_same(self.rows(), "刷单")[0])
        self.assertIsNone(find_same(self.rows(), "投放方案")[0])  # the only similar item is already done
        by_link = find_same(self.rows(), "完全不同的标题", task_link="https://applink.feishu.cn/client/todo/detail?guid=11111111-2222-3333-4444-555555555555")
        self.assertEqual((by_link[0]["编号"], by_link[2]), (12, "同一个飞书任务"))
        pairs = {(p["drop"], p["keep"]) for p in duplicate_pairs(self.rows())}
        self.assertIn((36, 35), pairs)
        self.assertNotIn((44, 30), pairs)  # parent and its AI preparation child belong together
        self.assertNotIn((36, 35), {(p["drop"], p["keep"]) for p in duplicate_pairs(self.rows(), ["35-36"])})

    def test_model_same_as_must_point_at_an_open_matching_item(self):
        from agent_matrix.meeting_review import _same_target, Action
        base = dict(text="t", quote="q", timestamp="00:10", reference="00:10 q", commitment_phrase="q", confidence=0.9,
                    title="写数据看板需求")
        self.assertEqual(_same_target(self.rows(), Action(**base, same_as=31))["编号"], 31)
        with self.assertRaises(MatrixError):
            _same_target(self.rows(), Action(**base, same_as=9))   # finished
        with self.assertRaises(MatrixError):
            _same_target(self.rows(), Action(**{**base, "title": "订机票"}, same_as=8))  # nothing in common

    def test_owner_merges_or_marks_distinct(self):
        from datetime import date, datetime
        from agent_matrix.reply import Applier, parse
        from types import SimpleNamespace
        data = {r["record_id"]: dict(r) for r in self.rows()}
        store = {}
        gw = SimpleNamespace(rows=lambda: list(data.values()), client=SimpleNamespace(root=tempfile.mkdtemp()),
                             update=lambda before, patch, key, source: data[before["record_id"]].update(patch))
        a = Applier.__new__(Applier)
        a.config, a.gateway, a.client = CONFIG, gw, None
        a.state = SimpleNamespace(get=lambda k, d=None: store.get(k, d), set=lambda k, v: store.__setitem__(k, v))
        a.source, a.ref, a.now = "test", "m", datetime(2026, 11, 17, 12, tzinfo=TZ)
        out = [line for op in parse("36 合并到 35", date(2026, 11, 17))[0] for line in a.apply(op)]
        self.assertIn("已并入 #35", out[0])
        self.assertEqual((data["rec36"]["状态"], data["rec36"]["排除本人提醒"]), (["取消"], True))
        self.assertIn("角色默认动作需要调整为不一致", data["rec35"]["补充材料"])
        out = [line for op in parse("31 不重复", date(2026, 11, 17))[0] for line in a.apply(op)]
        self.assertIn("不是重复", out[0])
        self.assertIn("3-31", store["merge:distinct"])


class SlackSourceTests(unittest.TestCase):
    def test_a_reply_xinxiang_sent_is_not_read_back_as_a_new_promise(self):
        from agent_matrix import replies as sent
        from agent_matrix.sources import slack
        from agent_matrix.sources.chats import pending
        from agent_matrix.state import State
        root = Path(tempfile.mkdtemp())
        state = State(root / "state.db")
        sent._write(root, "rec27", {"status": "sent", "text": "…我再去库里跑一下补给你。", "ref": "1795400010.000100"})
        history = {"D1": [{"ts": "1795400010.000100", "user": "UME", "text": "要精确的数我再去库里跑一下补给你。"},
                          {"ts": "1795400011.000100", "user": "UME", "text": "明天我把报价单发你"}]}
        replies = {"auth.test": {"user_id": "UME", "url": "https://acme.slack.com/"},
                   "users.conversations": {"channels": [{"id": "D1", "is_im": True, "user": "UCHEN", "updated": 1}]},
                   "users.info": {"user": {"profile": {"display_name": "陈一凡"}}}}
        def api(tok, method, **p):
            if method == "search.messages":
                raise MatrixError("Slack search.messages 失败：missing_scope")
            return {"messages": history[p["channel"]]} if method == "conversations.history" else replies[method]
        slack.poll(CONFIG.model_copy(update={"slack_token": "xoxp-test"}), root, state,
                   datetime(2026, 11, 17, 10, 0, tzinfo=TZ), api=api)
        self.assertEqual([m["text"] for m in pending(state)], ["明天我把报价单发你"])
        why = dict(state.db.execute("SELECT text, status FROM chat_candidates WHERE status LIKE 'drop:%'"))
        self.assertEqual(why, {"要精确的数我再去库里跑一下补给你。": "drop:心响替你发出去的回复，算在原来那件事里"})

    def test_slack_lines_land_in_chat_candidates_with_permalink(self):
        from agent_matrix.sources import slack
        from agent_matrix.sources.chats import pending
        from agent_matrix.state import State
        root = Path(tempfile.mkdtemp())
        state = State(root / "state.db")
        replies = {
            "auth.test": {"user_id": "UME", "url": "https://acme.slack.com/"},
            "users.conversations": {"channels": [{"id": "C1", "name": "ops", "updated": 2},
                                                 {"id": "D1", "is_im": True, "user": "UCHEN", "updated": 1}]},
            "users.info": {"user": {"profile": {"display_name": "陈一凡"}}},
        }
        history = {"C1": [{"ts": "1795400000.000100", "user": "UCHEN", "text": "<@UME> 周五前帮忙把投放预算定一下"},
                          {"ts": "1795400001.000100", "user": "UCHEN", "text": "周五前帮忙把投放预算定一下"},  # not addressed
                          {"ts": "1795400002.000100", "bot_id": "B1", "text": "<@UME> 明天记得发周报"}],
                   "D1": [{"ts": "1795400003.000200", "user": "UCHEN", "text": "能不能明天把数据发我"}]}
        def api(tok, method, **p):
            if method == "search.messages":
                raise MatrixError("Slack search.messages 失败：missing_scope")  # token without search:read
            return {"messages": history[p["channel"]]} if method == "conversations.history" else replies[method]
        config = CONFIG.model_copy(update={"slack_token": "xoxp-test"})
        out = slack.poll(config, root, state, datetime(2026, 11, 17, 10, 0, tzinfo=TZ), api=api)
        self.assertEqual((out["candidates"], out["via"]), (3, "list"))       # the bot line is the only one left out
        self.assertEqual(sorted(r["kind"] for r in pending(state)), ["@你", "找你的", "群聊"])
        rows = {r["chat_name"]: r for r in pending(state) if r["kind"] != "群聊"}
        self.assertEqual(rows["Slack · ops"]["message_id"], "https://acme.slack.com/archives/C1/p1795400000000100")
        self.assertIn("@王小明", rows["Slack · ops"]["text"])
        self.assertEqual(rows["Slack · 陈一凡"]["chat_mode"], "p2p")
        self.assertEqual(state.get("slack:cursor:C1"), "1795400002.000100")
        self.assertEqual(slack.poll(CONFIG, root, state, datetime(2026, 11, 17, 10, 0, tzinfo=TZ), api=api), "no_token")
        a = ChatAction(message_id=rows["Slack · ops"]["message_id"], title="定投放预算", quote="周五前帮忙把投放预算定一下",
                       commitment_phrase="帮忙把投放预算定一下", owner_name="王小明", executor="我", confidence=0.9)
        self.assertEqual(chat_fields(CONFIG, a, rows["Slack · ops"])["来源链接"], rows["Slack · ops"]["message_id"])

    def test_the_next_read_follows_the_interval_set_on_the_settings_page(self):
        from agent_matrix.sources import chats
        from agent_matrix.state import State
        state = State(Path(tempfile.mkdtemp()) / "state.db")
        client = SimpleNamespace(root=None, _exec=lambda argv: {"messages": []})
        now = datetime(2026, 11, 23, 10, 0, tzinfo=TZ)
        chats.poll(CONFIG.model_copy(update={"chat_poll_minutes": 10, "lark_brand": "lark"}), client, state, now)
        self.assertEqual(state.get("chats:next-poll"), "2026-11-23T10:10:00+08:00")

    def test_lark_reads_every_chat_with_one_search_call_and_never_repeats_a_line(self):
        # Lark's API quota is per company (10000 a month): one call per poll, not one per chat.
        from agent_matrix.sources import chats
        from agent_matrix.state import State
        state = State(Path(tempfile.mkdtemp()) / "state.db")
        msg = lambda mid, cid, cname, who, text, **k: {"message_id": mid, "chat_id": cid, "chat_name": cname,
            "chat_type": k.get("mode", "group"), "msg_type": "text", "create_time": k.get("at", "2026-11-23 10:00"),
            "sender": {"id": who, "name": "Amy", "sender_type": "user"}, "content": text, "mentions": k.get("mentions")}
        found = [msg("om_2", "oc_a", "示例客户会员群", "ou_y", "那我先把app装上，你们帮忙升级一下？", at="2026-11-23 10:01"),
                 msg("om_1", "oc_a", "示例客户会员群", "ou_me", "发个店铺来"),
                 msg("om_3", "oc_m", "team-core-dev", "ou_y", "帮忙 review 一下"),            # muted chat, not @你
                 msg("om_4", "oc_m", "team-core-dev", "ou_y", "@王小明 线上报错了", mentions=[{"id": "ou_me"}]),
                 msg("om_5", "oc_b", "审批助手", "ou_y", "请审批")]                          # built-in noise
        calls = []
        client = SimpleNamespace(root=None, _exec=lambda argv: calls.append(argv) or {"messages": found})
        config = CONFIG.model_copy(update={"chat_skip_chats": ["team-core-dev"], "lark_brand": "lark"})   # Feishu reads busy chats in full
        now = datetime(2026, 11, 23, 10, 5, 0, 123456, tzinfo=TZ)
        with unittest.mock.patch("agent_matrix.activity.log"):
            out = chats.poll(config, client, state, now)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][:2], ["im", "+messages-search"])
            self.assertEqual(calls[0][calls[0].index("--end") + 1], "2026-11-23T10:05:00+08:00")  # no fractions
            self.assertEqual(out["candidates"], 3)
            got = {r["message_id"]: r["kind"] for r in chats.pending(state)}
            self.assertEqual(got, {"om_1": "我说的", "om_2": "群聊", "om_4": "@你"})
            self.assertEqual(json.loads(dict((r["message_id"], r["context"]) for r in chats.pending(state))["om_2"]),
                             ["我：发个店铺来"])                                              # read in time order
            # the next poll looks back a little (search lags) and gets the same lines again: nothing is repeated
            self.assertIn("deferred_until", chats.poll(config, client, state, now + timedelta(minutes=10)))   # not before the next round
            out = chats.poll(config, client, state, now + timedelta(minutes=chats.POLL_MINUTES + 1))
            self.assertEqual((out["messages_scanned"], out["candidates"]), (0, 0))

    def test_search_finds_new_lines_in_any_conversation_and_moves_one_cursor(self):
        from agent_matrix.sources import slack
        from agent_matrix.sources.chats import pending
        from agent_matrix.state import State
        root = Path(tempfile.mkdtemp())
        state = State(root / "state.db")
        now = datetime(2026, 11, 17, 10, 0, tzinfo=TZ)
        t = now.timestamp()
        found = [  # newest first, as search returns them
            {"ts": f"{t - 60:.6f}", "user": "UME", "text": "我明天下午3点前把报价单整理好发给 Tom",
             "channel": {"id": "DSELF", "is_im": True, "name": "UME"}},
            {"ts": f"{t - 120:.6f}", "user": "UCHEN", "text": "<@UME> 麻烦周五前确认一下合同", "channel": {"id": "C9", "name": "sales"}},
            {"ts": f"{t - 180:.6f}", "user": "UCHEN", "text": "周五前确认一下合同", "channel": {"id": "C9", "name": "sales"}},
            {"ts": f"{t - 3 * 3600:.6f}", "user": "UCHEN", "text": "<@UME> 老消息，明天记得", "channel": {"id": "C9", "name": "sales"}},
        ]
        calls = []
        def api(tok, method, **p):
            calls.append(method)
            if method == "auth.test":
                return {"user_id": "UME", "url": "https://acme.slack.com/"}
            if method == "users.info":
                return {"user": {"real_name": {"UME": "王小明", "UCHEN": "陈一凡"}[p["user"]]}}
            return {"messages": {"matches": found, "paging": {"pages": 1}}}
        config = CONFIG.model_copy(update={"slack_token": "xoxp-test"})
        out = slack.poll(config, root, state, now, api=api)
        self.assertEqual((out["candidates"], out["via"]), (3, "search"))
        self.assertNotIn("conversations.history", calls)
        kinds = sorted((r["chat_name"], r["kind"]) for r in pending(state))
        self.assertEqual(kinds, [("Slack · sales", "@你"), ("Slack · sales", "群聊"), ("Slack · 王小明", "我说的")])
        self.assertEqual(state.get("slack:search-cursor"), found[0]["ts"])
        self.assertEqual(slack.poll(config, root, state, now, api=api)["candidates"], 0)  # nothing new since the cursor

    def test_followups_after_a_slack_item_are_read_from_slack(self):
        from datetime import datetime
        from types import SimpleNamespace
        from agent_matrix.background import after_context
        from agent_matrix.timeutil import TZ
        def api(tok, method, **p):
            if method == "auth.test":
                return {"user_id": "UME"}
            if method == "users.info":
                return {"user": {"profile": {"display_name": "Richmond"}}}
            self.assertEqual(p["channel"], "D1")
            return {"messages": [{"ts": "1795000500.000200", "user": "UME", "text": "合同确认好了"},
                                 {"ts": "1795000000.000100", "user": "UR", "text": "麻烦你周五前确认一下合同"}]}
        from agent_matrix.sources import slack
        client = SimpleNamespace(config=Config(slack_token="xoxp-1"))
        row = {"来源链接": "https://acme.slack.com/archives/D1/p1795000000000100", "原话引用": "", "创建时间": ""}
        orig = slack.history
        try:
            slack.history = lambda config, ch, oldest, latest=None, thread=None: orig(config, ch, oldest, latest, thread=thread, api=api)
            later = after_context(client, row, datetime.now(TZ))
        finally:
            slack.history = orig
        self.assertEqual(len(later), 1)  # the request itself is not "after"
        self.assertTrue(later[0].endswith("本人] 合同确认好了"))

    def test_followups_include_thread_replies(self):
        from agent_matrix.background import after_context
        from agent_matrix.sources import slack
        from agent_matrix.timeutil import TZ
        asked = "1795000000.000100"
        calls = []
        def api(tok, method, **p):
            calls.append(method)
            if method == "auth.test":
                return {"user_id": "UME"}
            if method == "users.info":
                return {"user": {"profile": {"display_name": "Richmond"}}}
            if method == "conversations.history":
                return {"messages": [{"ts": asked, "user": "UR", "text": "麻烦确认合同", "reply_count": 1, "latest_reply": "1795000900.000000"}]}
            self.assertEqual(p["ts"], asked)
            return {"messages": [{"ts": asked, "user": "UR", "text": "麻烦确认合同", "thread_ts": asked},
                                 {"ts": "1795000900.000000", "user": "UME", "text": "已确认", "thread_ts": asked}]}
        orig = slack.history
        slack.history = lambda config, ch, oldest, latest=None, thread=None: orig(config, ch, oldest, latest, thread=thread, api=api)
        try:
            client = SimpleNamespace(config=Config(slack_token="xoxp-1"))
            # The request is a channel message; the owner answered in its thread.
            later = after_context(client, {"来源链接": f"https://acme.slack.com/archives/C1/p{asked.replace('.', '')}"}, datetime.now(TZ))
            self.assertEqual([x.split("] ")[1] for x in later], ["↳ 已确认"])
            # The request itself sits in a thread: read that thread.
            calls.clear()
            link = slack.permalink("https://acme.slack.com/", "C1", "1794999000.000001", asked)
            self.assertEqual(slack.link_parts(link), ("C1", "1794999000.000001", asked))
            later = after_context(client, {"来源链接": link}, datetime.now(TZ))
            self.assertNotIn("conversations.history", calls)
            self.assertEqual([x.split("] ")[1] for x in later], ["麻烦确认合同", "已确认"])
        finally:
            slack.history = orig

    def test_a_slack_at_mention_in_a_group_is_kept_even_without_ask_words(self):
        from agent_matrix.sources import slack
        from agent_matrix.state import State
        root = Path(tempfile.mkdtemp())
        def api(tok, method, **p):
            if method == "auth.test":
                return {"user_id": "UME", "url": "https://acme.slack.com/"}
            if method == "users.info":
                return {"user": {"profile": {"display_name": "大山"}}}
            return {"messages": {"matches": [
                {"ts": "1795000000.000100", "user": "UX", "text": "<@UME> 业务是否有偏移", "channel": {"id": "C1", "name": "team-dev"}},
                {"ts": "1795000001.000100", "user": "UX", "text": "业务是否有偏移，大家看看", "channel": {"id": "C1", "name": "team-dev"}}],
                "paging": {"pages": 1}}}
        state = State(root / "state.db")
        try:
            out = slack.poll(Config(slack_token="xoxp-1", owner_name="Alex Wang"), root, state,
                             datetime.fromtimestamp(1795000100, ZoneInfo("Asia/Shanghai")), api=api)
            kinds = dict(state.db.execute("SELECT text, kind FROM chat_candidates WHERE status = 'pending'"))
            self.assertEqual(kinds, {"@Alex Wang 业务是否有偏移": "@你", "业务是否有偏移，大家看看": "群聊"})
        finally:
            state.close()

    def test_muted_chats_keep_only_mentions_and_words_are_skipped(self):
        from agent_matrix.sources import slack
        from agent_matrix.state import State
        root = Path(tempfile.mkdtemp())
        def api(tok, method, **p):
            if method == "auth.test":
                return {"user_id": "UME", "url": "https://acme.slack.com/"}
            if method == "users.info":
                return {"user": {"profile": {"display_name": "大山"}}}
            return {"messages": {"matches": [
                {"ts": "1795000000.000100", "user": "UX", "text": "<@UME> 帮忙看下", "channel": {"id": "C1", "name": "bot-shop-alerts"}},
                {"ts": "1795000000.000200", "user": "UX", "text": "<@UME> 优化一下这个", "channel": {"id": "C3", "name": "team-core-dev"}},
                {"ts": "1795000000.000300", "user": "UX", "text": "Alex Wang 今天能看下这个吗", "channel": {"id": "C3", "name": "team-core-dev"}},
                {"ts": "1795000001.000100", "user": "UX", "text": "<@UME> Issue created 帮忙看下", "channel": {"id": "C2", "name": "team-dev"}},
                {"ts": "1795000002.000100", "user": "UX", "text": "<@UME> 帮忙看下这个需求", "channel": {"id": "C2", "name": "team-dev"}}],
                "paging": {"pages": 1}}}
        state = State(root / "state.db")
        try:
            config = Config(slack_token="xoxp-1", owner_name="Alex Wang", chat_skip_chats=["CORE"], chat_skip_words=["issue created"])
            out = slack.poll(config, root, state, datetime.fromtimestamp(1795000100, ZoneInfo("Asia/Shanghai")), api=api)
            # bot channels are never read; a muted channel keeps only real @-mentions; muted words skip, case-insensitive
            texts = sorted(t for t, in state.db.execute("SELECT text FROM chat_candidates WHERE status = 'pending'"))
            self.assertEqual(out["candidates"], 2)
            self.assertEqual(texts, ["@Alex Wang 优化一下这个", "@Alex Wang 帮忙看下这个需求"])
        finally:
            state.close()

    def test_private_report_with_screenshots_is_kept_and_context_survives_polls(self):
        from agent_matrix.sources import slack
        from agent_matrix.state import State
        root = Path(tempfile.mkdtemp())
        dm = {"id": "D1", "name": "cara", "is_im": True}
        rounds = [[{"ts": "1795000000.000100", "user": "UX", "subtype": "file_share", "text": "截图在这", "channel": dm},
                   {"ts": "1795000000.000200", "user": "UX", "text": "网页比例100%时，这个会员弹窗只能展示一点出来", "channel": dm}],
                  [{"ts": "1795000060.000100", "user": "UX", "text": "<http://dreame-us.myshopify.com|dreame-us.myshopify.com>", "channel": dm}]]
        def api(tok, method, **p):
            if method == "auth.test":
                return {"user_id": "UME", "url": "https://acme.slack.com/"}
            if method == "users.info":
                return {"user": {"profile": {"display_name": "cara"}}}
            return {"messages": {"matches": rounds.pop(0) if rounds else [], "paging": {"pages": 1}}}
        state = State(root / "state.db")
        try:
            config = Config(slack_token="xoxp-1", owner_name="Alex Wang")
            slack.poll(config, root, state, datetime.fromtimestamp(1795000030, TZ), api=api)
            slack.poll(config, root, state, datetime.fromtimestamp(1795000090, TZ), api=api)
            rows = {t: json.loads(c) for t, c in state.db.execute("SELECT text, context FROM chat_candidates WHERE status = 'pending'")}
            # a bug report in a DM has no ask words but is kept; the picture's caption is read
            self.assertIn("网页比例100%时，这个会员弹窗只能展示一点出来", rows)
            self.assertEqual(rows["网页比例100%时，这个会员弹窗只能展示一点出来"], ["cara：截图在这"])
            # the next poll still sees what came just before
            self.assertEqual(rows["<http://dreame-us.myshopify.com|dreame-us.myshopify.com>"],
                             ["cara：截图在这", "cara：网页比例100%时，这个会员弹窗只能展示一点出来"])
        finally:
            state.close()

    def test_replies_to_the_owner_and_channel_notices_are_kept_unless_muted(self):
        from agent_matrix.sources import slack
        from agent_matrix.state import State
        root = Path(tempfile.mkdtemp())
        dev, noisy = {"id": "C1", "name": "dev"}, {"id": "C2", "name": "bot-free-noise"}
        def thread_link(c, ts, root_ts):
            return f"https://acme.slack.com/archives/{c}/p{ts.replace('.', '')}?thread_ts={root_ts}&cid={c}"
        rounds = [[{"ts": "1795000000.000100", "user": "UME", "text": "导出功能今天上 dev 了", "channel": dev},
                   {"ts": "1795000000.000200", "user": "UME", "text": "噪音群里我也发一句", "channel": noisy}],
                  [{"ts": "1795000060.000100", "user": "UX", "text": "试了一下，还是超时",
                    "permalink": thread_link("C1", "1795000060.000100", "1795000000.000100"), "channel": dev},
                   {"ts": "1795000060.000200", "user": "UX", "text": "<!here> 今晚 10 点封版", "channel": dev},
                   {"ts": "1795000060.000300", "user": "UX", "text": "午饭吃啥", "channel": dev},
                   {"ts": "1795000060.000400", "user": "UX", "text": "收到，我也看看",
                    "permalink": thread_link("C2", "1795000060.000400", "1795000000.000200"), "channel": noisy},
                   {"ts": "1795000060.000500", "user": "UX", "text": "<!channel> 例行通知", "channel": noisy}]]
        def api(tok, method, **p):
            if method == "auth.test":
                return {"user_id": "UME", "url": "https://acme.slack.com/"}
            if method == "users.info":
                return {"user": {"profile": {"display_name": "大山"}}}
            return {"messages": {"matches": rounds.pop(0) if rounds else [], "paging": {"pages": 1}}}
        state = State(root / "state.db")
        try:
            config = Config(slack_token="xoxp-1", owner_name="Alex Wang", chat_skip_chats=["noise"])
            slack.poll(config, root, state, datetime.fromtimestamp(1795000030, TZ), api=api)
            slack.poll(config, root, state, datetime.fromtimestamp(1795000090, TZ), api=api)
            kinds = dict(state.db.execute("SELECT text, kind FROM chat_candidates WHERE status = 'pending'"))
            # dev: everything goes to the model, labelled; the muted channel keeps nothing (no one @-ed the owner there)
            self.assertEqual(kinds, {"导出功能今天上 dev 了": "我说的", "试了一下，还是超时": "回你的",
                                     "@here 今晚 10 点封版": "全员", "午饭吃啥": "群聊"})
        finally:
            state.close()

    def test_connect_saves_token_and_shows_identity_without_the_token(self):
        from agent_matrix import app
        from agent_matrix.config import load
        from agent_matrix.sources import slack
        root = Path(tempfile.mkdtemp())
        api = lambda tok, method, **p: ({"user": {"real_name": "王小明", "profile": {"real_name": "王小明", "display_name": "小明"}}}
                                        if method == "users.info" else
                                        {"user_id": "UME", "user": "wang", "team": "Acme", "url": "https://acme.slack.com/"})
        with self.assertRaises(MatrixError):
            slack.connect(root, "xoxb-bot-token", api=api)            # bot tokens can't read as the owner
        who = slack.connect(root, " xoxp-1234-abcd ", api=api)
        saved = load(root / "config.yaml")
        self.assertEqual(saved.slack_token, "xoxp-1234-abcd")
        self.assertEqual((saved.owner_name, saved.owner_aliases), ("王小明", ["小明"]))  # "you" in Slack is recognised
        shown = app.system_info(root)["slack"]
        self.assertEqual((shown["team"], shown["tail"]), ("Acme", "abcd"))
        self.assertNotIn("xoxp", json.dumps(shown) + json.dumps(who))
        slack.disconnect(root)
        self.assertEqual((load(root / "config.yaml").slack_token, app.system_info(root)["slack"]), ("", None))
