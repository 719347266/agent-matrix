import copy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent_matrix.brief_migration import (STATUS_MAP, build_plan, canonical_status, check_schema,
    definitions, number, setup_fields, source_digest, validate_registry, verify_approval)
from agent_matrix.models import Config, MatrixError
from agent_matrix.state import State


def row(rid="r1", n="1", status="草稿已备"):
    return {"record_id": rid, "编号": n, "标题": "已准备的材料", "状态": [status],
            "执行方": [], "负责人": [{"id": "owner"}], "截止日": "2026-11-17", "延期次数": 2,
            "AI草稿链接": "https://example.invalid/material"}


class MigrationTests(unittest.TestCase):
    def test_all_old_and_new_states_have_one_canonical_meaning(self):
        for old, new in STATUS_MAP.items():
            self.assertEqual(canonical_status([old]), new)
            self.assertEqual(canonical_status(old), new)
        for value in ([], ["待办", "完成"], "已归档", None):
            with self.subTest(value=value), self.assertRaises(MatrixError):
                canonical_status(value)

    def test_invalid_numbers_are_not_guessed(self):
        for value in (True, False, None, "NO.001", "01", "#1", -1, 0, 1.2, "", "1\n"):
            with self.subTest(value=value), self.assertRaises(MatrixError):
                number(value)
        self.assertEqual(number("12"), 12)

    def test_number_map_survives_sorting_deletion_and_new_records(self):
        first = validate_registry([row("r2", "2"), row()])
        second = validate_registry([row("r2", "2"), row("r3", "3")], first)
        self.assertEqual(second["by_number"], {"1": "r1", "2": "r2", "3": "r3"})
        self.assertEqual(second["by_record"]["r1"], 1)

    def test_duplicate_or_reused_numbers_fail_without_altering_registry(self):
        old = validate_registry([row()]); original = copy.deepcopy(old)
        for rows in ([row(), row("r2", "1")], [row(), row("r1", "2")], [row("r2", "1")], [row("r1", "2")]):
            with self.subTest(rows=rows), self.assertRaises(MatrixError):
                validate_registry(rows, old)
            self.assertEqual(old, original)

    def test_existing_schema_is_never_overwritten(self):
        expected = definitions("tbl")
        actual = {d["name"]: copy.deepcopy(d) for d in expected}
        check_schema(actual, expected)
        variants = [("编号", "type", "number"), ("编号", "style", {"rules": []}),
                    ("执行方", "multiple", True), ("父事项", "link_table", "wrong")]
        for name, key, value in variants:
            changed = copy.deepcopy(actual); changed[name][key] = value
            with self.subTest(name=name), self.assertRaises(MatrixError):
                check_schema(changed, expected)

    def test_plan_is_read_only_and_preserves_deadline_and_history(self):
        rows = [row()]; before = copy.deepcopy(rows)
        result = build_plan(rows, {"r1": {"executor": "AI", "reason": "已经备好材料供本人验收"}}, "spec")
        self.assertEqual(rows, before)
        item = result["items"][0]
        self.assertEqual((item["old_status"], item["proposed_status"], item["proposed_executor"]), ("草稿已备", "待验收", "AI"))
        self.assertEqual(item["due"], rows[0]["截止日"])
        self.assertEqual(item["defer_count"], 2)
        self.assertEqual(result["deadline_changes"], [])
        self.assertFalse(result["approved"])
        self.assertFalse(result["ready_for_activation"])

    def test_unknown_executor_or_incomplete_mapping_is_not_inferred(self):
        for proposal in ({}, {"r1": {"executor": "我/AI", "reason": "混合"}}, {"r1": {"executor": "AI"}}):
            with self.subTest(proposal=proposal), self.assertRaises(MatrixError):
                build_plan([row()], proposal, "spec")

    def test_mixed_work_waits_for_ai_child_acceptance_without_creating_children(self):
        proposal = {"r1": {"executor": "我", "reason": "取确认是本人动作", "split": {
            "ai_title": "准备待确认清单", "user_title": "发出并取得确认"}}}
        result = build_plan([row()], proposal, "spec")
        self.assertEqual(result["split_count"], 1)
        self.assertEqual(result["child_records_created"], 0)
        self.assertEqual(result["items"][0]["proposed_status"], "卡住")
        self.assertNotIn("number", result["items"][0]["split"])

    def test_exclusions_and_missing_information_preserved(self):
        r = row(); r.update(负责人=[], 截止日=None)
        item = build_plan([r], {"r1": {"executor": "别人", "reason": "本人明确不承担", "excluded_from_owner_work": True}}, "spec")["items"][0]
        self.assertTrue(item["excluded_from_owner_work"])
        self.assertEqual(item["missing"], ["负责人", "日期"])
        self.assertEqual(item["owners"], [])

    def test_approval_requires_explicit_reference_and_exact_snapshot(self):
        rows = [row()]; plan = build_plan(rows, {"r1": {"executor": "AI", "reason": "备稿"}}, "spec")
        approved = {"decision": "approve", "owner_message_ref": "verified-owner-message", "plan_id": plan["plan_id"]}
        self.assertTrue(verify_approval(plan, rows, approved))
        for approval in (None, {}, {**approved, "plan_id": "stale"}, {**approved, "owner_message_ref": ""}):
            with self.subTest(approval=approval), self.assertRaises(MatrixError):
                verify_approval(plan, rows, approval)
        changed = copy.deepcopy(rows); changed[0]["截止日"] = "2026-11-18"
        with self.assertRaises(MatrixError): verify_approval(plan, changed, approved)

    def test_snapshot_digest_does_not_depend_on_row_order(self):
        rows = [row(), row("r2", "2")]
        self.assertEqual(source_digest(rows), source_digest(rows[::-1]))

    def test_legacy_prework_does_not_start_new_work_during_mapping_review(self):
        from agent_matrix.prework import check
        with TemporaryDirectory() as tmp:
            state = State(Path(tmp)/"state.db")
            state.set("brief-v1:migration-stage", "fields_ready_awaiting_owner_mapping")
            client = Mock()
            result = check(Config(prework_enabled=True), client, state)
            self.assertFalse(result["run_model"])
            self.assertEqual(result["reason"], "executor_mapping_pending")
            self.assertEqual(result["ready"], [])
            self.assertEqual(client.mock_calls, [])
            state.close()

    def test_additive_setup_writes_only_new_fields_once(self):
        with TemporaryDirectory() as tmp:
            state = State(Path(tmp)/"state.db"); state.set("owned_base", "base")
            config = Config(base_token="base", tables={"事项表": "tbl"})
            schema = [{"name": "状态", "type": "select", "multiple": False, "options": [{"name": "草稿已备"}]}]
            writes = []
            def call(command, *args, write=False):
                if command == "+field-list": return copy.deepcopy(schema)
                self.assertEqual(command, "+field-create"); self.assertTrue(write)
                import json
                new = json.loads(args[args.index("--json")+1]); writes.append(new); schema.extend(new)
                return {"items": [{"created": True} for _ in new]}
            client = SimpleNamespace(call=call)
            with patch("agent_matrix.brief_migration.read_records", return_value=[row()]):
                a = setup_fields(config, client, state)
                b = setup_fields(config, client, state)
            self.assertEqual(len(writes), 1)
            self.assertEqual(len(writes[0]), 10)
            self.assertNotIn("状态", {d["name"] for d in writes[0]})
            self.assertFalse(a["executor_values_written"]); self.assertFalse(a["statuses_migrated"])
            self.assertEqual(b["created_fields"], [])
            state.close()


if __name__ == "__main__": unittest.main()
