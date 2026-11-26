"""The owner tidies deliverables by hand, and picks an app for one item without changing the usual one."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_matrix import deliverables, itemstore, local_sessions, manual, thread
from agent_matrix.config import save
from agent_matrix.models import Config, MatrixError
from agent_matrix.values import draft_url


class DeliverableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        save(self.root / "config.yaml", Config(item_store="device", worker_enabled=False, intake_mode="manual"))
        made = manual.add(self.root, "写复盘", who="我")
        self.n, self.rid = made["n"], made["record_id"]

    def link(self):
        return draft_url(next(r for r in itemstore.rows(self.root) if r["record_id"] == self.rid).get("交付物链接"))

    def versions(self):
        return [(v["v"], v["summary"], v["url"]) for v in (thread.history(self.root).get(self.rid) or {}).get("deliverables") or []]

    def test_add_correct_and_remove_and_the_item_follows_the_newest(self):
        out = deliverables.change(self.root, self.n, "add", url="https://example.com/docx/A", summary=" AI 写的  初稿 ")
        self.assertEqual(out, {"versions": 1, "url": "https://example.com/docx/A"})
        self.assertEqual(self.link(), "https://example.com/docx/A")
        deliverables.change(self.root, self.n, "add", url="https://example.com/docx/B")
        self.assertEqual(self.versions(), [(1, "AI 写的 初稿", "https://example.com/docx/A"), (2, "你加的交付", "https://example.com/docx/B")])
        deliverables.change(self.root, self.n, "edit", version=2, url="https://example.com/docx/MINE", summary="我自己建的文档")
        self.assertEqual(self.versions()[1], (2, "我自己建的文档", "https://example.com/docx/MINE"))
        self.assertEqual(self.link(), "https://example.com/docx/MINE")       # the item points at the newest version
        deliverables.change(self.root, self.n, "edit", version=1, summary="旧稿")   # wording only: the link stays
        self.assertEqual(self.versions()[0], (1, "旧稿", "https://example.com/docx/A"))
        deliverables.change(self.root, self.n, "remove", version=2)
        self.assertEqual(self.link(), "https://example.com/docx/A")
        out = deliverables.change(self.root, self.n, "remove", version=1)
        self.assertEqual((out["versions"], self.link()), (0, None))          # nothing left: the item has no link either

    def test_a_link_from_before_versions_were_kept_is_v1_and_is_not_lost(self):
        from agent_matrix.feishu import Feishu
        from agent_matrix.snapshot import save_snapshot
        from agent_matrix.state import State, exclusive_lock
        from agent_matrix.workflow import Gateway
        from agent_matrix.config import load
        with exclusive_lock(self.root / "state", wait=30):
            state = State(self.root / "state/state.db")
            try:
                gateway = Gateway(Feishu(load(self.root / "config.yaml"), self.root), state, on_rows=lambda rows: save_snapshot(self.root, rows))
                row = next(r for r in gateway.rows() if r["record_id"] == self.rid)
                gateway.update(row, {"交付物链接": "https://example.com/docx/OLD", "预做结果摘要": "早先 AI 交的"}, "seed-old-link", "test")
            finally:
                state.close()
        self.assertEqual(self.versions(), [])                                 # nothing kept as a version yet
        deliverables.change(self.root, self.n, "add", url="https://example.com/docx/MINE", summary="我自己建的")
        self.assertEqual(self.versions(), [(1, "早先 AI 交的", "https://example.com/docx/OLD"), (2, "我自己建的", "https://example.com/docx/MINE")])
        self.assertEqual(self.link(), "https://example.com/docx/MINE")

    def test_what_is_not_a_link_or_not_a_version_changes_nothing(self):
        for bad in (dict(op="add", url="文档在这"), dict(op="edit", version=7, url="https://example.com/x"), dict(op="remove", version=7), dict(op="rename")):
            with self.assertRaises(MatrixError):
                deliverables.change(self.root, self.n, **bad)
        with self.assertRaises(MatrixError):
            deliverables.change(self.root, 999, "add", url="https://example.com/x")
        self.assertEqual((self.versions(), self.link()), ([], None))

    def test_an_app_for_this_one_item_leaves_the_usual_choice_alone(self):
        thread.set_setting(self.root, "pickup_app", "cindy")
        found = {"cindy": True, "claude": True, "terminal-claude": False, "terminal-codex": True, "terminal-gemini": False}
        seen = []
        with patch.object(local_sessions, "apps", return_value=found), \
             patch.object(local_sessions, "start_in_cindy", side_effect=lambda *a, **k: seen.append("cindy") or {"opened": "cindy"}), \
             patch.object(local_sessions, "start_for_item", side_effect=lambda *a, **k: seen.append("claude") or {"opened": "claude"}), \
             patch.object(local_sessions, "start_in_terminal", side_effect=lambda *a, cli="", **k: seen.append("terminal-" + cli) or {"opened": "terminal"}):
            local_sessions.pick_up(self.root, Config(), {"n": 1})
            local_sessions.pick_up(self.root, Config(), {"n": 1}, where="claude")
            local_sessions.pick_up(self.root, Config(), {"n": 1}, where="terminal-codex")
            local_sessions.pick_up(self.root, Config(), {"n": 1})
            with self.assertRaises(MatrixError):
                local_sessions.pick_up(self.root, Config(), {"n": 1}, where="terminal-gemini")   # not on this computer
            with self.assertRaises(MatrixError):
                local_sessions.pick_up(self.root, Config(), {"n": 1}, where="notepad")
        self.assertEqual(seen, ["cindy", "claude", "terminal-codex", "cindy"])
        self.assertEqual(thread.setting(self.root, "pickup_app", None), "cindy")


if __name__ == "__main__":
    unittest.main()
