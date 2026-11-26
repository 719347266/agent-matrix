import tempfile
import unittest
from pathlib import Path

from agent_matrix import localdoc, promptfile

DOC = """# 周会议程

- 三项议题
- 共 30 分钟

## 议程

| 顺序 | 议题 |
|---|---|
| 1 | 上周进度 <b> |

正文一行，**重点**，`code`，[链接](https://example.com/a)。

## 我做的假设

1. 无
"""


class LocalDocTest(unittest.TestCase):
    def test_saved_once_per_content_and_read_back(self):
        with tempfile.TemporaryDirectory() as d:
            url = localdoc.save(d, "recA", DOC.encode(), 47800)
            self.assertEqual(url, localdoc.save(d, "recA", DOC.encode(), 47800))
            self.assertNotEqual(url, localdoc.save(d, "recA", (DOC + "改了").encode(), 47800))
            self.assertTrue(localdoc.is_local(url, 47800))
            self.assertFalse(localdoc.is_local("https://example.com/doc/x", 47800))
            rid, name = url.split("/")[-2:]
            self.assertEqual(localdoc.read(d, rid, name), DOC)

    def test_only_its_own_names_are_looked_for(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "config.yaml").write_text("secret", encoding="utf-8")
            for rid, name in (("..", "config"), ("recA", "../../config"), ("../..", "0" * 12), ("recA", "0" * 12), ("", ""), (None, None)):
                self.assertIsNone(localdoc.read(d, rid, name))

    def test_page_shows_the_document_and_escapes_it(self):
        page = localdoc.page(DOC)
        for part in ("<title>周会议程</title>", "<h1>周会议程</h1>", "<li>三项议题</li>", "<th>议题</th>", "<td>上周进度 &lt;b&gt;</td>",
                     "<b>重点</b>", "<code>code</code>", '<a href="https://example.com/a"', "<ol><li>无</li></ol>"):
            self.assertIn(part, page)
        self.assertNotIn("<script", localdoc.page("<script>alert(1)</script>\n\n```\n<script>x</script>\n```"))


class PromptFileTest(unittest.TestCase):
    NAMES = ("worker.md", "triage.md", "learn.md", "meeting_extract.md", "chat_extract.md")

    def test_a_new_data_folder_has_every_job_description(self):
        with tempfile.TemporaryDirectory() as d:  # an installed client: nothing but config and state in the data folder
            for name in self.NAMES:
                self.assertGreater(len(promptfile.read(d, name)), 200, name)

    def test_the_owners_own_wording_wins(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "prompts").mkdir()
            (Path(d) / "prompts/worker.md").write_text("我自己的写法", encoding="utf-8")
            self.assertEqual(promptfile.read(d, "worker.md"), "我自己的写法")
            self.assertGreater(len(promptfile.read(d, "triage.md")), 200)


if __name__ == "__main__":
    unittest.main()
