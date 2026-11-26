"""Updating with one click: which version is newer, who may update themselves, and what the page is told."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_matrix import update


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "state").mkdir()

    def test_which_version_is_newer(self):
        self.assertTrue(update.newer("0.4.1", "0.4.0"))
        self.assertTrue(update.newer("v0.10.0", "0.9.9"))
        self.assertFalse(update.newer("0.4.0", "0.4.0"))
        self.assertFalse(update.newer("", "0.4.0"))
        self.assertFalse(update.newer("0.3.9", "0.4.0"))

    def test_the_releases_page_is_asked_now_and_then_and_a_failure_is_only_noted(self):
        latest = {"version": "9.9.9", "page": "https://example.invalid/r", "setup": "https://example.invalid/Xinxiang-Setup-9.9.9.exe", "notes": ""}
        with patch.object(update, "fetch_latest", return_value=latest) as fetch:
            self.assertEqual(update.check(self.root)["version"], "9.9.9")
            update.check(self.root)                       # fresh: not asked again
            self.assertEqual(fetch.call_count, 1)
            update.check(self.root, force=True)
            self.assertEqual(fetch.call_count, 2)
        with patch.object(update, "fetch_latest", side_effect=OSError("offline")):
            seen = update.check(self.root, force=True)
        self.assertEqual(seen["version"], "9.9.9")        # what was known stays
        self.assertIn("连不上发布页", seen["error"])

    def test_the_page_is_told_whether_one_click_can_do_it(self):
        latest = {"version": "9.9.9", "page": "p", "setup": "https://example.invalid/s.exe", "notes": ""}
        with patch.object(update, "fetch_latest", return_value=latest), patch.object(update, "current", return_value="0.4.0"):
            update.check(self.root)
            with patch.object(update, "how", return_value=("client", True, "")):
                st = update.status(self.root)
                self.assertEqual((st["newer"], st["can"], st["latest"], st["current"]), (True, True, "9.9.9", "0.4.0"))
            with patch.object(update, "how", return_value=("git", False, "这份是开发中的版本")):
                st = update.status(self.root)
                self.assertEqual((st["newer"], st["can"]), (True, False))
                self.assertEqual(update.start(self.root)["ok"], False)      # a copy under development is left alone
        with patch.object(update, "fetch_latest", return_value={**latest, "version": "0.4.0"}), patch.object(update, "current", return_value="0.4.0"), \
             patch.object(update, "how", return_value=("client", True, "")):
            self.assertIn("已经是最新", update.start(self.root)["reply"])

    def test_this_checkout_is_a_copy_under_development_or_the_release_branch(self):
        kind, can, why = update.how(self.root)
        self.assertIn(kind, {"git", "other"})
        if kind == "git" and not can:
            self.assertTrue(why)

    def test_a_failed_update_says_so_and_a_landed_one_clears_the_note(self):
        latest = {"version": "9.9.9", "page": "p", "setup": "https://example.invalid/s.exe", "notes": ""}
        with patch.object(update, "fetch_latest", return_value=latest), patch.object(update, "current", return_value="0.4.0"), \
             patch.object(update, "how", return_value=("client", True, "")), patch.object(update, "_update_client", side_effect=OSError("下载下来的安装包不完整")):
            self.assertTrue(update.start(self.root)["ok"])
            for _ in range(50):
                if update.status(self.root)["failed"]:
                    break
                __import__("time").sleep(0.05)
            self.assertIn("安装包不完整", update.status(self.root)["failed"])
        update._say(self.root, "正在安装，心响会自己重新打开…")
        with patch.object(update, "current", return_value="9.9.9"):
            update.check = update.check   # the newest release is the one now running
            update.settle(self.root)
            self.assertEqual(update.status(self.root)["doing"], "")


if __name__ == "__main__":
    unittest.main()
