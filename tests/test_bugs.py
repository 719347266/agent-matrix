"""Problems written down while using 心响: kept with what the program knew, handed in together, nothing private."""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from agent_matrix import activity, bugs, thread, update
from agent_matrix.timeutil import TZ


class BugTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "state").mkdir()
        (self.root / "logs").mkdir()

    def test_a_problem_is_kept_with_what_the_program_knew_and_handed_in_together(self):
        activity.log(self.root, "error", "后台定时任务出错：minute", detail="prep: FeishuError 飞书调用失败（99991663）", status="failed")
        activity.log(self.root, "detect", "聊天里发现 2 条可能跟你有关的话")
        (self.root / "logs/listen.log").write_text("11-20 15:14:01 timer snapshot → {}\n11-20 15:14:02 timer failed minute prep: FeishuError x\n", encoding="utf-8")
        self.assertIsNone(bugs.add(self.root, "   "))
        first = bugs.add(self.root, "窗口打开太窄，设置页挤成竖排")
        bugs.add(self.root, "点更新后一直显示正在更新\n等了十分钟")
        listed = bugs.all(self.root)
        self.assertEqual([b["text"].splitlines()[0] for b in listed], ["点更新后一直显示正在更新", "窗口打开太窄，设置页挤成竖排"])
        said = bugs.report(self.root)
        for words in ("心响问题反馈（2 条）", "## 1. 窗口打开太窄", "## 2. 点更新后", "等了十分钟", "后台定时任务出错：minute", "99991663", "timer failed minute"):
            self.assertIn(words, said)
        self.assertNotIn("聊天里发现", said)                       # only its own errors, nothing about what was read
        self.assertNotIn("timer snapshot", said)
        link = urlparse(bugs.issue_url(self.root))
        self.assertEqual((link.netloc, link.path), ("github.com", "/MILESMAKMAK/agent-matrix/issues/new"))
        self.assertIn("问题反馈：2 条", parse_qs(link.query)["title"][0])
        self.assertLessEqual(len(parse_qs(link.query)["body"][0]), 5600)
        bugs.mark_sent(self.root)
        self.assertEqual({b["status"] for b in bugs.all(self.root)}, {"sent"})
        self.assertIn("问题反馈（0 条）", bugs.report(self.root))
        bugs.remove(self.root, first)
        self.assertEqual(len(bugs.all(self.root)), 1)

    def test_no_home_folder_names_and_nothing_that_looks_like_a_key(self):
        home = str(Path.home())
        said = bugs.scrub(f"读不到 {home}/心响/config.yaml；C:\\Users\\王小明\\AppData 里 token=abcdEFGH1234 和 xoxp-12345-abcdef，/Users/wang/x")
        for secret in (home, "王小明", "abcdEFGH1234", "xoxp-12345", "/Users/wang"):
            self.assertNotIn(secret, said)
        self.assertIn("config.yaml", said)
        activity.log(self.root, "error", "出错", detail=f"open('{home}/a') 失败 sk-abcdefghijkl", status="failed")
        bugs.add(self.root, "读不到文件")
        self.assertNotIn(home, bugs.report(self.root)); self.assertNotIn("sk-abcdefghijkl", bugs.report(self.root))

    def test_an_update_that_never_landed_says_so(self):
        latest = {"version": "9.9.9", "page": "p", "setup": "https://example.invalid/s.exe", "notes": ""}
        with patch.object(update, "fetch_latest", return_value=latest), patch.object(update, "current", return_value="0.4.2"), \
             patch.object(update, "how", return_value=("client", True, "")):
            update.check(self.root)
            thread.set_setting(self.root, "update:doing", {"step": "正在安装，心响会自己重新打开…", "error": "", "at": datetime.now(TZ).isoformat()})
            self.assertEqual(update.status(self.root)["doing"], "正在安装，心响会自己重新打开…")
            thread.set_setting(self.root, "update:doing", {"step": "正在安装，心响会自己重新打开…", "error": "",
                                                           "at": (datetime.now(TZ) - timedelta(minutes=10)).isoformat()})
            st = update.status(self.root)
            self.assertEqual(st["doing"], ""); self.assertIn("更新没有完成", st["failed"])


if __name__ == "__main__":
    unittest.main()
