import tempfile
import unittest
from pathlib import Path

from agent_matrix import db, lifecycle, schema
from agent_matrix.brief_migration import STATUS_MAP


class LifecycleTest(unittest.TestCase):
    def test_the_table_covers_exactly_the_statuses_the_item_table_has(self):
        self.assertEqual(lifecycle.STATUSES, schema.STATUSES)
        self.assertEqual(set(lifecycle.ALLOWED), set(lifecycle.STATUSES))
        self.assertEqual(lifecycle.OPEN | lifecycle.CLOSED, set(lifecycle.STATUSES))
        for targets in lifecycle.ALLOWED.values():
            self.assertLessEqual(targets, set(lifecycle.STATUSES))

    def test_old_wording_maps_the_same_way_as_the_migration(self):
        for old, new in STATUS_MAP.items():
            self.assertEqual(lifecycle.canonical(old), new, old)
        self.assertEqual(lifecycle.canonical(["草稿已备"]), "待验收")
        self.assertIsNone(lifecycle.canonical("随便写的"))
        self.assertIsNone(lifecycle.canonical([]))

    def test_every_open_status_can_reach_done_and_cancelled(self):
        for status in lifecycle.OPEN:
            self.assertLessEqual(lifecycle.CLOSED, lifecycle.ALLOWED[status], status)

    def test_normal_work_is_allowed(self):
        for old, new in [("待确认", "未开始"), ("未开始", "进行中"), ("进行中", "待验收"), ("待验收", "完成"),
                         ("待验收", "进行中"), ("卡住", "进行中"), ("完成", "进行中"), ("取消", "待确认"),
                         ("待办", "草稿已备"), (None, "待验收"), ("完成", "完成"), ("进行中", None)]:
            self.assertIsNone(lifecycle.problem(old, new), (old, new))

    def test_finished_or_dropped_work_is_not_overwritten_by_a_late_result(self):
        self.assertIn("不能直接变成", lifecycle.problem("完成", "待验收"))
        self.assertIn("不能直接变成", lifecycle.problem(["取消"], ["待验收"]))
        self.assertIn("不能直接变成", lifecycle.problem("取消", "完成"))
        self.assertIn("不在状态表里", lifecycle.problem("进行中", "搞定了"))


class GatewayObservesTest(unittest.TestCase):
    """An unlisted change is still written today, and leaves one line in the activity log."""

    def setUp(self):
        from agent_matrix import workflow
        self.workflow = workflow
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def events(self):
        conn = db.open(self.root, "activity")
        try:
            return conn.execute("SELECT kind, status, title, ref FROM events").fetchall()
        finally:
            conn.close()

    def test_unlisted_change_is_recorded_not_blocked(self):
        self.workflow.note_transition(self.root, {"record_id": "rec1", "编号": 7, "状态": ["完成"]},
                                      {"状态": ["待验收"]}, "prework")
        self.assertEqual(self.events(), [("lifecycle", "observed", "#7 「完成」不能直接变成「待验收」（来自 prework）", "rec1")])

    def test_listed_change_and_other_fields_leave_no_trace(self):
        self.workflow.note_transition(self.root, {"record_id": "rec1", "状态": ["进行中"]}, {"状态": ["待验收"]}, "x")
        self.workflow.note_transition(self.root, {"record_id": "rec1", "状态": ["完成"]}, {"进度": "完成"}, "x")
        self.assertEqual(self.events(), [])

    def test_enforcing_refuses_the_write(self):
        from agent_matrix.models import MatrixError
        lifecycle.ENFORCE = True
        self.addCleanup(setattr, lifecycle, "ENFORCE", False)
        with self.assertRaises(MatrixError):
            self.workflow.note_transition(self.root, {"record_id": "rec1", "状态": ["取消"]}, {"状态": ["完成"]}, "x")


if __name__ == "__main__":
    unittest.main()
