"""关于你: who the owner is reaches every AI before any work."""
import json
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
            with self.assertRaises(ValueError):
                memory.ask_scan(root, ["nowhere"])
            memory.ask_scan(root, ["feishu", "folders"])
            self.assertIn("profile", drive.kinds_for(root, "Antigravity"))
            job = drive.next_job(root, None, "Antigravity", kinds=["profile"])
            self.assertEqual(job["kind"], "profile")
            for part in ("飞书近 30 天、本机项目文件夹", "lark-cli docs +search", ".git", "## 交付习惯", "不抄聊天原文"):
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
            self.assertEqual(memory.profile(root)["text"].strip(), "", "nothing changes until the owner says so")
            self.assertIn("交付习惯", memory.draft(root)["text"])
            memory.drop_draft(root, use=True)
            self.assertIn("交付习惯", memory.profile(root)["text"]); self.assertIsNone(memory.draft(root))

    def test_confirmed_profile_only_changes_at_a_reviewed_anchor(self):
        from agent_matrix import tidy
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            original = "## 职责\n负责产品\n\n## 交付\n先给结论"
            memory.set_profile(root, original)
            memory.save_draft(root, "老草稿")
            with self.assertRaises(ValueError):
                memory.drop_draft(root, use=True)
            def scan(changes):
                memory.ask_scan(root, ["folders"])
                req = memory.scan_asked(root)
                memory.ask_scan(root, ["folders"])
                self.assertEqual(req, memory.scan_asked(root))
                job = drive.next_job(root, None, "测试", kinds=["profile"])
                self.assertEqual(job["output"], ["profile-updates.json"])
                self.assertNotIn("lark-cli", job["instructions"])
                (Path(job["folder"]) / job["output"][0]).write_text(json.dumps({"changes": changes}))
                with patch("agent_matrix.inbox.add") as notify:
                    out = drive.submit(root, None, job["id"])
                    self.assertEqual(notify.called, bool(out.get("proposed")))
                return out
            change = {"type": "profile", "old": "负责产品", "new": "负责产品和定价", "why": "本周交接了定价职责"}
            self.assertEqual(scan([])["proposed"], 0)
            self.assertEqual(scan([change])["proposed"], 1)
            self.assertEqual(scan([change])["proposed"], 0)
            self.assertEqual(scan([dict(change, new="负责产品和增长")])["proposed"], 1)
            self.assertEqual(memory.profile(root)["text"].strip(), original)
            [pending] = tidy.updates(root)
            self.assertEqual(tidy.use(root, [pending["id"]])[1], {})
            self.assertEqual(memory.profile(root)["text"].strip(), original.replace("负责产品", "负责产品和增长"))
            self.assertEqual(memory.draft(root)["text"].strip(), "老草稿")
