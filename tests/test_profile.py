"""关于你: who the owner is reaches every AI before any work."""
import tempfile
import unittest
from pathlib import Path

from agent_matrix import drive, memory


class Profile(unittest.TestCase):
    def test_written_once_read_by_every_job(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "state").mkdir()
            self.assertEqual(memory.profile_text(root), "")
            memory.set_profile(root, "岗位：观己 App 产品经理，负责会员和增长\n交付习惯：先给结论，表格优先")
            self.assertTrue((root / "state/profile.md").is_file())
            job = drive._open(root, {"id": "learn-1", "kind": "learn", "title": "t", "folder": d, "ref": {}, "output": [],
                                     "instructions": "做这件事"}, "Antigravity")
            self.assertIn("## 本人是谁、在做什么", job["instructions"])
            self.assertIn("观己 App 产品经理", job["instructions"])
            self.assertTrue(job["instructions"].endswith("做这件事"))
            memory.set_profile(root, "  ")
            self.assertEqual(memory.profile(root)["text"], "")


if __name__ == "__main__":
    unittest.main()


class Scan(unittest.TestCase):
    def test_ticked_places_become_one_job_for_the_driving_ai_and_its_draft_waits(self):
        from unittest.mock import patch
        from agent_matrix import aidrivers
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "state").mkdir()
            aidrivers.hello(root, "Antigravity")
            memory.set_profile(root, "岗位：产品经理")
            with self.assertRaises(ValueError):
                memory.ask_scan(root, ["nowhere"])
            memory.ask_scan(root, ["feishu", "folders"])
            self.assertIn("profile", drive.kinds_for(root, "Antigravity"))
            job = drive.next_job(root, None, "Antigravity", kinds=["profile"])
            self.assertEqual(job["kind"], "profile")
            for part in ("飞书近 30 天、本机项目文件夹", "lark-cli docs +search", ".git", "岗位：产品经理", "## 交付习惯", "不抄聊天原文"):
                self.assertIn(part, job["instructions"])
            self.assertNotIn("wechat-cli", job["instructions"], "only the places that were ticked")
            self.assertIsNone(drive._prepare_profile(root, None, "Antigravity"), "one at a time")
            folder = Path(job["folder"])
            (folder / "画像.md").write_text("## 岗位和职责\n太短", encoding="utf-8")
            self.assertEqual(drive.submit(root, None, job["id"])["status"], "fix")
            body = "\n".join(f"## {h}\n{h}的内容写在这里，至少有一些真实信息可以读。（飞书文档）" for h in
                             ("岗位和职责", "公司与产品", "正在推进的事", "常一起干活的人", "交付习惯", "常用工具和地方"))
            (folder / "画像.md").write_text(body, encoding="utf-8")
            with patch("agent_matrix.inbox.add"):
                self.assertEqual(drive.submit(root, None, job["id"])["status"], "done")
            self.assertIsNone(memory.scan_asked(root))
            self.assertEqual(memory.profile(root)["text"].strip(), "岗位：产品经理", "nothing changes until the owner says so")
            self.assertIn("交付习惯", memory.draft(root)["text"])
            memory.drop_draft(root, use=True)
            self.assertIn("交付习惯", memory.profile(root)["text"]); self.assertIsNone(memory.draft(root))
