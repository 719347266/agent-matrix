"""Keeps the structure from getting worse. Layers and the scan live in tools/archmap.py.

The lists below are what is still wrong today. They may only shrink: fix one, delete its line.
Adding a line here needs a reason in the commit message.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import archmap  # noqa: E402

# Imports that still reach into a higher layer: (from, to).
KNOWN_UPWARD = {
    ("drive", "monitor"), ("itemstore", "bitable"), ("itemstore", "feishu"), ("itemstore", "records"),
    ("itemstore", "workflow"), ("linked_sync", "automation"), ("local_sessions", "monitor"),
    ("memory", "meeting_review"), ("memory", "merge"), ("notion", "workflow"), ("prework_store", "delivery"),
    ("proactive", "drive"), ("reply", "drive"), ("reply", "monitor"), ("reply", "worker"),
    ("sources.feishu_tasks", "brief_migration"), ("sources.feishu_tasks", "merge"),
    ("sources.feishu_tasks", "workflow"),
}
# Modules still inside an import cycle.
KNOWN_CYCLIC = {
    "agents.daily_brief", "agents.pre_worker", "bitable", "brief_migration", "brief_v1", "feishu", "itemstore",
    "prework", "records", "workflow", "chat_review", "drive", "local_sessions", "monitor", "proactive", "reply",
    "sessions", "worker", "meeting_review", "memory",
}
# Modules that still open SQLite themselves instead of going through db.py.
KNOWN_CONNECTS = set()


class ArchitectureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = archmap.report()

    def test_every_module_has_a_layer(self):
        self.assertEqual(self.report["unplaced"], [], "新模块要先在 tools/archmap.py 的 LAYERS 里归层")

    def test_no_new_upward_imports(self):
        found = {(src, dst) for src, dst, _ in self.report["upward"]}
        self.assertEqual(sorted(found - KNOWN_UPWARD), [], "下层模块不能引用上层模块")
        self.assertEqual(sorted(KNOWN_UPWARD - found), [], "已修好的向上引用请从 KNOWN_UPWARD 删除")

    def test_no_new_cycles(self):
        found = {m for group in self.report["cycles"] for m in group}
        self.assertEqual(sorted(found - KNOWN_CYCLIC), [], "出现了新的循环依赖")
        self.assertEqual(sorted(KNOWN_CYCLIC - found), [], "已解开的循环请从 KNOWN_CYCLIC 删除")

    def test_database_opened_in_one_place(self):
        found = set(self.report["connects"])
        self.assertEqual(sorted(found - KNOWN_CONNECTS), [], "数据库连接统一走 db.py")
        self.assertEqual(sorted(KNOWN_CONNECTS - found), [], "已改走 db.py 的模块请从 KNOWN_CONNECTS 删除")


if __name__ == "__main__":
    unittest.main()
