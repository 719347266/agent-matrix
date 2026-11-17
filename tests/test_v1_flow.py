"""v1 flow: owner reply commands, strict meeting tasks, complete deliverables, one-screen brief."""
from datetime import date, datetime
import json
from hashlib import sha256
from types import SimpleNamespace
import unittest
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from agent_matrix.brief_v1 import build, visible_text
from agent_matrix.meeting_review import Action, Review, kept_actions, summary_line, validate_review
from agent_matrix.models import Config, MatrixError
from agent_matrix.prework import Result
from agent_matrix.reply import Applier, parse

TZ = ZoneInfo("Asia/Shanghai")
TODAY = date(2026, 11, 16)  # Monday


class ParseTests(unittest.TestCase):
    def ops(self, text):
        return [(o.numbers, o.verb, o.arg) for o in parse(text, TODAY)[0]]

    def test_commands(self):
        self.assertEqual(self.ops("#15 过"), [([15], "approve", "")])
        self.assertEqual(self.ops("15 19 过"), [([15, 19], "approve", "")])
        self.assertEqual(self.ops("19 退 少了定价"), [([19], "reject", "少了定价")])
        self.assertEqual(self.ops("8 周五"), [([8], "reschedule", "2026-11-20")])
        self.assertEqual(self.ops("8 11-20"), [([8], "reschedule", "2026-11-20")])
        self.assertEqual(self.ops("8 改到下周三"), [([8], "reschedule", "2026-11-25")])
        self.assertEqual(self.ops("18 AI备"), [([18], "prep", "")])
        self.assertEqual(self.ops("12 给张三"), [([12], "delegate", "张三")])
        self.assertEqual(self.ops("20 + 数据在共享盘"), [([20], "material", "数据在共享盘")])
        self.assertEqual(self.ops("展开 12"), [([12], "expand", "")])

    def test_chat_is_not_a_command_and_unclear_asks_once(self):
        self.assertEqual(parse("今天开了三个会", TODAY), ([], []))
        self.assertEqual(parse("3月份的数据挺好的，我们下周再看看吧", TODAY), ([], []))
        ops, problems = parse("19 退", TODAY)
        self.assertEqual(ops, []); self.assertEqual(len(problems), 1)


class FakeGateway:
    def __init__(self, rows):
        self.data = {r["record_id"]: r for r in rows}
        self.writes = []

    def rows(self):
        return list(self.data.values())

    def update(self, before, patch, key, source):
        self.writes.append((before["record_id"], patch))
        self.data[before["record_id"]] = {**self.data[before["record_id"]], **patch}
        return {"verified_by_readback": True}

    def create(self, fields, key, source):
        rid = "rec_new"
        self.data[rid] = {"record_id": rid, "编号": 99, **fields}
        return {"record_id_list": [rid]}


def applier(rows):
    a = Applier.__new__(Applier)
    a.config = Config(owner_open_id="ou_me", owner_name="我")
    a.gateway, a.state, a.client = FakeGateway(rows), None, None
    a.source, a.ref, a.now = "test", "m1", datetime(2026, 11, 16, 16, 0, tzinfo=TZ)
    return a


def row(n, status, executor="AI", **extra):
    return {"record_id": f"rec{n}", "编号": n, "标题": f"事项{n}", "状态": [status], "执行方": [executor], **extra}


