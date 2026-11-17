from copy import deepcopy
from datetime import date, datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_matrix.agents.recurring import plan, run
from agent_matrix.models import Config, MatrixError
from agent_matrix.state import State


def rule(**changes):
    return {"record_id": "recWeekly", "名称": "周期复盘", "频率": "FREQ=WEEKLY;BYDAY=FR",
            "提前天数": 2, "默认负责人": [{"id": "ou_owner", "name": "负责人"}],
            "预计耗时(分钟)": 30, "检查清单": "检查本周待办", "能否AI预做": False,
            "启用": True, **changes}


class RecurringPlanTests(unittest.TestCase):
    def test_friday_is_created_on_wednesday_with_two_day_lead(self):
        result = plan([rule()], [], date(2026, 11, 18))
        self.assertEqual(result["planned_count"], 1)
        row = result["create_records"][0]
        self.assertEqual(row["截止日"], "2026-11-20")
        self.assertEqual(row["首次截止日"], "2026-11-20")
        self.assertEqual(row["去重键"], "rule:recWeekly:2026-11-20")
        self.assertEqual(row["状态"], ["待办"])
        self.assertEqual(row["类型"], ["周期事项"])
        self.assertEqual(row["来源"], ["周期规则"])
        self.assertEqual(row["负责人"], [{"id": "ou_owner", "name": "负责人"}])

    def test_due_today_is_included_but_past_occurrence_is_not_backfilled(self):
        self.assertEqual(plan([rule(**{"提前天数": 0})], [], date(2026, 11, 20))["planned_count"], 1)
        self.assertEqual(plan([rule()], [], date(2026, 11, 21))["planned_count"], 0)

    def test_daily_rule_generates_each_distinct_due_date_including_both_bounds(self):
        result = plan([rule(**{"频率": "FREQ=DAILY"})], [], date(2026, 11, 18))
        self.assertEqual([row["截止日"] for row in result["create_records"]],
                         ["2026-11-18", "2026-11-19", "2026-11-20"])
        self.assertEqual(len({row["去重键"] for row in result["create_records"]}), 3)

    def test_completed_cancelled_and_open_remote_records_all_deduplicate(self):
        for status in ["完成", "取消", "待办"]:
            with self.subTest(status=status):
                existing = [{"record_id": "recItem", "去重键": "rule:recWeekly:2026-11-20", "状态": [status]}]
                result = plan([rule()], existing, date(2026, 11, 18))
                self.assertEqual(result["create_records"], [])
                self.assertEqual(result["skipped_count"], 1)
                self.assertEqual(result["candidate_count"], 1)

    def test_previous_occurrence_completion_does_not_suppress_next_date(self):
        existing = [{"record_id": "recOld", "去重键": "rule:recWeekly:2026-11-13", "状态": ["完成"]}]
        self.assertEqual(plan([rule()], existing, date(2026, 11, 18))["planned_count"], 1)

    def test_disabled_invalid_examples_and_non_boolean_enable_are_ignored(self):
        result = plan([{"启用": False, "频率": "bad", "提前天数": -4}, {}, {"启用": "true"}, {"启用": 1}], [], date(2026, 11, 18))
        self.assertEqual(result["disabled_count"], 4)
        self.assertEqual(result["enabled_count"], 0)
        self.assertEqual(result["create_records"], [])

    def test_month_end_skips_impossible_day_31_in_february(self):
        item = rule(**{"频率": "FREQ=MONTHLY;BYMONTHDAY=31", "提前天数": 61})
        result = plan([item], [], date(2027, 1, 30))
        self.assertEqual([row["截止日"] for row in result["create_records"]], ["2027-01-31", "2027-03-31"])

    def test_negative_monthday_is_actual_last_day(self):
        item = rule(**{"频率": "FREQ=MONTHLY;BYMONTHDAY=-1", "提前天数": 2})
        self.assertEqual(plan([item], [], date(2028, 2, 27))["create_records"][0]["截止日"], "2028-02-29")

    def test_year_boundary_and_explicit_yearly_date(self):
        item = rule(**{"频率": "FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=1", "提前天数": 2})
        result = plan([item], [], date(2026, 12, 30))
        self.assertEqual(result["create_records"][0]["截止日"], "2027-01-01")

    def test_explicit_monthly_weekday(self):
        item = rule(**{"频率": "FREQ=MONTHLY;BYDAY=-1FR", "提前天数": 2})
        self.assertEqual(plan([item], [], date(2026, 11, 25))["create_records"][0]["截止日"], "2026-11-27")

    def test_missing_owner_needs_confirmation_and_does_not_guess_user(self):
        for missing in (None, []):
            with self.subTest(owner=missing):
                row = plan([rule(**{"默认负责人": missing})], [], date(2026, 11, 18))["create_records"][0]
                self.assertEqual(row["状态"], ["待确认"])
                self.assertNotIn("负责人", row)
                self.assertNotIn("方向", row)

    def test_inherits_only_prework_flag_not_execution_or_instructions(self):
        row = plan([rule(**{"能否AI预做": True, "预做指令": "先写草稿"})], [], date(2026, 11, 18))["create_records"][0]
        self.assertIs(row["能否AI预做"], True)
        self.assertNotIn("预做指令", row)
        self.assertNotIn("AI草稿链接", row)

    def test_prework_instructions_are_carried_verbatim_when_requested(self):
        instructions = "先列大纲\n保留没有确认的部分，不提交。"
        row = plan([rule(**{"预做指令": instructions})], [], date(2026, 11, 18),
                   include_prework_instructions=True)["create_records"][0]
        self.assertEqual(row["预做指令"], instructions)
        self.assertNotIn("预做策略", row)
        self.assertNotIn("预做结果摘要", row)
        self.assertNotIn("预做凭据", row)
        self.assertEqual(row["状态"], ["待办"])

    def test_prework_floor_moves_creation_day_not_due_day_or_rule(self):
        rules = [rule(**{"提前天数": 0})]
        before = deepcopy(rules)
        self.assertEqual(plan(rules, [], date(2026, 11, 18))["create_records"], [])
        row = plan(rules, [], date(2026, 11, 18), minimum_lead_days=2)["create_records"][0]
        self.assertEqual(row["截止日"], "2026-11-20")
        self.assertEqual(row["首次截止日"], "2026-11-20")
        self.assertEqual(row["负责人"], rules[0]["默认负责人"])
        self.assertEqual(row["去重键"], "rule:recWeekly:2026-11-20")
        self.assertEqual(rules, before)

    def test_prework_floor_does_not_shorten_longer_rule_window(self):
        rules = [rule(**{"提前天数": 10})]
        original = plan(rules, [], date(2026, 11, 18))
        expanded = plan(rules, [], date(2026, 11, 18), minimum_lead_days=2)
        self.assertEqual(expanded, original)
        self.assertEqual([row["截止日"] for row in expanded["create_records"]], ["2026-11-20", "2026-11-27"])

    def test_prework_floor_never_repairs_invalid_or_enables_disabled_rules(self):
        for bad in [{"提前天数": None}, {"提前天数": -1}, {"频率": "FREQ=WEEKLY"}, {"预做指令": ["先做"]}]:
            with self.subTest(bad=bad), self.assertRaises(MatrixError):
                plan([rule(**bad)], [], date(2026, 11, 18), minimum_lead_days=2, include_prework_instructions=True)
        result = plan([{"启用": False, "频率": "", "提前天数": None, "预做指令": []}], [],
                      date(2026, 11, 18), minimum_lead_days=2, include_prework_instructions=True)
        self.assertEqual(result["disabled_count"], 1)
        self.assertEqual(result["create_records"], [])

    def test_interval_anchor_is_stable_across_runs(self):
        item = rule(**{"频率": "DTSTART;VALUE=DATE:20261106\nRRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=FR"})
        result = plan([item], [], date(2026, 11, 18))
        self.assertEqual(result["create_records"][0]["截止日"], "2026-11-20")
        self.assertEqual(plan([item], [], date(2026, 11, 25))["create_records"], [])
        self.assertEqual(plan([item], [], date(2026, 12, 2))["create_records"][0]["截止日"], "2026-12-04")

    def test_count_requires_anchor_and_eventually_stops(self):
        item = rule(**{"频率": "DTSTART:20261113T090000\nRRULE:FREQ=WEEKLY;BYDAY=FR;COUNT=2"})
        self.assertEqual(plan([item], [], date(2026, 11, 18))["planned_count"], 1)
        self.assertEqual(plan([item], [], date(2026, 11, 25))["planned_count"], 0)

    def test_explicit_utc_anchor_is_converted_to_china_due_date(self):
        item = rule(**{"频率": "DTSTART:20261119T200000Z\nRRULE:FREQ=DAILY;COUNT=1", "提前天数": 0})
        result = plan([item], [], date(2026, 11, 20))
        self.assertEqual(result["create_records"][0]["截止日"], "2026-11-20")
        self.assertEqual(plan([item], [], date(2026, 11, 19))["planned_count"], 0)

    def test_explicit_tzid_and_float_datetime_do_not_set_task_deadline_hour(self):
        for start in ["DTSTART;TZID=Asia/Shanghai:20261120T093000", "DTSTART:20261120T093000"]:
            with self.subTest(start=start):
                item = rule(**{"频率": start + "\nRRULE:FREQ=DAILY;COUNT=1"})
                row = plan([item], [], date(2026, 11, 18))["create_records"][0]
                self.assertEqual(row["截止日"], "2026-11-20")

    def test_until_date_is_respected(self):
        item = rule(**{"频率": "FREQ=DAILY;UNTIL=20261119", "提前天数": 3})
        self.assertEqual([row["截止日"] for row in plan([item], [], date(2026, 11, 18))["create_records"]],
                         ["2026-11-18", "2026-11-19"])

    def test_incomplete_or_unsafe_recurrence_is_rejected(self):
        cases = ["FREQ=WEEKLY", "FREQ=MONTHLY", "FREQ=YEARLY;BYMONTH=1", "FREQ=DAILY;INTERVAL=2",
                 "FREQ=DAILY;COUNT=2", "FREQ=HOURLY", "FREQ=MINUTELY", "FREQ=SECONDLY",
                 "FREQ=DAILY;INTERVAL=0", "FREQ=DAILY;INTERVAL=-1", "FREQ=DAILY;BYHOUR=8,9",
                 "FREQ=WEEKLY;BYDAY=0FR", "FREQ=MONTHLY;BYMONTHDAY=0", "FREQ=DAILY;BYMONTH=13",
                 "FREQ=DAILY;COUNT=2;UNTIL=20261120", "FREQ=DAILY;FREQ=WEEKLY", "FREQ=DAILY;BYMINUTE=15",
                 "FREQ=DAILY;UNTIL=2026", "FREQ=DAILY;UNTIL=202611", "FREQ=DAILY;UNTIL=20261120T09",
                 "DTSTART:20260230T090000\nRRULE:FREQ=DAILY", "DTSTART;TZID=Not/AZone:20261120T090000\nRRULE:FREQ=DAILY",
                 "FREQ=DAILY\nRDATE:20261120", "RRULE:FREQ=DAILY\nRRULE:FREQ=WEEKLY"]
        for frequency in cases:
            with self.subTest(frequency=frequency), self.assertRaises(MatrixError):
                plan([rule(**{"频率": frequency})], [], date(2026, 11, 18))

    def test_invalid_lead_or_owner_or_estimate_rejected_before_planning(self):
        for lead in [None, "2", -1, 367, 0.5, True, float("nan"), float("inf")]:
            with self.subTest(lead=lead), self.assertRaises(MatrixError):
                plan([rule(**{"提前天数": lead})], [], date(2026, 11, 18))
        for values in [{"默认负责人": "ou_owner"}, {"默认负责人": [{"name": "负责人"}]},
                       {"预计耗时(分钟)": -1}, {"预计耗时(分钟)": float("inf")}, {"能否AI预做": "true"}]:
            with self.subTest(values=values), self.assertRaises(MatrixError):
                plan([rule(**values)], [], date(2026, 11, 18))

    def test_plan_is_pure_and_invalid_duplicate_rule_ids_rejected(self):
        rules, existing = [rule()], [{"record_id": "manual", "状态": ["待办"]}]
        before = deepcopy((rules, existing))
        plan(rules, existing, date(2026, 11, 18))
        self.assertEqual((rules, existing), before)
        with self.assertRaises(MatrixError):
            plan([rule(), rule()], [], date(2026, 11, 18))
        with self.assertRaises(MatrixError):
            plan([rule()], [], datetime(2026, 11, 18))


class RecurringRunTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = State(Path(self.directory.name) / "state.db")
        self.addCleanup(self.state.close)
        self.config = Config(base_token="owned-base", tables={"周期规则表": "rules", "事项表": "tasks"})
        self.state.set("owned_base", "owned-base")
        self.rules = [rule()]
        self.existing = []
        self.writes = []
        self.receipt = {"record_id_list": ["recCreated"]}
        self.readback_records = None
        owner = self

        class Client:
            config = owner.config

            def call(self, command, *args, **kwargs):
                owner.writes.append((command, args, kwargs))
                return owner.receipt

        self.client = Client()
        self.read = patch("agent_matrix.records.read_records", side_effect=self.read_records).start()
        self.addCleanup(patch.stopall)
        self.fields = patch("agent_matrix.records.ensure_fields").start()

    def read_records(self, client, state, table, fields):
        if table == "事项表" and "标题" in fields:
            if self.readback_records is not None:
                return deepcopy(self.readback_records)
            rows = []
            for _, args, _ in self.writes:
                row = json.loads(args[args.index("--json") + 1])["create_records"][0]
                row["record_id"] = self.receipt["record_id_list"][0]
                row["截止日"] += "T00:00:00+08:00"
                row["首次截止日"] += "T00:00:00+08:00"
                rows.append(row)
            return rows
        return deepcopy(self.rules if table == "周期规则表" else self.existing)

    def execute(self, **kwargs):
        return run(self.config, self.client, self.state, today=date(2026, 11, 18), **kwargs)

    def test_writes_only_owned_task_table_with_date_only_select_arrays(self):
        result = self.execute()
        self.assertEqual(result["created_count"], 1)
        self.assertEqual(result["created_ids"], ["recCreated"])
        command, args, kwargs = self.writes[0]
        self.assertEqual(command, "+record-batch-create")
        self.assertEqual(args[args.index("--base-token") + 1], "owned-base")
        self.assertEqual(args[args.index("--table-id") + 1], "tasks")
        self.assertEqual(kwargs, {"write": True})
        row = json.loads(args[args.index("--json") + 1])["create_records"][0]
        self.assertEqual(row["截止日"], "2026-11-20")
        self.assertEqual(row["状态"], ["待办"])
        self.assertEqual(self.state.operation(row["去重键"])["status"], "done")
        self.assertIs(self.state.operation(row["去重键"])["receipt"]["verified_by_readback"], True)

    def test_repeated_run_deduplicates_with_state_even_when_remote_snapshot_stale(self):
        self.execute()
        result = self.execute()
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(result["created_count"], 0)
        self.assertEqual(result["cached_count"], 1)
        self.assertEqual(result["skipped_count"], 1)

    def test_remote_completed_record_deduplicates_without_state(self):
        self.existing = [{"record_id": "old", "去重键": "rule:recWeekly:2026-11-20", "状态": ["完成"]}]
        result = self.execute()
        self.assertEqual(result["created_count"], 0)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.state.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 0)

    def test_preview_reads_but_never_writes_or_creates_operations(self):
        result = self.execute(preview=True)
        self.assertEqual(result["planned_count"], 1)
        self.assertEqual(result["created_count"], 0)
        self.assertTrue(result["preview"])
        self.assertEqual(self.read.call_count, 2)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.state.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 0)

    def test_enabled_prework_creates_early_and_carries_instruction_with_readback(self):
        self.config.prework_enabled = True
        self.rules = [rule(**{"提前天数": 0, "预做指令": "整理周报草稿，待本人确认"})]
        result = self.execute()
        self.assertEqual(result["created_count"], 1)
        row = result["create_records"][0]
        self.assertEqual(row["截止日"], "2026-11-20")
        self.assertEqual(row["预做指令"], "整理周报草稿，待本人确认")
        self.assertEqual(row["状态"], ["待办"])
        self.assertIn("预做指令", self.read.call_args_list[0].args[3])
        self.assertIn("预做指令", self.fields.call_args_list[1].args[2])
        self.assertIn("预做指令", self.read.call_args_list[-1].args[3])
        self.assertEqual(self.execute()["created_count"], 0)
        self.assertEqual(len(self.writes), 1)

    def test_enabled_prework_preview_expands_window_without_write(self):
        self.config.prework_enabled = True
        self.rules = [rule(**{"提前天数": 0})]
        result = self.execute(preview=True)
        self.assertEqual(result["planned_count"], 1)
        self.assertEqual(result["created_count"], 0)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.state.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 0)

    def test_disabled_prework_preserves_legacy_window_and_field_contract(self):
        self.config.prework_enabled = False
        self.rules = [rule(**{"提前天数": 0, "预做指令": "规则原先已有，但事项表尚未迁移"})]
        result = self.execute(preview=True)
        self.assertEqual(result["planned_count"], 0)
        for call in self.fields.call_args_list:
            self.assertNotIn("预做指令", call.args[2])
        self.assertNotIn("预做指令", self.read.call_args_list[0].args[3])
        self.rules[0]["提前天数"] = 2
        self.assertNotIn("预做指令", self.execute()["create_records"][0])

    def test_enabled_prework_only_requires_new_target_field_when_it_writes_instructions(self):
        self.config.prework_enabled = True
        self.execute(preview=True)
        self.assertNotIn("预做指令", self.fields.call_args_list[1].args[2])
        for field in ["预做策略", "预做结果摘要", "预做凭据"]:
            self.assertNotIn(field, self.fields.call_args_list[1].args[2])
        self.rules[0]["预做指令"] = "先列大纲"
        self.fields.reset_mock()
        self.fields.side_effect = [None, MatrixError("事项表缺少必要字段：预做指令")]
        with self.assertRaisesRegex(MatrixError, "预做指令"):
            self.execute()
        self.assertEqual(self.writes, [])
        self.assertEqual(self.state.pending(), [])

    def test_one_day_prework_setting_does_not_silently_use_two_days(self):
        self.config.prework_enabled = True
        self.config.prework_lead_days = 1
        self.rules = [rule(**{"提前天数": 0})]
        self.assertEqual(self.execute(preview=True)["planned_count"], 0)
        self.rules[0]["提前天数"] = 3
        self.assertEqual(self.execute(preview=True)["planned_count"], 1)

    def test_disabled_seed_stays_disabled_with_prework_enabled(self):
        self.config.prework_enabled = True
        self.rules = [{"record_id": "sample", "名称": "示例", "启用": False}]
        result = self.execute()
        self.assertEqual(result["disabled_count"], 1)
        self.assertEqual(result["created_count"], 0)
        self.assertEqual(self.writes, [])

    def test_later_invalid_rule_prevents_all_creates(self):
        self.rules.append(rule(**{"record_id": "bad", "频率": "FREQ=MONTHLY"}))
        with self.assertRaises(MatrixError):
            self.execute()
        self.assertEqual(self.writes, [])
        self.assertEqual(self.state.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 0)

    def test_missing_target_field_prevents_all_creates(self):
        self.fields.side_effect = [None, MatrixError("事项表缺少必要字段")]
        with self.assertRaises(MatrixError):
            self.execute()
        self.assertEqual(self.writes, [])
        self.assertEqual(self.state.pending(), [])

    def test_pending_second_occurrence_stops_entire_batch_before_first_write(self):
        self.rules = [rule(**{"频率": "FREQ=DAILY"})]
        self.state.db.execute("INSERT INTO operations VALUES (?, 'pending', NULL, 'now')", ("rule:recWeekly:2026-11-19",))
        self.state.db.commit()
        preview = self.execute(preview=True)
        self.assertFalse(preview["ok"])
        self.assertEqual(preview["pending_keys"], ["rule:recWeekly:2026-11-19"])
        with self.assertRaises(MatrixError):
            self.execute()
        self.assertEqual(self.writes, [])

    def test_missing_receipt_keeps_operation_pending_and_prevents_retry(self):
        self.receipt = {}
        with self.assertRaises(MatrixError):
            self.execute()
        self.assertEqual(self.state.pending(), ["rule:recWeekly:2026-11-20"])
        with self.assertRaises(MatrixError):
            self.execute()
        self.assertEqual(len(self.writes), 1)

    def test_ignored_field_receipt_stops_and_keeps_pending(self):
        self.receipt["ignored_fields"] = ["截止日"]
        with self.assertRaisesRegex(MatrixError, "字段被忽略"):
            self.execute()
        self.assertEqual(self.state.pending(), ["rule:recWeekly:2026-11-20"])

    def test_missing_readback_does_not_report_success(self):
        self.readback_records = []
        with self.assertRaisesRegex(MatrixError, "读回不完整"):
            self.execute()
        self.assertEqual(self.state.pending(), ["rule:recWeekly:2026-11-20"])

    def test_readback_wrong_status_does_not_report_success(self):
        row = plan(self.rules, [], date(2026, 11, 18))["create_records"][0]
        self.readback_records = [{**row, "record_id": "recCreated", "状态": ["完成"]}]
        with self.assertRaisesRegex(MatrixError, "字段不一致"):
            self.execute()
        self.assertEqual(self.state.pending(), ["rule:recWeekly:2026-11-20"])

    def test_readback_wrong_day_does_not_report_success(self):
        row = plan(self.rules, [], date(2026, 11, 18))["create_records"][0]
        self.readback_records = [{**row, "record_id": "recCreated", "截止日": "2026-11-19T00:00:00+08:00"}]
        with self.assertRaisesRegex(MatrixError, "字段不一致"):
            self.execute()
        self.assertEqual(self.state.pending(), ["rule:recWeekly:2026-11-20"])

    def test_uncertain_remote_row_does_not_silently_clear_pending_on_next_run(self):
        self.receipt["ignored_fields"] = ["状态"]
        with self.assertRaises(MatrixError):
            self.execute()
        self.existing = [{"record_id": "recCreated", "去重键": "rule:recWeekly:2026-11-20", "状态": None}]
        preview = self.execute(preview=True)
        self.assertFalse(preview["ok"])
        self.assertEqual(preview["pending_keys"], ["rule:recWeekly:2026-11-20"])
        with self.assertRaises(MatrixError):
            self.execute()
        self.assertEqual(len(self.writes), 1)

    def test_proven_not_sent_operation_can_be_retried(self):
        self.state.db.execute("INSERT INTO operations VALUES (?, 'not_sent', NULL, 'now')", ("rule:recWeekly:2026-11-20",))
        self.state.db.commit()
        result = self.execute()
        self.assertTrue(result["ok"])
        self.assertEqual(result["created_count"], 1)

    def test_refuses_unowned_base_before_remote_reads(self):
        self.state.set("owned_base", "different-base")
        with self.assertRaises(MatrixError):
            self.execute()
        self.assertEqual(self.read.call_count, 0)
        self.assertEqual(self.writes, [])


if __name__ == "__main__":
    unittest.main()
