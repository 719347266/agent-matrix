"""One broken part never blanks the page: the rest still opens, and the broken part says what went wrong."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_matrix import app, winclient

ROWS = [{"record_id": "rec1", "编号": 1, "标题": "写周报", "状态": ["进行中"], "执行方": ["我"]},
        {"record_id": "rec2", "编号": 2, "标题": "坏掉的一条", "状态": ["进行中"], "原话引用": "BAD"},
        {"record_id": "rec3", "编号": 3, "标题": "定发布日期", "状态": ["待确认"]}]


class PageData(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "state").mkdir()
        winclient.ensure_config(self.root)   # a first start, like the Windows client's
        patch("agent_matrix.snapshot._snapshot", return_value={"rows": ROWS}).start()
        self.addCleanup(patch.stopall)

    def test_a_part_that_fails_is_named_and_the_rest_still_shows(self):
        with patch("agent_matrix.local_sessions.view", side_effect=OSError("no such folder")):
            state = app.build_state(self.root)
        self.assertEqual([i["n"] for i in state["items"]], [1, 2, 3])
        self.assertIn("本机 AI 会话", state["broken"])
        self.assertIn("no such folder", state["broken"]["本机 AI 会话"])
        self.assertEqual(state["local_sessions"], [])

    def test_one_broken_item_is_left_out_and_the_others_stay(self):
        real = app._clean
        with patch.object(app, "_clean", side_effect=lambda v: (_ for _ in ()).throw(ValueError("odd quote")) if v == "BAD" else real(v)):
            state = app.build_state(self.root)
        self.assertEqual([i["n"] for i in state["items"]], [1, 3])
        self.assertIn("事项 #2", state["broken"])

    def test_a_healthy_page_has_nothing_broken(self):
        self.assertEqual(app.build_state(self.root)["broken"], {})


if __name__ == "__main__":
    unittest.main()
