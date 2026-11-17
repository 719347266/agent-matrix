from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agent_matrix.automation import Item, Evidence, decide, evidence_key, sync_one
from agent_matrix.automation_demo import DemoGateway, run_demo
from agent_matrix.digest import Notice, build_digest, merge_notices, delivery_plan
from agent_matrix.feishu import FeishuError
from agent_matrix.models import MatrixError
from agent_matrix.state import State


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.now = 1800000000000
        self.item = Item("r1", "提交材料", "进行中", "v1", self.now-10000, "t1", "task_completed", True)
        self.evidence = Evidence("e1", "r1", "t1", "task_status", "completed", self.now-1000, self.now,
                                 "https://example.invalid/t1")
        self.tmp = TemporaryDirectory()
        self.state = State(Path(self.tmp.name)/"state.db")
        self.g = DemoGateway([self.item], [self.evidence])

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def sync(self, item=None, evidence=None):
        return sync_one(item or self.item, evidence or self.evidence, self.g, self.state, now_ms=self.now)

    def test_authoritative_completion_and_repeat(self):
        self.assertTrue(self.sync()["checked"])
        self.assertEqual(self.sync()["status"], "already_synced")
        self.assertEqual(self.g.writes, ["r1"])

    def test_message_and_file_cannot_complete(self):
        for kind in ("message", "artifact"):
            self.assertEqual(self.sync(evidence=replace(self.evidence, kind=kind))["status"], "review")
        self.assertEqual(self.g.writes, [])

    def test_wrong_item_or_source_rejected(self):
        for e in (replace(self.evidence, item_id="r2"), replace(self.evidence, source_id="t2")):
            self.assertEqual(self.sync(evidence=e)["status"], "skip")
        self.assertEqual(self.g.writes, [])

    def test_preset_criterion_is_required(self):
        for item in (replace(self.item, auto_enabled=False), replace(self.item, rule="manual"),
                     replace(self.item, rule="approval_passed")):
            self.assertFalse(self.sync(item=item)["checked"])

    def test_partial_draft_is_not_approval(self):
        item = replace(self.item, rule="approval_passed")
        draft = replace(self.evidence, kind="approval_status", outcome="submitted")
        approved = replace(draft, outcome="approved")
        self.assertEqual(decide(item, draft, now_ms=self.now).action, "skip")
        self.assertEqual(decide(item, approved, now_ms=self.now).action, "complete")

    def test_stale_future_and_missing_evidence(self):
        for e in (replace(self.evidence, source_url=""), replace(self.evidence, happened_ms=0),
                  replace(self.evidence, observed_ms=self.now+1),
                  replace(self.evidence, happened_ms=self.now-400000, observed_ms=self.now-400000)):
            self.assertEqual(self.sync(evidence=e)["status"], "review")

    def test_manual_edits_and_cancelled_items_preserved(self):
        for status in ("待确认", "阻塞", "草稿已备", "完成", "取消"):
            self.assertFalse(self.sync(item=replace(self.item, status=status))["checked"])
        self.g.items["r1"] = replace(self.item, revision="v2")
        self.assertEqual(self.sync()["status"], "review")
        self.assertEqual(self.g.writes, [])

    def test_manual_reopen_does_not_replay_old_evidence(self):
        self.sync()
        self.g.items["r1"] = replace(self.item, revision="v3", updated_ms=self.now)
        self.assertEqual(self.sync(item=self.g.items["r1"])["status"], "review")
        self.assertEqual(self.g.writes, ["r1"])

    def test_source_reopened_before_write(self):
        self.g.evidence["r1"] = replace(self.evidence, outcome="incomplete")
        self.assertEqual(self.sync()["status"], "review")
        self.assertEqual(self.g.writes, [])

    def test_success_receipt_without_readback_never_shows_checked(self):
        self.g.write_complete = lambda *args: {"ok": True}
        self.assertEqual(self.sync()["status"], "pending_verification")
        self.assertFalse(self.sync()["checked"])

    def test_timeout_never_blindly_retries(self):
        attempts = []
        def fail(*args):
            attempts.append(1)
            raise FeishuError("timeout")
        self.g.write_complete = fail
        with self.assertRaises(FeishuError): self.sync()
        self.assertEqual(self.sync()["status"], "review")
        self.assertEqual(len(attempts), 1)

    def test_timeout_can_be_reconciled_from_matching_remote_proof(self):
        write = self.g.write_complete
        def fail_after_write(*args):
            write(*args)
            raise FeishuError("timeout")
        self.g.write_complete = fail_after_write
        with self.assertRaises(FeishuError): self.sync()
        self.assertTrue(self.sync(item=self.g.items["r1"])["checked"])
        self.assertEqual(self.state.pending(), [])
        self.assertEqual(self.g.writes, ["r1"])

    def test_proven_not_sent_can_retry(self):
        write = self.g.write_complete
        def fail(*args):
            error = FeishuError("preview_failed"); error.not_sent = True
            raise error
        self.g.write_complete = fail
        with self.assertRaises(FeishuError): self.sync()
        self.g.write_complete = write
        self.assertTrue(self.sync()["checked"])

    def test_merge_is_scoped_deduplicated_and_keeps_evidence(self):
        n = Notice("m1", "r1", "评审时间", "上午", 1, "https://example.invalid/m1")
        n2 = replace(n, id="m2", text="改为下午", timestamp_ms=2)
        n3 = replace(n, id="m3", item_id="out_of_scope")
        n4 = replace(n, id="m4", necessary=False)
        groups = merge_notices([n2,n,n,n3,n4], {"r1"})
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["count"], 2)
        self.assertEqual(groups[0]["latest"], "改为下午")
        self.assertEqual(groups[0]["messages"][0]["text"], "上午")

    def test_notification_is_one_per_window_and_updates_existing(self):
        digest = build_digest([self.item], [], [])
        first = delivery_plan(digest, window="evening")
        previous = {**first, "message_id": "om_demo"}
        self.assertEqual(first["action"], "send_once")
        self.assertEqual(delivery_plan(digest, window="evening", previous=previous)["action"], "skip_unchanged")
        changed = build_digest([replace(self.item, title="新标题")], [], [])
        self.assertEqual(delivery_plan(changed, window="evening", previous=previous)["action"], "update_existing")
        self.assertEqual(delivery_plan(digest, window="morning", previous=previous)["action"], "send_once")
        self.assertEqual(delivery_plan(build_digest([],[],[]), window="evening")["action"], "skip_empty")

    def test_demo_does_not_write_remote_and_shows_uncertainty(self):
        result = run_demo()
        self.assertEqual(result["remote_writes"], 0)
        self.assertEqual(result["test_table_writes"], 1)
        self.assertEqual(result["duplicate_notification"], "skip_unchanged")
        self.assertEqual(len(result["digest"]["completed"]), 1)
        self.assertEqual(len(result["digest"]["needs_confirmation"]), 1)


if __name__ == "__main__":
    unittest.main()
