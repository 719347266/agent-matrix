from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from agent_matrix.models import MatrixError
from agent_matrix.prework_store import WorkQueue
from agent_matrix.state import State


TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 11, 16, 9, 0, tzinfo=TZ)
DIGEST = hashlib.sha256(b"draft").hexdigest()


def candidate(record_id="rec_1", version="v1", **extra):
    return {"record_id": record_id, "input_version": version, "title": "周报草稿",
            "due_date": "2026-11-20", "reason": "已取得工作材料", "fields": {"状态": ["待办"]},
            "policy": {"prepare_only": True}, **extra}


class WorkQueueTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name)/"state.db"
        self.state = State(self.path)
        self.queue = WorkQueue(self.state)

    def tearDown(self):
        self.state.close()
        self.directory.cleanup()

    def seed(self, record_id="rec_1", version="v1"):
        return self.queue.discover([candidate(record_id, version)], NOW)[0]

    def prepared(self, record_id="rec_1", version="v1"):
        self.seed(record_id, version)
        self.queue.claim(record_id, version, NOW)
        return self.queue.prepared(record_id, version, "/drafts/weekly.md", DIGEST, NOW + timedelta(minutes=1))

    def test_full_flow_delivers_for_review_without_business_completion(self):
        row = self.seed()
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["candidate"]["fields"]["状态"], ["待办"])
        self.assertEqual(len(self.queue.ready(NOW)), 1)
        claimed = self.queue.claim("rec_1", "v1", NOW)
        self.assertEqual(claimed["status"], "running")
        prepared = self.queue.prepared("rec_1", "v1", Path("/drafts/weekly.md"), DIGEST.upper(), NOW)
        self.assertEqual(prepared["status"], "prepared")
        self.assertEqual(prepared["result_hash"], DIGEST)
        with patch("agent_matrix.prework_store._now", return_value=NOW + timedelta(minutes=2)):
            result = self.queue.done("rec_1", "v1", {"data": {"message_id": "om_1"}})
        self.assertEqual(result["status"], "done")
        self.assertTrue(result["delivered_for_review"])
        self.assertEqual(result["candidate"]["fields"]["状态"], ["待办"])
        self.assertEqual(result["receipt"], {"data": {"message_id": "om_1"}})
        self.assertEqual(self.queue.ready(NOW + timedelta(days=1)), [])

    def test_repeated_discover_preserves_every_progress_field(self):
        self.prepared()
        before = self.queue.get("rec_1", "v1")
        found = self.queue.discover([candidate(title="新标题但错误沿用旧版本")], NOW + timedelta(days=1))[0]
        self.assertEqual(found, before)
        self.assertEqual(len(self.queue.list()), 1)

    def test_equal_titles_are_not_task_identity(self):
        self.queue.discover([candidate("rec_a"), candidate("rec_b"), candidate("rec_a")], NOW)
        self.assertEqual([row["record_id"] for row in self.queue.list()], ["rec_a", "rec_b"])

    def test_restart_keeps_prepared_evidence_and_can_resume_after_lease(self):
        original = self.prepared()
        self.state.close()
        self.state = State(self.path)
        self.queue = WorkQueue(self.state)
        self.assertEqual(self.queue.get("rec_1", "v1"), original)
        self.assertIsNone(self.queue.claim("rec_1", "v1", NOW + timedelta(minutes=29)))
        row = self.queue.claim("rec_1", "v1", NOW + timedelta(minutes=30))
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["result_path"], "/drafts/weekly.md")
        self.assertEqual(row["result_sha256"], DIGEST)
        self.assertEqual(row["claim_count"], 2)
        resumed = self.queue.prepared("rec_1", "v1", row["result_path"], row["result_sha256"], NOW + timedelta(minutes=31))
        self.assertEqual(resumed["prepared_at"], original["prepared_at"])

    def test_new_version_stales_running_work_and_old_snapshot_cannot_revive_it(self):
        self.prepared()
        before = self.queue.get("rec_1", "v1")
        self.queue.discover([candidate(version="v2")], NOW + timedelta(minutes=2))
        old = self.queue.get("rec_1", "v1")
        self.assertEqual(old["status"], "stale")
        self.assertEqual(old["result_path"], before["result_path"])
        self.assertEqual(old["candidate"], before["candidate"])
        self.assertIsNone(self.queue.claim("rec_1", "v1", NOW + timedelta(days=1)))
        with self.assertRaises(MatrixError):
            self.queue.prepared("rec_1", "v1", "/drafts/weekly.md", DIGEST, NOW + timedelta(minutes=3))
        with self.assertRaises(MatrixError):
            self.queue.done("rec_1", "v1", {"message_id": "om_old"})
        self.queue.discover([candidate(version="v1")], NOW + timedelta(minutes=4))
        self.assertEqual(self.queue.get("rec_1", "v1")["status"], "stale")
        self.assertEqual(self.queue.get("rec_1", "v2")["status"], "queued")
        self.assertEqual([x["input_version"] for x in self.queue.ready(NOW + timedelta(minutes=4))], ["v2"])

    def test_version_changes_stale_queued_and_running_but_keep_done_evidence(self):
        for record_id, status in (("queued", "queued"), ("running", "running")):
            self.seed(record_id)
            if status == "running":
                self.queue.claim(record_id, "v1", NOW)
            self.queue.discover([candidate(record_id, "v2")], NOW)
            self.assertEqual(self.queue.get(record_id, "v1")["status"], "stale")
        self.prepared("delivered")
        self.queue.done("delivered", "v1", {"message_id": "om_delivered"})
        self.queue.discover([candidate("delivered", "v2")], NOW)
        self.assertEqual(self.queue.get("delivered", "v1")["status"], "done")
        self.assertEqual(self.queue.get("delivered", "v1")["receipt"], {"message_id": "om_delivered"})

    def test_active_leases_are_not_ready_or_double_claimed(self):
        self.seed()
        self.queue.claim("rec_1", "v1", NOW, lease_minutes=45)
        self.assertEqual(self.queue.ready(NOW + timedelta(minutes=44)), [])
        self.assertIsNone(self.queue.claim("rec_1", "v1", NOW + timedelta(minutes=44)))
        self.assertEqual(len(self.queue.ready(NOW + timedelta(minutes=45))), 1)
        self.assertIsNotNone(self.queue.claim("rec_1", "v1", NOW + timedelta(minutes=45)))

    def test_two_connections_cannot_claim_same_live_lease(self):
        self.seed()
        other_state = State(self.path)
        try:
            other = WorkQueue(other_state)
            self.assertIsNotNone(self.queue.claim("rec_1", "v1", NOW))
            self.assertIsNone(other.claim("rec_1", "v1", NOW))
        finally:
            other_state.close()

    def test_done_requires_message_receipt_and_prepared_result(self):
        self.prepared()
        for invalid in (None, {}, {"ok": True}, {"message_id": "  "}, {"message_id": 3}):
            with self.assertRaises(MatrixError):
                self.queue.done("rec_1", "v1", invalid)
            self.assertEqual(self.queue.get("rec_1", "v1")["status"], "prepared")
        self.seed("not_prepared")
        with self.assertRaises(MatrixError):
            self.queue.done("not_prepared", "v1", {"message_id": "om_2"})

    def test_duplicate_done_is_idempotent_but_cannot_replace_receipt(self):
        self.prepared()
        original = self.queue.done("rec_1", "v1", {"message_id": "om_1", "proof": "first"})
        repeated = self.queue.done("rec_1", "v1", {"data": {"message_id": "om_1"}})
        self.assertEqual(repeated, original)
        with self.assertRaises(MatrixError):
            self.queue.done("rec_1", "v1", {"message_id": "om_other"})
        self.assertEqual(self.queue.get("rec_1", "v1"), original)

    def test_defer_backs_off_then_blocks_after_three_failures(self):
        self.prepared()
        first = self.queue.defer("rec_1", "v1", NOW + timedelta(minutes=2), "timeout")
        self.assertEqual(first["status"], "queued")
        self.assertEqual(first["attempts"], 1)
        first_retry = NOW + timedelta(minutes=32)
        self.assertEqual(datetime.fromisoformat(first["next_attempt_at"]), first_retry)
        self.assertEqual(self.queue.ready(first_retry - timedelta(seconds=1)), [])
        self.assertIsNone(self.queue.claim("rec_1", "v1", first_retry - timedelta(seconds=1)))
        # Repeating the same failed turn during its cooldown is not another failure.
        duplicate = self.queue.defer("rec_1", "v1", NOW + timedelta(minutes=3), "timeout")
        self.assertEqual(duplicate, first)
        self.queue.claim("rec_1", "v1", first_retry)
        second = self.queue.defer("rec_1", "v1", first_retry, "timeout")
        second_retry = first_retry + timedelta(minutes=60)
        self.assertEqual(datetime.fromisoformat(second["next_attempt_at"]), second_retry)
        self.queue.claim("rec_1", "v1", second_retry)
        last = self.queue.defer("rec_1", "v1", second_retry, "timeout")
        self.assertEqual(last["status"], "blocked")
        self.assertEqual(last["attempts"], 3)
        self.assertEqual(last["result_path"], "/drafts/weekly.md")
        self.assertEqual(last["result_sha256"], DIGEST)
        self.assertEqual(self.queue.ready(second_retry + timedelta(days=7)), [])
        self.assertEqual(self.queue.defer("rec_1", "v1", second_retry, "timeout"), last)

    def test_block_preserves_result_and_never_persists_raw_error_secret(self):
        self.prepared()
        secret = "super-private-token-123"
        result = self.queue.block("rec_1", "v1", "Authorization: Bearer " + secret)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["result_sha256"], DIGEST)
        self.assertNotIn(secret, result["reason"])
        self.assertIn("授权", result["reason"])
        persisted = " ".join(str(row) for row in self.state.db.execute("SELECT * FROM prework_queue"))
        self.assertNotIn(secret, persisted)
        self.assertEqual(self.queue.block("rec_1", "v1", "another secret"), result)

    def test_datekey_is_shanghai_and_all_persisted_timestamps_are_aware(self):
        utc = datetime(2026, 11, 15, 20, 0, tzinfo=timezone.utc)
        result = self.queue.discover([candidate()], utc)[0]
        self.assertEqual(result["datekey"], "2026-11-16")
        self.assertEqual(result["candidate"]["due_date"], "2026-11-20")
        for key in ("created_at", "updated_at", "next_attempt_at"):
            self.assertEqual(datetime.fromisoformat(result[key]).utcoffset(), timedelta(hours=8))
        self.assertEqual(len(self.queue.ready("2026-11-15T20:00:00Z")), 1)
        with self.assertRaises(MatrixError):
            self.queue.ready(datetime(2026, 11, 16, 9))
        with self.assertRaises(MatrixError):
            self.queue.discover([candidate("naive")], "2026-11-16T09:00:00")
        self.assertIsNone(self.queue.get("naive", "v1"))

    def test_ambiguous_or_invalid_batch_does_not_partially_discover(self):
        for batch in ([candidate(), candidate(version="v2")], [candidate(), {"record_id": "broken"}],
                      [candidate(), candidate("bad", policy={"value": float("nan")})]):
            with self.assertRaises(MatrixError):
                self.queue.discover(batch, NOW)
            self.assertEqual(self.queue.list(), [])

    def test_missing_record_invalid_lease_and_result_are_rejected(self):
        with self.assertRaises(MatrixError):
            self.queue.claim("missing", "v1", NOW)
        self.seed()
        for value in (0, -1, True, float("inf"), float("nan")):
            with self.assertRaises(MatrixError):
                self.queue.claim("rec_1", "v1", NOW, lease_minutes=value)
        self.queue.claim("rec_1", "v1", NOW)
        with self.assertRaises(MatrixError):
            self.queue.prepared("rec_1", "v1", "", DIGEST, NOW)
        with self.assertRaises(MatrixError):
            self.queue.prepared("rec_1", "v1", "/draft.md", "not-a-digest", NOW)
        with self.assertRaises(MatrixError):
            self.queue.prepared("rec_1", "v1", "/draft.md", DIGEST, NOW + timedelta(minutes=30))

    def test_prepared_evidence_cannot_be_silently_overwritten(self):
        first = self.prepared()
        repeated = self.queue.prepared("rec_1", "v1", "/drafts/weekly.md", DIGEST, NOW + timedelta(minutes=2))
        self.assertEqual(first, repeated)
        with self.assertRaises(MatrixError):
            self.queue.prepared("rec_1", "v1", "/different.md", DIGEST, NOW + timedelta(minutes=2))
        self.queue.claim("rec_1", "v1", NOW + timedelta(minutes=31))
        with self.assertRaises(MatrixError):
            self.queue.prepared("rec_1", "v1", "/different.md", DIGEST, NOW + timedelta(minutes=32))

    def test_stale_and_done_cannot_be_requeued_or_blocked(self):
        self.prepared()
        self.queue.done("rec_1", "v1", {"message_id": "om_1"})
        for action in (lambda: self.queue.defer("rec_1", "v1", NOW, "timeout"),
                       lambda: self.queue.block("rec_1", "v1", "等待处理")):
            with self.assertRaises(MatrixError):
                action()
        self.seed("stale")
        self.queue.discover([candidate("stale", "v2")], NOW)
        with self.assertRaises(MatrixError):
            self.queue.defer("stale", "v1", NOW, "timeout")


if __name__ == "__main__":
    unittest.main()