class ApplyTests(unittest.TestCase):
    def run_text(self, a, text):
        out = []
        for op in parse(text, TODAY)[0]:
            out += a.apply(op)
        return out

    def test_only_delivered_ai_work_can_pass_and_pass_unlocks_parent(self):
        parent = row(3, "卡住", "我", 卡点="等#24验收备稿")
        child = row(24, "待验收", 父事项=[{"id": "rec3"}], 交付物链接="https://x.feishu.cn/docx/abc")
        a = applier([parent, child, row(5, "未开始")])
        self.assertIn("还没交付", self.run_text(a, "5 过")[0])
        reply = self.run_text(a, "24 过")[0]
        self.assertIn("完成 ✓", reply); self.assertIn("#3 可以开始了", reply)
        self.assertEqual(a.gateway.data["rec3"]["状态"], ["未开始"])
        self.assertIsNone(a.gateway.data["rec3"]["卡点"])

    def test_reject_keeps_comment_and_reopens(self):
        a = applier([row(19, "待验收")])
        self.run_text(a, "19 退 少了定价")
        r = a.gateway.data["rec19"]
        self.assertEqual((r["状态"], r["验收结果"]), (["进行中"], ["退回"]))
        self.assertIn("少了定价", r["退回意见"])

    def test_later_date_counts_a_deferral_with_history(self):
        a = applier([row(8, "未开始", "我", 截止日="2026-11-16T00:00:00.000+08:00")])
        reply = self.run_text(a, "8 周五")[0]
        r = a.gateway.data["rec8"]
        self.assertEqual(r["顺延次数"], 1); self.assertIn("11-20", r["顺延历史"]); self.assertIn("顺延1次", reply)

    def test_unknown_number_is_reported_not_guessed(self):
        a = applier([row(1, "未开始")])
        self.assertIn("找不到", self.run_text(a, "77 过")[0])
        self.assertEqual(a.gateway.writes, [])

    def test_prep_creates_ai_child_and_blocks_owner_step(self):
        a = applier([row(18, "未开始", "我")])
        reply = self.run_text(a, "18 AI备")[0]
        self.assertIn("#99", reply)
        self.assertEqual(a.gateway.data["rec_new"]["执行方"], ["AI"])
        self.assertEqual(a.gateway.data["rec18"]["状态"], ["卡住"])


class MeetingTaskTests(unittest.TestCase):
    transcript = "00:10 我：如果你看了没问题，我这边就让大伟去做进去了。\n00:20 杨：我再看看吧。"

    def review(self, **changes):
        action = dict(title="安排大伟做标签体系", text="安排", quote="我这边就让大伟去做进去了", timestamp="00:10",
                      reference=self.transcript.splitlines()[0], owner_name="我", direction="mine",
                      commitment_phrase="我这边就让大伟去做", confidence=0.6)
        action.update(changes)
        return Review(meeting_id="1" * 19, transcript_sha256=sha256(self.transcript.encode()).hexdigest(),
                      actions=[Action(**action)])

    def test_low_confidence_or_missing_commitment_is_not_a_task(self):
        meta = {"meeting_id": "1" * 19, "start_time": "2026-11-16 14:32", "topic": "标签体系沟通"}
        review = self.review()
        validate_review(review, self.transcript, meta)
        self.assertEqual(kept_actions(review), [])
        self.assertIsNone(summary_line(review, Config(owner_name="我", owner_open_id="ou_me"), meta))
        with self.assertRaises(MatrixError):
            validate_review(self.review(commitment_phrase="我来负责"), self.transcript, meta)


class DeliverableTests(unittest.TestCase):
    def test_half_finished_result_is_rejected(self):
        base = {"record_id": "rec1", "input_version": "a" * 64, "kind": "internal_result", "template": "周报",
                "summary": "汇总本周 12 件", "artifact_path": "x.xml", "artifact_sha256": "b" * 64,
                "checks": ["本周完成：表格"], "source_refs": ["事项表"]}
        Result(**base)
        with self.assertRaises(ValidationError):
            Result(**base, limitations=["缺下周计划"])
        with self.assertRaises(ValidationError):
            Result(**{**base, "summary": "长" * 41})


