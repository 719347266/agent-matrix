"""Projects: the owner sorts items; what the owner has not placed is guessed, and a guess is never written."""
import tempfile
import unittest
from pathlib import Path

from agent_matrix import itemlinks, projects
from agent_matrix.models import MatrixError

ROWS = [{"record_id": "r1", "标题": "撰写标签体系需求文档"}, {"record_id": "r2", "标题": "标签更新：加心理、使用行为两大类"},
        {"record_id": "r3", "标题": "付费点和付费归因怎么分析"}, {"record_id": "r4", "标题": "发布岗位招聘信息"},
        {"record_id": "r5", "标题": "标签更新：加使用行为类"}]


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "state").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_nothing_is_sorted_until_there_is_a_project(self):
        self.assertEqual(projects.all(self.root), [])
        self.assertEqual(projects.place(self.root, ROWS), {})

    def test_the_owner_places_items_and_the_rest_is_guessed(self):
        tag = projects.create(self.root, "标签体系")
        hire = projects.create(self.root, "招人")
        self.assertEqual([p["name"] for p in projects.all(self.root)], ["标签体系", "招人"])
        self.assertNotEqual(*[p["color"] for p in projects.all(self.root)])
        projects.assign(self.root, ["r2"], tag)
        itemlinks.link(self.root, "r2", "r3")                    # r3 continues r2: it follows it into the project
        placed = projects.place(self.root, ROWS)
        self.assertEqual(placed["r2"], (tag, projects.OWNER))
        self.assertEqual(placed["r1"], (tag, projects.GUESS))    # the project's name is in the title
        self.assertEqual(placed["r3"], (tag, projects.GUESS))
        self.assertEqual(placed["r5"], (tag, projects.GUESS))    # reads like an item the owner already put there
        self.assertNotIn("r4", placed)                           # nothing says where it belongs
        self.assertEqual(projects.chosen(self.root), {"r2": tag})  # guesses are not written
        projects.assign(self.root, ["r4"], hire)
        projects.assign(self.root, ["r3"], 0)                    # "none" is a choice too: not guessed over
        placed = projects.place(self.root, ROWS)
        self.assertEqual(placed["r4"], (hire, projects.OWNER))
        self.assertNotIn("r3", placed)

    def test_rename_remove_and_bad_input(self):
        a = projects.create(self.root, "  运营  复盘 ")
        self.assertEqual(projects.all(self.root)[0]["name"], "运营 复盘")
        self.assertEqual(projects.create(self.root, "运营 复盘"), a)          # the same name is the same project
        b = projects.create(self.root, "2.3 版本")
        with self.assertRaises(MatrixError):
            projects.rename(self.root, b, "运营 复盘")
        with self.assertRaises(MatrixError):
            projects.create(self.root, "   ")
        with self.assertRaises(MatrixError):
            projects.assign(self.root, ["r1"], 999)
        projects.rename(self.root, b, "2.4 版本")
        projects.assign(self.root, ["r1", "r2"], b)
        projects.remove(self.root, b)
        self.assertEqual([p["name"] for p in projects.all(self.root)], ["运营 复盘"])
        self.assertEqual(projects.place(self.root, ROWS), {})                # its items are unsorted again
        self.assertEqual(projects.chosen(self.root), {})


if __name__ == "__main__":
    unittest.main()
