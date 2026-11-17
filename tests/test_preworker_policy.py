from copy import deepcopy
from datetime import date, datetime
from types import SimpleNamespace
import unittest

from agent_matrix.agents.pre_worker import FIELDS, assess, fingerprint
from agent_matrix.models import MatrixError


def config(**changes):
    return SimpleNamespace(prework_enabled=True, prework_lead_days=2, owner_open_id="ou_me",
                           timezone="Asia/Shanghai", **changes)


def task(**changes):
    return {"record_id": "rec_1", "标题": "整理需求说明", "负责人": [{"id": "ou_me", "name": "我"}],
            "方向": ["我的"], "截止日": "2026-11-18T18:00:00+08:00", "状态": ["待办"],
            "下一步动作": "根据会议原文整理需求与未决问题", "来源": ["会议"],
            "来源链接": "https://example.test/meeting/1", "原话引用": "01:05 我来整理需求说明。",
            "预做策略": ["自动判断"], "预做指令": "保留待确认部分", **changes}


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_is_sha256_and_repeatable(self):
        self.assertRegex(fingerprint(task()), r"^[a-f0-9]{64}$")
        self.assertEqual(fingerprint(task()), fingerprint(dict(reversed(list(task().items())))))

    def test_actual_task_inputs_change_version(self):
        original = fingerprint(task())
        changes = [{"标题": "整理另一份需求说明"}, {"负责人": [{"id": "ou_other"}]},
                   {"截止日": "2026-11-19T18:00:00+08:00"}, {"下一步动作": "补充测试范围"},
                   {"来源": ["周期规则"]}, {"来源链接": "https://example.test/meeting/2"},
                   {"原话引用": "01:06 需要整理测试范围。"}, {"预做策略": ["仅备草稿"]},
                   {"预做指令": "先列大纲"}, {"方向": ["别人的"]}]
        for change in changes:
            with self.subTest(change=change):
                self.assertNotEqual(original, fingerprint(task(**change)))

    def test_automatic_fields_and_record_identity_do_not_change_version(self):
        updated = task(**{"状态": ["草稿已备"], "更新时间": "2026-11-16T19:00:00+08:00",
                          "AI草稿链接": "https://example.test/draft/1", "创建时间": "2026-11-16",
                          "record_id": "another_id", "能否AI预做": True, "上次预做版本": "old"})
        self.assertEqual(fingerprint(task()), fingerprint(updated))

    def test_owner_display_name_and_equivalent_select_shapes_are_not_new_inputs(self):
        self.assertEqual(fingerprint(task()), fingerprint(task(**{"负责人": [{"id": "ou_me", "name": "改了显示名"}],
                                                                    "预做策略": "自动判断", "来源": "会议", "方向": "我的"})))

    def test_empty_policy_means_same_default_automatic_assessment(self):
        for policy in [None, [], ""]:
            with self.subTest(policy=policy):
                self.assertEqual(fingerprint(task()), fingerprint(task(**{"预做策略": policy})))

    def test_equivalent_deadline_timezones_produce_same_version(self):
        self.assertEqual(fingerprint(task()), fingerprint(task(**{"截止日": "2026-11-18T10:00:00Z"})))

    def test_non_serializable_input_is_rejected(self):
        with self.assertRaises(MatrixError):
            fingerprint(task(**{"预做指令": object()}))


class PreworkAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.config = config()
        self.today = date(2026, 11, 16)

    def assess(self, rows):
        return assess(self.config, rows, today=self.today)

    def test_due_boundary_includes_today_plus_two_days(self):
        row = task(**{"来源": ["周期规则"]})
        result = self.assess([row])
        self.assertTrue(result["ok"])
        self.assertEqual(result["candidate_count"], 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["record_id"], "rec_1")
        self.assertEqual(candidate["due_date"], "2026-11-18")
        self.assertEqual(candidate["input_version"], fingerprint(row))
        self.assertEqual(candidate["policy"], "自动判断")
        self.assertEqual(set(candidate["fields"]), set(FIELDS))
        self.assertNotIn("can_execute", candidate)

    def test_future_after_cutoff_is_skipped(self):
        result = self.assess([task(**{"来源": ["周期规则"], "截止日": "2026-11-19"})])
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["skipped"][0]["code"], "too_early")

    def test_overdue_items_remain_visible(self):
        result = self.assess([task(**{"截止日": "2025-01-01"})])
        self.assertEqual(result["candidate_count"], 1)
        self.assertIn("已逾期", result["candidates"][0]["reason"])

    def test_china_day_boundary_uses_timezone(self):
        result = self.assess([task(**{"来源": ["周期规则"], "截止日": "2026-11-18T16:00:00Z"})])
        self.assertEqual(result["candidates"], [])
        result = self.assess([task(**{"来源": ["周期规则"], "截止日": "2026-11-18T15:59:59Z"})])
        self.assertEqual(result["candidates"][0]["due_date"], "2026-11-18")

    def test_cross_week_meeting_tasks_are_assessed_immediately(self):
        for status in [["待办"], ["进行中"]]:
            with self.subTest(status=status):
                result = self.assess([task(**{"截止日": "2026-11-30", "状态": status})])
                self.assertEqual(result["candidate_count"], 1)
                candidate = result["candidates"][0]
                self.assertEqual(candidate["due_date"], "2026-11-30")
                self.assertIn("来源为会议，直接评估", candidate["reason"])
                self.assertNotIn("近期到期", candidate["reason"])

    def test_same_distant_deadline_is_immediate_for_meeting_only(self):
        rows = [task(record_id="meeting", **{"截止日": "2026-11-30"}),
                task(record_id="recurring", **{"来源": ["周期规则"], "截止日": "2026-11-30"})]
        result = self.assess(rows)
        self.assertEqual([item["record_id"] for item in result["candidates"]], ["meeting"])
        self.assertEqual(result["skipped"][0]["record_id"], "recurring")
        self.assertEqual(result["skipped"][0]["code"], "too_early")

    def test_ambiguous_or_unknown_sources_cannot_claim_immediate_meeting_assessment(self):
        for source in [["会议", "周期规则"], ["会议", "会议"], [], None, "", ["未知"],
                       ["手动"], ["会议", ""], {"name": "会议"}]:
            with self.subTest(source=source):
                result = self.assess([task(**{"来源": source, "截止日": "2026-11-30"})])
                self.assertEqual(result["candidates"], [])
                self.assertEqual(result["skipped"][0]["code"], "too_early")

    def test_immediate_meeting_assessment_still_requires_other_eligibility_checks(self):
        for fields, code in [({"负责人": [{"id": "ou_other"}]}, "owner_other"),
                             ({"负责人": None}, "owner_missing"), ({"预做策略": ["不代做"]}, "policy_declined"),
                             ({"状态": ["完成"]}, "state_ineligible"), ({"截止日": "下个月"}, "due_invalid")]:
            with self.subTest(fields=fields):
                row = task(**{"截止日": "2026-11-30", **fields})
                result = self.assess([row])
                self.assertEqual(result["candidates"], [])
                self.assertEqual(result["skipped"][0]["code"], code)

    def test_missing_deadline_is_candidate_without_guessing_date(self):
        for value in [None, ""]:
            with self.subTest(value=value):
                candidate = self.assess([task(**{"截止日": value})])["candidates"][0]
                self.assertIsNone(candidate["due_date"])
                self.assertIn("截止日待确认", candidate["reason"])
                self.assertEqual(candidate["fields"]["截止日"], value)

    def test_bad_deadline_is_not_treated_as_missing(self):
        for value in ["下周", "2026-02-30", "2026-11-18T18:00:00", 1795000000000, True, []]:
            with self.subTest(value=value):
                result = self.assess([task(**{"截止日": value})])
                self.assertEqual(result["candidates"], [])
                self.assertEqual(result["skipped"][0]["code"], "due_invalid")

    def test_other_missing_ambiguous_owners_never_default_to_self(self):
        values = [([{"id": "ou_other"}], "owner_other"), (None, "owner_missing"),
                  ([], "owner_missing"), ([{"name": "我"}], "owner_ambiguous"),
                  ([{"id": "ou_me"}, {"id": "ou_other"}], "owner_ambiguous"), ("ou_me", "owner_ambiguous")]
        for owners, code in values:
            with self.subTest(owners=owners):
                result = self.assess([task(**{"负责人": owners})])
                self.assertEqual(result["candidates"], [])
                self.assertEqual(result["skipped"][0]["code"], code)

    def test_user_opt_out_and_unknown_policies_skip_with_reason(self):
        for policy, code in [(["不代做"], "policy_declined"), (["直接提交"], "policy_unknown"),
                             (["自动判断", "仅备草稿"], "policy_unknown"), (True, "policy_unknown")]:
            with self.subTest(policy=policy):
                result = self.assess([task(**{"预做策略": policy})])
                self.assertEqual(result["candidates"], [])
                self.assertEqual(result["skipped"][0]["code"], code)
                self.assertTrue(result["skipped"][0]["reason"])

    def test_empty_automatic_and_draft_only_policies_can_be_assessed(self):
        for policy in [None, [], "", "自动判断", ["自动判断"], ["仅备草稿"]]:
            with self.subTest(policy=policy):
                result = self.assess([task(**{"预做策略": policy})])
                self.assertEqual(result["candidate_count"], 1)
        candidate = self.assess([task(**{"预做策略": ["仅备草稿"]})])["candidates"][0]
        self.assertEqual(candidate["policy"], "仅备草稿")
        self.assertIn("仅备草稿", candidate["reason"])

    def test_completed_cancelled_drafted_or_unconfirmed_do_not_reenter(self):
        for status in [["完成"], ["取消"], ["草稿已备"], ["待确认"], ["阻塞"], None, [], ["待办", "进行中"]]:
            with self.subTest(status=status):
                self.assertEqual(self.assess([task(**{"状态": status})])["candidates"], [])
        self.assertEqual(self.assess([task(**{"状态": "进行中"})])["candidate_count"], 1)

    def test_owned_meeting_pending_missing_deadline_allows_explicit_draft_only(self):
        for missing in [None, ""]:
            with self.subTest(deadline=missing):
                row = task(**{"状态": ["待确认"], "截止日": missing, "预做策略": ["仅备草稿"]})
                before = deepcopy(row)
                result = self.assess([row])
                self.assertEqual(result["candidate_count"], 1)
                candidate = result["candidates"][0]
                self.assertEqual(candidate["policy"], "仅备草稿")
                self.assertIsNone(candidate["due_date"])
                self.assertEqual(candidate["fields"]["状态"], ["待确认"])
                self.assertEqual(candidate["fields"]["截止日"], missing)
                self.assertIn("不视为日期已确认", candidate["reason"])
                self.assertEqual(row, before)

    def test_pending_meeting_draft_exception_cannot_bypass_other_uncertainty(self):
        base = {"状态": ["待确认"], "截止日": None, "预做策略": ["仅备草稿"]}
        denied = [
            {"预做策略": ["自动判断"]}, {"预做策略": None}, {"预做策略": []},
            {"预做策略": ["仅备草稿", "自动判断"]}, {"预做策略": ["不代做"]},
            {"来源": ["手动"]}, {"来源": ["周期规则"]}, {"来源": None},
            {"来源": ["会议", "手动"]}, {"来源": ["会议", "会议"]},
            {"截止日": "2026-11-18"}, {"截止日": "以后确认"}, {"截止日": " "},
            {"截止日": []}, {"截止日": False}, {"截止日": 0},
            {"负责人": None}, {"负责人": []}, {"负责人": [{"id": "ou_other"}]},
            {"负责人": [{"id": "ou_me"}, {"id": "ou_other"}]},
            {"状态": ["阻塞"]}, {"状态": ["完成"]}, {"状态": ["取消"]},
        ]
        for changes in denied:
            with self.subTest(changes=changes):
                result = self.assess([task(**{**base, **changes})])
                self.assertEqual(result["candidates"], [])

    def test_pending_meeting_draft_still_requires_prework_enablement(self):
        self.config.prework_enabled = False
        result = self.assess([task(**{"状态": ["待确认"], "截止日": None, "预做策略": ["仅备草稿"]})])
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["skipped"][0]["code"], "prework_disabled")

    def test_sort_is_due_first_no_date_last_and_stable_across_input_order(self):
        rows = [task(record_id="no-date", **{"截止日": None}), task(record_id="z"), task(record_id="a"),
                task(record_id="overdue", **{"截止日": "2026-11-15"})]
        first = self.assess(rows)
        second = self.assess(list(reversed(rows)))
        self.assertEqual(first, second)
        self.assertEqual([item["record_id"] for item in first["candidates"]], ["overdue", "a", "z", "no-date"])

    def test_pure_idempotent_and_result_does_not_alias_original_fields(self):
        rows = [task(), task(record_id="skip", **{"预做策略": ["不代做"]})]
        before = deepcopy(rows)
        first, second = self.assess(rows), self.assess(rows)
        self.assertEqual(first, second)
        self.assertEqual(rows, before)
        first["candidates"][0]["fields"]["负责人"][0]["id"] = "changed-output-only"
        self.assertEqual(rows, before)

    def test_title_keywords_never_make_execution_decisions(self):
        for title in ["写周报", "支付工资", "直接发送邮件", "不知道如何执行的事项"]:
            with self.subTest(title=title):
                result = self.assess([task(**{"标题": title})])
                self.assertEqual(result["candidate_count"], 1)
                self.assertIn("评估可代做性", result["candidates"][0]["reason"])

    def test_disabled_does_not_generate_candidates(self):
        self.config.prework_enabled = False
        result = self.assess([task()])
        self.assertTrue(result["ok"])
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["skipped"][0]["code"], "prework_disabled")

    def test_unconfigured_owner_fails_closed(self):
        self.config.owner_open_id = ""
        result = self.assess([task()])
        self.assertFalse(result["ok"])
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["skipped"][0]["code"], "owner_unconfigured")

    def test_configurable_lead_and_default_two_days(self):
        self.config.prework_lead_days = 1
        row = task(**{"来源": ["周期规则"]})
        self.assertEqual(self.assess([row])["candidates"], [])
        del self.config.prework_lead_days
        self.assertEqual(self.assess([row])["candidate_count"], 1)

    def test_invalid_config_and_date_do_not_silently_guess(self):
        for value in [-1, True, 1.5, "2"]:
            with self.subTest(value=value), self.assertRaises(MatrixError):
                self.config.prework_lead_days = value
                self.assess([task()])
        with self.assertRaises(MatrixError):
            assess(config(), [task()], today=datetime(2026, 11, 16))

    def test_invalid_or_duplicate_record_identity_is_not_actionable(self):
        result = self.assess([task(), task(), task(record_id=""), None])
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["skipped_count"], 4)


if __name__ == "__main__":
    unittest.main()
