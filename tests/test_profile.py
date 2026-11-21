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
