import copy
from datetime import date
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent_matrix.agents.daily_brief import FIELDS, SUMMARY_FIELD, build_brief, run
from agent_matrix.models import Config, MatrixError
from agent_matrix.state import State


DAY = date(2026, 11, 16)


def task(record_id, due="2026-11-16", priority="P1", minutes=60, status="待办", **extra):
    return {"record_id": record_id, "标题": "事项 " + record_id, "状态": [status],
            "截止日": due, "优先级": [priority], "预计耗时(分钟)": minutes,
            "方向": ["我的"], "负责人": [{"id": "ou_owner", "name": "本人"}], **extra}


def meeting(event_id, start, end, rsvp="accept", **extra):
    return {"event_id": event_id, "summary": "会议 " + event_id,
            "start_time": {"datetime": start}, "end_time": {"datetime": end},
            "self_rsvp_status": rsvp, **extra}


class DailyBriefTests(unittest.TestCase):
    def config(self, **kwargs):
        return Config(owner_open_id="ou_owner", **kwargs)

    def build(self, items=None, events=None, **kwargs):
        return build_brief(self.config(**kwargs), items or [], events or [], DAY, "morning")

    def test_overlap_breaks_and_cross_midnight_are_unions(self):
        events = [
            meeting("a", "2026-11-16T09:00+08:00", "2026-11-16T11:00+08:00"),
            meeting("b", "2026-11-16T10:00+08:00", "2026-11-16T12:30+08:00"),
            meeting("c", "2026-11-16T12:15+08:00", "2026-11-16T13:30+08:00"),
            meeting("d", "2026-11-15T23:00+08:00", "2026-11-16T09:30+08:00"),
            meeting("e", "2026-11-16T17:30+08:00", "2026-11-17T01:00+08:00"),
        ]
        result = self.build([task("r", minutes=150)], events, work_start="09:00", work_end="18:00",
                            work_breaks=[["12:00", "13:00"], ["12:30", "13:00"]])
        capacity = result["capacity"]
        self.assertEqual(capacity["work_minutes"], 540)
        self.assertEqual(capacity["break_minutes"], 60)
        self.assertEqual(capacity["meeting_minutes_excluding_breaks"], 240)
        self.assertEqual(capacity["available_minutes"], 240)
        self.assertEqual(capacity["planning_limit_minutes"], 168)
        self.assertFalse(capacity["exceeds_planning_limit"])
        self.assertEqual(result["calendar"]["confirmed_meeting_minutes"], 1200)
        self.assertEqual(result["calendar"]["accepted"][0]["time"], "00:00–09:30")
        self.assertEqual(result["calendar"]["accepted"][-1]["time"], "17:30–24:00")

    def test_unconfigured_hours_do_not_invent_capacity(self):
        result = self.build([task("r", minutes=600)])
        self.assertIsNone(result["capacity"]["available_minutes"])
        self.assertIsNone(result["capacity"]["exceeds_planning_limit"])
        self.assertIn("工作时段未设置，暂不判断是否排得下", result["text"])
        self.assertNotIn("10:00", result["text"])
        self.assertFalse(self.build(work_start="09:00")["capacity"]["configured"])

    def test_unknown_estimates_do_not_count_as_zero_or_claim_fit(self):
        rows = [task("known", minutes=30), task("unknown", minutes=None),
                task("invalid", minutes=-10), task("nan", minutes="nan"), task("zero", minutes=0)]
        result = self.build(rows, work_start="09:00", work_end="18:00")
        self.assertEqual(result["capacity"]["known_task_minutes"], 30)
        self.assertEqual(result["capacity"]["unknown_task_count"], 3)
        self.assertIn("暂不能确认是否排得下", result["text"])

    def test_foreign_blocked_future_and_unconfirmed_work_excluded_from_day_capacity(self):
        rows = [task("mine", minutes=20), task("waiting", minutes=100, 方向=["别人答应我的"]),
                task("other", minutes=100, 方向=["别人的"]), task("blocked", minutes=100, status="阻塞"),
                task("future", due="2026-12-01", minutes=100), task("pending", minutes=100, status="待确认"),
                task("urgent", due=None, priority="P0", minutes=10)]
        result = self.build(rows)
        self.assertEqual(result["capacity"]["known_task_minutes"], 30)
        self.assertEqual(set(result["capacity"]["planned_record_ids"]), {"mine", "urgent"})
        self.assertEqual(len(result["pending"]), 6)

    def test_explicit_others_do_not_affect_any_brief_section_or_count(self):
        unrelated = [
            task("foreign_draft_a", status="草稿已备", due=None, minutes=None,
                 方向=["别人的"], 负责人=[], AI草稿链接="https://example.invalid/draft-a",
                 预做结果摘要="他人已准备的材料"),
            {"record_id": "foreign_draft_b", "fields": {
                "标题": "他人的待核验草稿", "状态": "草稿已备", "方向": "别人的", "负责人": []}},
            # An explicit direction overrides even an inconsistent owner field.
            task("foreign_todo", minutes=900, 方向=["别人的"]),
            task("foreign_done", status="完成", 方向=["别人的"],
                 更新时间="2026-11-15T16:00+08:00"),
        ]
        normal = [task("mine", minutes=30),
                  task("my_draft", status="草稿已备", AI草稿链接="https://example.invalid/my-draft"),
                  task("my_done", status="完成", 更新时间="2026-11-15T16:00+08:00")]
        long_rows = [task(str(i), 标题="长事项" * 200) for i in range(100)]
        config = self.config(work_start="09:00", work_end="18:00")
        for edition in ("morning", "evening"):
            for label, kept in (("empty", []), ("normal", normal), ("long", long_rows)):
                with self.subTest(edition=edition, case=label):
                    expected = build_brief(config, kept, [], DAY, edition)
                    result = build_brief(config, [*kept, *unrelated], [], DAY, edition)
                    # Includes pending/focus/review/confirmations/completed, capacity,
                    # rendered text and condensed-message totals, not just title hiding.
                    self.assertEqual(result, expected)

    def test_promises_to_me_and_unknown_ownership_still_get_followed_up(self):
        rows = [task("promised", minutes=900, 方向=["别人答应我的"], 负责人=[]),
                task("promised_draft", status="草稿已备", 方向=["别人答应我的"], 负责人=[],
                     AI草稿链接="https://example.invalid/promised-draft"),
                task("promised_done", status="完成", 方向=["别人答应我的"], 负责人=[],
                     更新时间="2026-11-15T16:00+08:00"),
                task("unknown", due=None, 方向=[], 负责人=[])]
        for edition in ("morning", "evening"):
            with self.subTest(edition=edition):
                result = build_brief(self.config(), rows, [], DAY, edition)
                self.assertEqual({row["record_id"] for row in result["pending"]},
                                 {"promised", "promised_draft", "unknown"})
                self.assertEqual([row["record_id"] for row in result["awaiting_review"]], ["promised_draft"])
                self.assertEqual([row["record_id"] for row in result["recent_completed"]], ["promised_done"])
                confirmations = {row["record_id"]: row["fields"] for row in result["confirmations"]}
                self.assertEqual(set(confirmations), {"promised", "promised_draft", "unknown"})
                self.assertIn("归属", confirmations["unknown"])
                self.assertEqual(result["focus"], [])
                self.assertEqual(result["capacity"]["planned_record_ids"], [])
                self.assertEqual(result["capacity"]["known_task_minutes"], 0)
                self.assertIn("查看草稿：https://example.invalid/promised-draft", result["text"])
                self.assertIn("✓ 事项 promised_done", result["text"])
                if edition == "evening":
                    self.assertIn("别人答应我的", result["text"])

    def test_completed_and_cancelled_are_never_pending(self):
        rows = [task("done", status="完成", 更新时间="2026-11-15T16:00+08:00"),
                task("cancel", status="取消"), task("old", status="完成", 更新时间="2026-10-01T09:00+08:00")]
        result = self.build(rows)
        self.assertEqual(result["pending"], [])
        self.assertEqual([x["record_id"] for x in result["recent_completed"]], ["done"])
        self.assertIn("✓ 事项 done", result["text"])
        self.assertNotIn("□ 事项 done", result["text"])
        self.assertNotIn("事项 cancel", result["text"])
        self.assertIn("不等同于实际完成时刻", result["text"])
        json.dumps(result)

    def test_empty_tasks_does_not_mean_no_work(self):
        result = self.build()
        self.assertIn("当前事项表无待办", result["text"])
        self.assertIn("不代表没有其他工作", result["text"])
        self.assertNotIn("最重要", result["text"])
        self.assertNotIn("第 2 项", result["text"])

    def test_priority_is_evidenced_and_stable(self):
        rows = [task("future", "2026-11-25"), task("soon", "2026-11-18"),
                task("urgent", "2026-12-01", "P0"), task("today", "2026-11-16"),
                task("late_b", "2026-11-15"), task("late_a", "2026-11-14"), task("unknown", None)]
        result = self.build(rows)
        ids = [x["record_id"] for x in result["pending"]]
        self.assertEqual(ids, ["late_a", "late_b", "today", "urgent", "soon", "future", "unknown"])
        self.assertEqual(result, self.build(list(reversed(rows))))

    def test_unanswered_declined_cancelled_and_all_day(self):
        events = [meeting("accepted", "2026-11-16T09:00+08:00", "2026-11-16T10:00+08:00"),
                  meeting("unanswered", "2026-11-16T10:00+08:00", "2026-11-16T11:00+08:00", "needs_action"),
                  meeting("refused", "2026-11-16T11:00+08:00", "2026-11-16T12:00+08:00", "decline"),
                  meeting("cancelled", "2026-11-16T12:00+08:00", "2026-11-16T13:00+08:00", status="cancelled"),
                  {"event_id": "birthday", "summary": "生日", "self_rsvp_status": "accept",
                   "start_time": {"date": "2026-11-16"}, "end_time": {"date": "2026-11-16"}}]
        result = self.build(events=events, work_start="09:00", work_end="18:00")
        self.assertEqual(result["calendar"]["confirmed_meeting_minutes"], 60)
        self.assertEqual(result["capacity"]["available_minutes"], 480)
        self.assertEqual(len(result["calendar"]["tentative"]), 1)
        self.assertEqual(len(result["calendar"]["all_day"]), 1)
        self.assertIn("参会待确认", result["text"])
        self.assertIn("全天提醒：生日", result["text"])
        self.assertNotIn("refused", result["text"])
        self.assertNotIn("cancelled", result["text"])

    def test_missing_fields_links_invalid_times_and_no_mutation(self):
        rows = [task("r", due=None, 负责人=[], 来源链接="https://example.invalid/record", 下一步动作="核对原稿")]
        events = [{"event_id": "bad", "summary": "待定", "start_time": {}, "end_time": {}},
                  meeting("valid", "2026-11-16T01:00Z", "2026-11-16T02:00Z", app_link="https://example.invalid/event")]
        before = copy.deepcopy((rows, events))
        result = self.build(rows, events)
        self.assertEqual((rows, events), before)
        self.assertEqual(result["confirmations"][0]["fields"], ["负责人", "截止日"])
        self.assertIn("https://example.invalid/record", result["text"])
        self.assertIn("https://example.invalid/event", result["text"])
        self.assertIn("09:00–10:00", result["text"])
        self.assertIn("开始或结束时间待确认", result["text"])

    def test_70_percent_warning_does_not_change_deadlines(self):
        rows = [task("r", minutes=400)]
        result = self.build(rows, work_start="09:00", work_end="17:00")
        self.assertTrue(result["capacity"]["exceeds_planning_limit"])
        self.assertIn("超过建议上限", result["text"])
        self.assertEqual(rows[0]["截止日"], "2026-11-16")

    def test_long_list_is_one_explicit_summary_not_false_complete_list(self):
        rows = [task(str(i), 标题="长事项" * 200) for i in range(100)]
        config = self.config(base_url="https://example.invalid/base")
        morning = build_brief(config, rows, [], DAY, "morning")
        self.assertEqual(len(morning["focus"]), 3)
        self.assertIn("其余未完成 97 项未展开", morning["text"])
        result = build_brief(config, rows, [], DAY, "evening")
        self.assertTrue(result["condensed"])
        self.assertEqual(len(result["pending"]), 100)
        self.assertLessEqual(len(result["text"].encode()), 18000)
        self.assertIn("未完成 100 项", result["text"])
        self.assertIn("另有 97 项未展开", result["text"])
        self.assertIn("https://example.invalid/base", result["text"])

    def test_focus_only_clear_actionable_owner_tasks_and_at_most_three(self):
        rows = [task("late_blocked", "2026-11-12", status="阻塞"),
                task("late_pending", "2026-11-13", status="待确认"),
                task("other", "2026-11-14", 方向=["别人的"]),
                task("late_mine", "2026-11-15"), task("today"),
                task("p0", "2026-12-01", "P0"), task("later", "2026-11-18")]
        result = self.build(rows)
        self.assertEqual([r["record_id"] for r in result["focus"]], ["late_mine", "today", "p0"])
        self.assertEqual(result["text"].count("□ "), 3)
        self.assertIn("其余未完成 3 项未展开", result["text"])
        self.assertIn("Cindy 对话里回复事项名称和变化", result["text"])
        only_pending = self.build(rows[:3])
        self.assertEqual(only_pending["focus"], [])
        self.assertIn("没有已明确归属、可直接安排的本人事项", only_pending["text"])

    def test_long_unicode_sources_still_fit_one_message(self):
        url = "https://example.invalid/" + "字" * 300
        rows = [task(str(i), 标题="😃" * 1000, 来源链接=url) for i in range(100)]
        events = [meeting(str(i), "2026-11-16T09:00+08:00", "2026-11-16T10:00+08:00",
                          summary="😃" * 1000, app_link=url) for i in range(100)]
        result = self.build(rows, events)
        self.assertTrue(result["condensed"])
        self.assertLessEqual(len(result["text"].encode()), 18000)
        self.assertEqual(result["text"].count("□ "), 3)

    def test_bad_settings_and_partial_calendar_fail_closed(self):
        with self.assertRaises(MatrixError):
            self.build(work_start="18:00", work_end="09:00")
        with self.assertRaises(MatrixError):
            self.build(work_start="09:00", work_end="18:00", work_breaks=[["12:00"]])
        with self.assertRaises(MatrixError):
            build_brief(self.config(), [], {"has_more": True}, DAY, "morning")

    def test_prepared_results_are_reviewable_not_complete_or_repeated_work(self):
        doc = "https://example.feishu.cn/docx/ActualPreparedDocument"
        rows = [task("draft", minutes=300, status="草稿已备", AI草稿链接=doc,
                     预做结果摘要="已整理三项进展、两项风险，缺少的销售数字已标待补充。",
                     更新时间="2026-11-16T08:00+08:00"),
                task("unestimated_draft", minutes=None, status="草稿已备", AI草稿链接=doc),
                task("actual_work", minutes=30)]
        for edition in ("morning", "evening"):
            result = build_brief(self.config(work_start="09:00", work_end="18:00"), rows, [], DAY, edition)
            self.assertEqual([row["record_id"] for row in result["focus"]], ["actual_work"])
            self.assertEqual(len(result["awaiting_review"]), 2)
            self.assertEqual(len(result["pending"]), 3)
            self.assertEqual(result["recent_completed"], [])
            self.assertEqual(result["capacity"]["known_task_minutes"], 30)
            self.assertEqual(result["capacity"]["unknown_task_count"], 0)
            self.assertEqual(result["capacity"]["planned_record_ids"], ["actual_work"])
            self.assertIn("结果待确认 2 项（草稿已备，不代表业务完成）", result["text"])
            self.assertIn("查看草稿：" + doc, result["text"])
            self.assertIn("已整理三项进展、两项风险", result["text"])
            self.assertNotIn("✓ 事项 draft", result["text"])
            self.assertNotIn("预计 300 分钟", result["text"])
            self.assertFalse(any("有效状态" in entry["fields"] for entry in result["confirmations"]))

    def test_missing_draft_link_or_summary_is_visible_not_invented(self):
        result = self.build([task("draft", status="草稿已备", AI草稿链接="javascript:invalid")])
        self.assertEqual(result["focus"], [])
        self.assertEqual(result["capacity"]["known_task_minutes"], 0)
        self.assertIn("结果摘要尚未提供", result["text"])
        self.assertIn("草稿链接尚未提供", result["text"])
        self.assertNotIn("javascript:", result["text"])
        self.assertIn("草稿链接", result["confirmations"][0]["fields"])
        self.assertNotIn("当前事项表无待办", result["text"])

    def test_condensed_brief_retains_real_draft_links_summaries_and_counts(self):
        doc = "https://example.feishu.cn/docx/PreparedReview"
        rows = [task(str(i), 标题="待确认长标题" * 100, status="草稿已备", AI草稿链接=doc,
                     预做结果摘要="已制作初稿，等待本人确认。" * 300) for i in range(20)]
        result = build_brief(self.config(), rows, [], DAY, "evening")
        self.assertTrue(result["condensed"])
        self.assertIn("结果待确认 20 项", result["text"])
        self.assertIn("另有 17 项结果待确认未展开", result["text"])
        self.assertIn(doc, result["text"])
        self.assertIn("已制作初稿", result["text"])
        self.assertEqual(result["capacity"]["known_task_minutes"], 0)
        self.assertEqual(result["focus"], [])
        self.assertLessEqual(len(result["text"].encode()), 18000)

    def test_busy_condensed_brief_prioritizes_direct_review_links_within_size_limit(self):
        url = "https://example.feishu.cn/docx/" + "a" * 310
        title = "😃" * 1000
        rows = [task("work" + str(i), 标题=title, 来源链接=url) for i in range(3)]
        rows += [task("draft" + str(i), 标题=title, status="草稿已备", AI草稿链接=url,
                      预做结果摘要=title) for i in range(3)]
        rows += [task("done" + str(i), 标题=title, status="完成", 更新时间="2026-11-15T10:00+08:00")
                 for i in range(3)]
        rows += [task("missing" + str(i), 标题=title, status="待确认", due=None, 负责人=[]) for i in range(3)]
        events = []
        for i in range(3):
            events.extend([meeting("yes" + str(i), "2026-11-16T09:00+08:00", "2026-11-16T10:00+08:00",
                                   summary=title, app_link=url),
                           meeting("maybe" + str(i), "2026-11-16T11:00+08:00", "2026-11-16T12:00+08:00",
                                   "needs_action", summary=title, app_link=url),
                           {"summary": title, "app_link": url, "start_time": {"date": "2026-11-16"}},
                           {"summary": title, "app_link": url, "start_time": {}, "end_time": {}}])
        result = self.build(rows, events, base_url="https://example.feishu.cn/base/" + "b" * 900)
        self.assertTrue(result["condensed"])
        self.assertIn("查看草稿：" + url, result["text"])
        self.assertLessEqual(len(result["text"].encode()), 18000)


class DailyBriefRunTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.state = State(Path(self.directory.name)/"state.db")
        self.config = Config(owner_open_id="ou_owner")
        self.client = SimpleNamespace(_exec=Mock(return_value=[]),
                                      send_self=Mock(return_value={"data": {"message_id": "om_1"}}))
        self.reader = patch("agent_matrix.records.read_records", return_value=[])
        self.read = self.reader.start()

    def tearDown(self):
        self.reader.stop()
        self.state.close()
        self.directory.cleanup()

    def test_target_dates_and_preview_has_no_side_effects(self):
        with patch("agent_matrix.agents.daily_brief._today", return_value=DAY):
            morning = run(self.config, self.client, self.state, preview=True)
            evening = run(self.config, self.client, self.state, edition="evening", preview=True)
        self.assertEqual(morning["target_date"], "2026-11-16")
        self.assertEqual(evening["target_date"], "2026-11-17")
        self.assertIn("2026-11-17T00:00:00+08:00", self.client._exec.call_args.args[0])
        self.client.send_self.assert_not_called()
        self.assertIsNone(self.state.operation("brief:morning:2026-11-16"))
        self.assertEqual(self.state.pending(), [])

    def test_duplicate_run_does_not_resend_even_if_content_changes(self):
        first = run(self.config, self.client, self.state, target_date=DAY)
        self.read.return_value = [task("changed")]
        second = run(self.config, self.client, self.state, target_date=DAY)
        self.assertTrue(first["sent"])
        self.assertFalse(second["sent"])
        self.assertTrue(second["already_sent"])
        self.assertEqual(self.client.send_self.call_count, 1)
        self.assertEqual(self.client.send_self.call_args.args[1], "brief:morning:2026-11-16")
        self.assertEqual(self.read.call_count, 1)
        self.assertEqual(first["message_id"], "om_1")

    def test_preview_after_sent_is_still_possible_without_resending(self):
        run(self.config, self.client, self.state, target_date=DAY)
        result = run(self.config, self.client, self.state, target_date=DAY, preview=True)
        self.assertTrue(result["preview"])
        self.assertEqual(self.client.send_self.call_count, 1)

    def test_uncertain_send_stays_pending_and_never_blindly_replays(self):
        self.client.send_self.return_value = {"ok": True}
        with self.assertRaises(MatrixError):
            run(self.config, self.client, self.state, target_date=DAY)
        self.assertEqual(self.state.operation("brief:morning:2026-11-16")["status"], "pending")
        with self.assertRaises(MatrixError):
            run(self.config, self.client, self.state, target_date=DAY)
        self.assertEqual(self.client.send_self.call_count, 1)

    def test_edition_keys_separate_and_evening_completions_use_previous_day(self):
        self.read.return_value = [task("done", status="完成", 更新时间="2026-11-16T19:00+08:00")]
        evening = run(self.config, self.client, self.state, target_date="2026-11-17", edition="evening")
        morning = run(self.config, self.client, self.state, target_date="2026-11-17", edition="morning")
        self.assertNotEqual(evening["operation_key"], morning["operation_key"])
        self.assertEqual(self.client.send_self.call_count, 2)
        self.assertEqual(len(evening["recent_completed"]), 1)

    def test_legacy_base_without_summary_field_still_shows_real_draft(self):
        self.config.tables = {"事项表": "tbl_tasks"}
        doc = "https://example.feishu.cn/docx/ExistingDraft"
        self.read.return_value = [task("draft", status="草稿已备", AI草稿链接=doc)]
        self.client.call = Mock(return_value=[{"name": name} for name in FIELDS])
        result = run(self.config, self.client, self.state, target_date=DAY, preview=True)
        self.assertEqual(self.read.call_count, 1)
        self.assertNotIn(SUMMARY_FIELD, self.read.call_args.args[3])
        self.assertIn("AI草稿链接", self.read.call_args.args[3])
        self.client.call.assert_called_once()
        self.assertIn(doc, result["text"])
        self.assertIn("结果摘要尚未提供", result["text"])
        self.client.send_self.assert_not_called()

    def test_new_summary_field_is_verified_before_projecting_it(self):
        self.config.tables = {"事项表": "tbl_tasks"}
        draft = task("draft", status="草稿已备", AI草稿链接="https://example.feishu.cn/docx/Prepared")
        order = []

        def read(client, state, table, fields):
            order.append("summary_read" if SUMMARY_FIELD in fields else "base_read")
            return [{**draft, **({SUMMARY_FIELD: "已按三个章节完成周报初稿。"} if SUMMARY_FIELD in fields else {})}]

        def fields(*args):
            order.append("schema_check")
            return [{"field_name": name} for name in [*FIELDS, SUMMARY_FIELD]]

        self.read.side_effect = read
        self.client.call = Mock(side_effect=fields)
        result = run(self.config, self.client, self.state, target_date=DAY, preview=True)
        self.assertEqual(order, ["base_read", "schema_check", "summary_read"])
        self.assertIn("已按三个章节完成周报初稿", result["text"])
        self.client.send_self.assert_not_called()

    def test_schema_check_failure_is_not_misreported_as_missing_summary(self):
        self.config.tables = {"事项表": "tbl_tasks"}
        self.read.return_value = [task("draft", status="草稿已备")]
        self.client.call = Mock(side_effect=MatrixError("授权需要刷新"))
        with self.assertRaisesRegex(MatrixError, "授权需要刷新"):
            run(self.config, self.client, self.state, target_date=DAY)
        self.client.send_self.assert_not_called()
        self.assertIsNone(self.state.operation("brief:morning:2026-11-16"))

    def test_new_draft_does_not_resend_same_day_edition(self):
        run(self.config, self.client, self.state, target_date=DAY)
        self.read.return_value = [task("draft", status="草稿已备", AI草稿链接="https://example.feishu.cn/docx/New")]
        result = run(self.config, self.client, self.state, target_date=DAY)
        self.assertTrue(result["already_sent"])
        self.assertEqual(self.client.send_self.call_count, 1)
        self.assertEqual(self.read.call_count, 1)


if __name__ == "__main__":
    unittest.main()
