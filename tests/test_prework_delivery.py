"""Prework delivery faults use temporary SQLite and a simulated Feishu only."""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_matrix import prework
from agent_matrix.models import Config, MatrixError
from agent_matrix.prework_store import WorkQueue
from agent_matrix.state import State
from agent_matrix.timeutil import TZ


XML = "<title>测试材料：待确认</title><p>本地测试数据的核对结果，仅供本人确认。</p>"


class FakeFeishu:
    def __init__(self, root, rows):
        self.root = root
        self.rows = rows
        self.created = 0
        self.updated = 0
        self.sent = []
        self.inits = 0
        self.doc_receipt = {"document": {"document_id": "doc_test", "url": "https://test.example/docx/doc_test"}, "warnings": []}
        self.remote_content = None
        self.send_receipt = {"message_id": "om_test"}
        self.parse_status = "passed"
        self.readback_link = lambda url: url
        self.command_log = []

    def _exec(self, argv):
        self.command_log.append(list(argv))
        if argv[:2] == ["docs", "+script"]:
            operation = argv[argv.index("--command") + 1]
            if operation == "init-draft":
                self.inits += 1
                directory = self.root / "drafts" / f"draft-{self.inits}"
                directory.mkdir(parents=True)
                (directory / ".presentation-decision.json").write_text("{}")
                return {"cwd": str(self.root), "workspace": str(directory), "draft_path": str(directory / "draft.xml")}
            if operation == "parse":
                return {"assessment": {"status": self.parse_status}}
        if argv[:2] == ["docs", "+create"]:
            if "--dry-run" in argv:
                return {"dry_run": True}
            self.created += 1
            self.created_content = (self.root / argv[argv.index("--content") + 1].removeprefix("@./")).read_text()
            return deepcopy(self.doc_receipt)
        if argv[:2] == ["docs", "+fetch"]:
            return {"document": {"document_id": "doc_test", "content": self.remote_content if self.remote_content is not None else self.created_content}}
        raise AssertionError("Unexpected native command: " + repr(argv))

    def call(self, command, *argv, **kwargs):
        if command != "+record-batch-update" or not kwargs.get("write"):
            raise AssertionError("Unexpected Base operation")
        updates = json.loads(argv[argv.index("--json") + 1])["update_records"]
        self.updated += 1
        for record_id, fields in updates.items():
            stored = deepcopy(fields)
            if "AI草稿链接" in stored:
                stored["AI草稿链接"] = self.readback_link(stored["AI草稿链接"])
            next(row for row in self.rows if row["record_id"] == record_id).update(stored)
        return {"updated_records": list(updates)}

    def send_self(self, message, key):
        self.sent.append((message, key))
        return deepcopy(self.send_receipt)


class PreworkDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.state = State(self.root / "state/state.db")
        self.addCleanup(self.state.close)
        self.queue = WorkQueue(self.state)
        self.config = Config(owner_open_id="ou_owner", base_token="base_test", base_url="https://test.example/base/base_test",
                             tables={"事项表": "tbl_test"}, prework_enabled=True)
        self.rows = [{"record_id": "rec_test", "标题": "核对本地测试材料", "负责人": [{"id": "ou_owner", "name": "本人"}],
                      "方向": ["我的"], "截止日": datetime.now(TZ).date().isoformat(), "状态": ["待办"],
                      "下一步动作": "根据已提供材料整理核对结果", "来源": ["手动"], "来源链接": "",
                      "原话引用": "", "预做策略": ["自动判断"], "预做指令": "核对材料后交本人确认"}]
        self.client = FakeFeishu(self.root, self.rows)
        self.row_patch = patch("agent_matrix.prework._rows", side_effect=lambda *_: deepcopy(self.rows))
        self.row_patch.start()
        self.addCleanup(self.row_patch.stop)

    def discover_and_prepare(self):
        checked = prework.check(self.config, self.client, self.state)
        self.assertTrue(checked["run_model"])
        self.version = checked["ready"][0]["input_version"]
        self.key = prework.key_for("rec_test", self.version)
        result = prework.prepare(self.config, self.client, self.state, "rec_test", self.version)
        self.assertTrue(Path(result["context_path"]).is_file())
        draft = prework.draft_init(self.config, self.client, self.state, "rec_test", self.version,
                                  {"audience": "本人", "reader_task": "核对成果并决定是否接受"})
        self.artifact = self.root / draft["draft_path"]
        self.artifact.write_text(XML)
        self.payload = {"record_id": "rec_test", "input_version": self.version, "kind": "internal_result",
                        "template": "其他",
                        "summary": "本地测试材料核对已备好", "artifact_path": draft["draft_path"],
                        "artifact_sha256": sha256(self.artifact.read_bytes()).hexdigest(),
                        "checks": ["核对全部测试输入字段"], "source_refs": ["local-test-fixture"],
                        "limitations": [], "confirmation": "请确认材料是否可用", "external_actions_performed": False}
        self.result_path = self.root / "result.json"
        self.save_payload()
        return result

    def save_payload(self):
        self.result_path.write_text(json.dumps(self.payload, ensure_ascii=False))

    def prepared(self):
        self.discover_and_prepare()
        return prework.stage(self.config, self.client, self.state, self.result_path)

    def publish(self):
        return prework.publish(self.config, self.client, self.state, "rec_test", self.version)

    def test_complete_flow_is_delivered_for_review_without_business_completion(self):
        self.prepared()
        result = self.publish()
        self.assertEqual(result["status"], "awaiting_owner_confirmation")
        self.assertEqual(self.rows[0]["状态"], ["草稿已备"])
        self.assertEqual(self.rows[0]["AI草稿链接"], result["url"])
        self.assertEqual(self.rows[0]["预做凭据"], self.version)
        sent = prework.notify(self.config, self.client, self.state)
        self.assertTrue(sent["sent"])
        self.assertEqual(self.queue.get("rec_test", self.version)["status"], "done")
        self.assertEqual(self.rows[0]["状态"], ["草稿已备"])
        self.assertIn(result["url"], self.client.sent[0][0])
        self.assertIn("请确认", self.client.sent[0][0])

    def test_repeat_publish_notify_does_not_duplicate_documents_messages_or_writeback(self):
        self.prepared()
        first = self.publish()
        self.assertEqual(self.publish(), first)
        prework.notify(self.config, self.client, self.state)
        self.assertEqual(self.publish(), first)
        self.assertFalse(prework.notify(self.config, self.client, self.state)["sent"])
        self.assertEqual((self.client.created, self.client.updated, len(self.client.sent)), (1, 1, 1))
        self.assertFalse(prework.check(self.config, self.client, self.state)["run_model"])

    def test_markdown_base_url_readback_supports_publish_resume_and_notify(self):
        self.prepared()
        self.client.readback_link = lambda url: f"[{url}]({url})"
        result = self.publish()
        self.assertEqual(self.rows[0]["AI草稿链接"], f"[{result['url']}]({result['url']})")
        self.assertEqual(self.publish(), result)
        self.assertEqual(self.state.operation(self.key + ":writeback")["status"], "done")
        prework.notify(self.config, self.client, self.state)
        self.assertEqual(self.queue.get("rec_test", self.version)["status"], "done")
        self.assertEqual((self.client.created, self.client.updated, len(self.client.sent)), (1, 1, 1))

    def test_structured_base_url_readback_uses_href_not_display_text(self):
        self.prepared()
        self.client.readback_link = lambda url: [{"url": url, "text": "已准备成果"}]
        result = self.publish()
        for represented in [result["url"], {"url": result["url"], "text": "任意显示文字"},
                            [{"url": result["url"]}], [result["url"]], f"[任意显示文字]({result['url']})"]:
            with self.subTest(represented=represented):
                self.rows[0]["AI草稿链接"] = represented
                self.assertEqual(self.publish(), result)
        prework.notify(self.config, self.client, self.state)
        self.assertEqual(len(self.client.sent), 1)

    def test_markdown_label_cannot_hide_wrong_href_during_writeback(self):
        self.prepared()
        self.client.readback_link = lambda url: f"[{url}](https://test.example/docx/other_doc)"
        with self.assertRaises(MatrixError):
            self.publish()
        self.assertIsNone(self.state.get(self.key + ":published"))
        self.assertEqual(self.state.operation(self.key + ":writeback")["status"], "pending")
        self.assertEqual(self.client.sent, [])

    def test_url_normalization_rejects_ambiguous_invalid_or_different_links(self):
        self.prepared(); result = self.publish()
        expected = result["url"]
        values = [
            f"[{expected}](https://test.example/docx/other_doc)",
            {"url": "https://test.example/docx/other_doc", "text": expected},
            [{"url": expected}, {"url": "https://test.example/docx/other_doc"}],
            f"prefix [{expected}]({expected})", f"[{expected}]({expected}) suffix",
            f"[{expected}](javascript:alert(1))", {"text": expected}, [], None,
            "https://test.example/docx/doc_test?changed=1", "https://test.example/docx/doc_test#changed",
            "https://test.example/docx/doc_test\ntrailing", "https://user@test.example/docx/doc_test",
        ]
        for represented in values:
            with self.subTest(represented=represented):
                self.rows[0]["AI草稿链接"] = represented
                with self.assertRaises(MatrixError):
                    self.publish()
        self.assertEqual((self.client.created, self.client.updated, len(self.client.sent)), (1, 1, 0))

    def test_cancelled_or_changed_owner_or_inputs_block_publication(self):
        self.prepared()
        original = deepcopy(self.rows[0])
        for change in ({"状态": ["取消"]}, {"状态": ["完成"]}, {"负责人": [{"id": "ou_other"}]},
                       {"下一步动作": "人工已修改事项范围"}, {"预做策略": ["不代做"]}):
            with self.subTest(change=change):
                self.rows[0].clear(); self.rows[0].update(deepcopy(original)); self.rows[0].update(change)
                with self.assertRaises(MatrixError):
                    self.publish()
        self.assertEqual((self.client.created, self.client.updated), (0, 0))

    def test_published_item_cancellation_prevents_stale_result_notification(self):
        self.prepared(); self.publish()
        self.rows[0]["状态"] = ["取消"]
        try:
            prework.notify(self.config, self.client, self.state)
        except MatrixError:
            pass
        self.assertEqual(self.client.sent, [])

    def test_draft_only_policy_rejects_internal_result(self):
        self.rows[0]["预做策略"] = ["仅备草稿"]
        self.discover_and_prepare()
        with self.assertRaises(MatrixError):
            prework.stage(self.config, self.client, self.state, self.result_path)
        self.payload["kind"] = "draft"; self.save_payload()
        self.assertTrue(prework.stage(self.config, self.client, self.state, self.result_path)["ok"])

    def test_local_artifact_path_escape_and_hash_change_are_rejected(self):
        self.discover_and_prepare()
        original_path = self.payload["artifact_path"]
        self.payload["artifact_path"] = "../outside.xml"; self.save_payload()
        with self.assertRaises(MatrixError):
            prework.stage(self.config, self.client, self.state, self.result_path)
        self.payload["artifact_path"] = original_path; self.save_payload()
        self.artifact.write_text(XML + "<p>changed after hashing</p>")
        with self.assertRaises(MatrixError):
            prework.stage(self.config, self.client, self.state, self.result_path)
        self.assertEqual(self.client.created, 0)

    def test_failed_native_parse_prevents_publication(self):
        self.discover_and_prepare(); self.client.parse_status = "failed"
        with self.assertRaises(MatrixError):
            prework.stage(self.config, self.client, self.state, self.result_path)
        self.assertIsNone(self.state.get(self.key + ":validated"))
        self.assertEqual(self.client.created, 0)

    def test_post_validation_artifact_edit_prevents_publication(self):
        self.prepared(); self.artifact.write_text(XML + "<p>changed after validation</p>")
        with self.assertRaises(MatrixError):
            self.publish()
        self.assertEqual(self.client.created, 0)

    def test_creation_warning_never_creates_second_document(self):
        self.prepared(); self.client.doc_receipt["warnings"] = ["simulated partial material"]
        for _ in range(2):
            with self.assertRaises(MatrixError):
                self.publish()
        self.assertEqual((self.client.created, self.client.updated, len(self.client.sent)), (1, 0, 0))
        self.assertEqual(self.rows[0]["状态"], ["待办"])

    def test_missing_document_receipt_never_creates_second_document(self):
        self.prepared(); self.client.doc_receipt = {"document": {}}
        for _ in range(2):
            with self.assertRaises(MatrixError):
                self.publish()
        self.assertEqual((self.client.created, self.client.updated), (1, 0))

    def test_wrong_document_readback_blocks_ready_state_without_recreation(self):
        self.prepared(); self.client.remote_content = "<title>不同文档</title><p>没有原始内容</p>"
        for _ in range(2):
            with self.assertRaises(MatrixError):
                self.publish()
        self.assertEqual((self.client.created, self.client.updated), (1, 0))
        self.assertEqual(self.rows[0]["状态"], ["待办"])

    def test_missing_message_id_stays_pending_and_blocks_blind_resend(self):
        self.prepared(); self.publish(); self.client.send_receipt = {}
        for _ in range(2):
            with self.assertRaises(MatrixError):
                prework.notify(self.config, self.client, self.state)
        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual(self.queue.get("rec_test", self.version)["status"], "prepared")
        batch = self.state.get(self.key + ":batch")
        self.assertEqual(self.state.operation(batch)["status"], "pending")
        self.assertIsNone(self.state.get(self.key + ":notice"))

    def test_restart_after_writeback_before_published_state_reuses_document(self):
        self.prepared()
        original_set = self.state.set
        def crash(key, value):
            if key == self.key + ":published":
                raise OSError("simulated interruption after verified writeback")
            return original_set(key, value)
        with patch.object(self.state, "set", side_effect=crash):
            with self.assertRaises(OSError):
                self.publish()
        self.assertEqual(self.rows[0]["状态"], ["草稿已备"])
        result = self.publish()
        self.assertEqual(result["url"], "https://test.example/docx/doc_test")
        self.assertEqual((self.client.created, self.client.updated), (1, 1))
        prework.notify(self.config, self.client, self.state)
        self.assertEqual(len(self.client.sent), 1)

    def test_notice_persistence_failure_reuses_successful_send_receipt(self):
        self.prepared(); self.publish()
        original_set = self.state.set
        def crash(key, value):
            if key == self.key + ":notice":
                raise OSError("simulated notice persistence interruption")
            return original_set(key, value)
        with patch.object(self.state, "set", side_effect=crash):
            with self.assertRaises(OSError):
                prework.notify(self.config, self.client, self.state)
        prework.notify(self.config, self.client, self.state)
        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual(self.queue.get("rec_test", self.version)["status"], "done")

    def test_queue_completion_interruption_cannot_leave_permanent_prepared_item(self):
        self.prepared(); self.publish()
        with patch.object(WorkQueue, "done", side_effect=OSError("simulated queue transition interruption")):
            with self.assertRaises(OSError):
                prework.notify(self.config, self.client, self.state)
        prework.notify(self.config, self.client, self.state)
        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual(self.queue.get("rec_test", self.version)["status"], "done")

    def test_blocked_item_reports_once_then_quiet(self):
        self.discover_and_prepare()
        self.queue.block("rec_test", self.version, "缺少指定材料")
        self.assertTrue(prework.check(self.config, self.client, self.state)["run_model"])
        self.assertTrue(prework.notify(self.config, self.client, self.state)["sent"])
        self.assertIn(self.queue.get("rec_test", self.version)["reason"], self.client.sent[0][0])
        self.assertFalse(prework.notify(self.config, self.client, self.state)["sent"])
        self.assertFalse(prework.check(self.config, self.client, self.state)["run_model"])

    def test_cancelled_queued_item_does_not_keep_waking_model(self):
        found = prework.check(self.config, self.client, self.state)
        self.assertTrue(found["run_model"])
        self.rows[0]["状态"] = ["取消"]
        self.assertFalse(prework.check(self.config, self.client, self.state)["run_model"])
        self.assertEqual(self.client.sent, [])

    def test_unknown_notification_receipt_does_not_wake_model_forever(self):
        self.prepared(); self.publish(); self.client.send_receipt = {}
        with self.assertRaises(MatrixError):
            prework.notify(self.config, self.client, self.state)
        self.queue.block("rec_test", self.version, "消息回执缺失，待核对")
        checked = prework.check(self.config, self.client, self.state)
        self.assertFalse(checked["run_model"])
        self.assertEqual(checked["awaiting_receipt_count"], 1)
        self.assertEqual(len(self.client.sent), 1)

    def test_concrete_missing_material_is_preserved(self):
        self.discover_and_prepare()
        reason = "缺少本周渠道投放费用和转化明细，需补充后才能核算"
        self.queue.block("rec_test", self.version, reason)
        prework.notify(self.config, self.client, self.state)
        self.assertIn(reason, self.client.sent[0][0])

    def seed_published_batch(self, count):
        """Create already-published fixture results; never call a real service."""
        template = deepcopy(self.rows[0])
        self.rows[:] = [{**deepcopy(template), "record_id": f"rec_batch_{i}", "标题": f"已准备材料 {i}"}
                        for i in range(count)]
        ready = prework.check(self.config, self.client, self.state)["ready"]
        fixture = self.root / "batch-fixture.xml"
        fixture.write_text(XML)
        now = datetime.now(TZ)
        for row in ready:
            record_id, version = row["record_id"], row["input_version"]
            self.queue.claim(record_id, version, now)
            self.queue.prepared(record_id, version, fixture.name, sha256(fixture.read_bytes()).hexdigest(), now)
            url = "https://test.example/docx/" + record_id
            self.state.set(prework.key_for(record_id, version) + ":published", {
                "record_id": record_id, "input_version": version, "url": url, "summary": "已经核对的测试成果",
                "confirmation": "请确认材料", "limitations": [], "kind": "draft", "status": "awaiting_owner_confirmation"})
            next(r for r in self.rows if r["record_id"] == record_id).update({
                "状态": ["草稿已备"], "AI草稿链接": url, "预做凭据": version})
        return ready

    def test_explicit_four_items_are_merged_into_one_message(self):
        ready = self.seed_published_batch(4)
        result = prework.notify(self.config, self.client, self.state, max_items=4)
        self.assertEqual(result["count"], 4)
        self.assertEqual(len(self.client.sent), 1)
        for row in ready:
            self.assertIn("https://test.example/docx/" + row["record_id"], self.client.sent[0][0])
            self.assertEqual(self.queue.get(row["record_id"], row["input_version"])["status"], "done")
        self.assertFalse(prework.notify(self.config, self.client, self.state, max_items=4)["sent"])

    def test_default_batch_still_limits_to_three_results(self):
        self.seed_published_batch(4)
        result = prework.notify(self.config, self.client, self.state)
        self.assertEqual(result["count"], 3)
        self.assertEqual(sum(r["status"] == "done" for r in self.queue.list()), 3)
        self.assertEqual(sum(r["status"] == "prepared" for r in self.queue.list()), 1)

    def test_six_results_can_merge_five_drafts_and_one_blocker(self):
        ready = self.seed_published_batch(6)
        blocked = ready[-1]
        key = prework.key_for(blocked["record_id"], blocked["input_version"])
        self.state.set(key + ":published", None)
        self.queue.block(blocked["record_id"], blocked["input_version"], "缺少头像验收材料")
        result = prework.notify(self.config, self.client, self.state, max_items=6)
        self.assertEqual(result["count"], 6)
        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual(self.client.sent[0][0].count("https://test.example/docx/"), 5)
        self.assertIn("缺少头像验收材料", self.client.sent[0][0])
        self.assertEqual(sum(r["status"] == "done" for r in self.queue.list()), 5)
        self.assertIsNotNone(self.state.get(key + ":notice"))
        self.assertFalse(prework.notify(self.config, self.client, self.state, max_items=6)["sent"])

    def test_eight_is_valid_explicit_batch_boundary(self):
        self.seed_published_batch(8)
        result = prework.notify(self.config, self.client, self.state, max_items=8)
        self.assertEqual(result["count"], 8)
        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual(sum(r["status"] == "done" for r in self.queue.list()), 8)

    def test_invalid_max_items_rejected_before_touching_queue_or_sending(self):
        ready = self.seed_published_batch(1)
        before = self.queue.list()
        for invalid in [True, False, 0, -1, 9, 1.5, "4", None, []]:
            with self.subTest(max_items=invalid), self.assertRaises(MatrixError):
                prework.notify(self.config, self.client, self.state, max_items=invalid)
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.queue.list(), before)
        key = prework.key_for(ready[0]["record_id"], ready[0]["input_version"])
        self.assertIsNone(self.state.get(key + ":batch"))

    def test_oversized_utf8_message_rejected_without_truncation_or_delivery(self):
        ready = self.seed_published_batch(1)
        key = prework.key_for(ready[0]["record_id"], ready[0]["input_version"])
        result = self.state.get(key + ":published")
        # Fewer than 18k characters, but more than 18k UTF-8 bytes. The limit
        # must include complete limitations instead of silently truncating them.
        result["limitations"] = ["需核实" * 2100]
        self.state.set(key + ":published", result)
        with self.assertRaisesRegex(MatrixError, "18000"):
            prework.notify(self.config, self.client, self.state, max_items=1)
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.queue.get(ready[0]["record_id"], ready[0]["input_version"])["status"], "prepared")
        batch = self.state.get(key + ":batch")
        self.assertIsNone(self.state.operation(batch))
        self.assertIsNone(self.state.get(key + ":notice"))

    def test_frozen_batch_retry_ignores_new_max_items_and_reuses_message(self):
        ready = self.seed_published_batch(4)
        with patch.object(WorkQueue, "done", side_effect=OSError("simulated completion interruption")):
            with self.assertRaises(OSError):
                prework.notify(self.config, self.client, self.state, max_items=4)
        original_text, original_batch = self.client.sent[0]
        original_members = self.state.get(original_batch + ":members")
        self.assertEqual(len(original_members), 4)
        resumed = prework.notify(self.config, self.client, self.state, max_items=1)
        self.assertEqual(resumed["count"], 4)
        self.assertEqual(self.client.sent, [(original_text, original_batch)])
        self.assertEqual(self.state.get(original_batch + ":members"), original_members)
        self.assertTrue(all(self.queue.get(r["record_id"], r["input_version"])["status"] == "done" for r in ready))

    def test_cli_notify_passes_explicit_or_default_max_items(self):
        for arguments, expected in [([], 3), (["--max-items", "4"], 4)]:
            with self.subTest(arguments=arguments):
                config_path = self.root / "config.yaml"
                self.state.set("owned_base", "base_test")
                with patch("agent_matrix.prework.load", return_value=self.config), \
                     patch("agent_matrix.prework.Feishu") as client_class, \
                     patch("agent_matrix.prework.notify", return_value={"ok": True}) as notify, \
                     patch("builtins.print"):
                    self.assertEqual(prework.main(["notify", "--config", str(config_path), *arguments]), 0)
                    self.assertEqual(notify.call_args.kwargs, {"max_items": expected})
                    self.assertIs(notify.call_args.args[1], client_class.return_value)


if __name__ == "__main__":
    unittest.main()