class BriefTests(unittest.TestCase):
    def test_one_screen_even_with_many_items(self):
        rows = []
        for n in range(1, 41):
            status, executor = [("未开始", "我"), ("待验收", "AI"), ("待确认", "我"), ("进行中", "AI")][n % 4]
            rows.append(row(n, status, executor, 进度="1/2 在做", 截止日="2026-11-17T00:00:00.000+08:00",
                            交付物链接="https://x.feishu.cn/docx/d%d" % n))
        config = Config(owner_open_id="ou_me", base_url="https://x.feishu.cn/base/b")
        out = build(config, rows, [], date(2026, 11, 17), "morning")
        self.assertLessEqual(out["line_count"], 15)
        self.assertLessEqual(len(visible_text(out["text"])), 400)
        self.assertNotIn("https://", visible_text(out["text"]))
        self.assertIn("其余", out["text"])

    def test_card_groups_items_and_explains_next_step(self):
        from agent_matrix.brief_v1 import build_card
        rows = [row(3, "卡住", "我", 卡点="等#26验收备稿", 截止日="2026-11-16T00:00:00.000+08:00"),
                row(26, "待验收", 交付物链接="https://x.feishu.cn/docx/a", 预做结果摘要="核对清单已整理。另有说明"),
                row(24, "待确认", "我")]
        c = build_card(Config(base_url="https://x.feishu.cn/base/b"), rows, [], date(2026, 11, 17), "morning")
        self.assertEqual(c["schema"], "2.0")
        flat = json.dumps(c, ensure_ascii=False)
        self.assertIn("先看一眼，没问题回「26 过」就能开始", flat)
        self.assertIn("什么时候做完？回「24 周五」", flat)
        self.assertIn("核对清单已整理", flat); self.assertNotIn("另有说明", flat)
        self.assertLessEqual(len([b for b in c["body"]["elements"] if b["tag"] == "interactive_container"]), 5)

    def test_empty_day_is_one_line(self):
        out = build(Config(), [], [], date(2026, 11, 17), "morning")
        self.assertEqual(out["line_count"], 1)


if __name__ == "__main__":
    unittest.main()


class PrepAndWeeklyTests(unittest.TestCase):
    def test_prep_links_items_by_topic_and_skips_empty_meetings(self):
        from agent_matrix.agents.meeting_prep import _keywords, _related, build
        keys = _keywords("2.2需求确认")
        self.assertTrue(_related("整理2.2多自定义角色与头像需求稿", keys))
        self.assertFalse(_related("整理投放测试策略", keys))
        self.assertTrue(_related("准备实习生例会复盘材料", _keywords("XX团队-实习生运营复盘会")))
        queue = SimpleNamespace(list=lambda: [])
        event = {"event_id": "e1", "summary": "2.2需求确认", "start_time": {"datetime": "2026-11-18T09:45:00+08:00"}}
        now = datetime(2026, 11, 18, 9, 15, tzinfo=TZ)
        rows = [row(13, "待验收", 标题="整理2.2多自定义角色与头像需求稿", 交付物链接="https://x.feishu.cn/docx/a")]
        card = build(Config(owner_name="我"), rows, event, queue, "/tmp", now)
        self.assertIn("30 分钟后开始", json.dumps(card, ensure_ascii=False))
        self.assertIsNone(build(Config(), [row(6, "未开始", 标题="整理投放测试策略")], event, queue, "/tmp", now))

    def test_weekly_reasons_are_recorded_facts_and_send_once_on_friday(self):
        from agent_matrix.agents.weekly_review import why, due
        self.assertEqual(why(row(1, "未开始", "我", 顺延历史='{"source":"evening"}')), "到期没做完，自动顺延")
        self.assertEqual(why(row(2, "未开始", "我", 顺延历史='{"source":"owner"}')), "你主动改了日期")
        self.assertEqual(why(row(3, "卡住", "我", 卡点="等#26验收备稿")), "等你先看 AI 的 #26")
        self.assertEqual(why(row(4, "未开始", "别人")), "等对方交付")
        state = SimpleNamespace(operation=lambda key: None)
        self.assertIsNone(due(datetime(2026, 11, 20, 16, 59, tzinfo=TZ), state))
        self.assertEqual(due(datetime(2026, 11, 20, 17, 1, tzinfo=TZ), state), "weekly:2026-W47")
        self.assertIsNone(due(datetime(2026, 11, 19, 17, 1, tzinfo=TZ), state))
