import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_matrix import procs

SRC = Path(__file__).resolve().parents[1] / "src/agent_matrix"


class Procs(unittest.TestCase):
    def test_a_running_process_is_alive_and_a_finished_one_is_not(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.assertTrue(procs.alive(child.pid))
            self.assertTrue(procs.alive(child.pid), "asking twice does not end it")
            self.assertIsNone(child.poll())
        finally:
            child.kill(); child.wait()
        self.assertFalse(procs.alive(child.pid))
        for nothing in (None, "", "x", 0, 1, -5):
            self.assertFalse(procs.alive(nothing))

    def test_windows_never_signals_the_process_it_asks_about(self):
        with patch.object(procs.os, "name", "nt"), patch.object(procs, "_alive_windows", return_value=True) as ask, \
             patch.object(procs.os, "kill", side_effect=AssertionError("os.kill on Windows ends the process")):
            self.assertTrue(procs.alive(1234))
        ask.assert_called_once_with(1234)

    def test_no_code_probes_a_process_with_os_kill_zero(self):
        for path in SRC.rglob("*.py"):
            if path.name == "procs.py":
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"os\.kill\([^)]*,\s*0\)", f"{path.name}: use procs.alive (os.kill(pid, 0) kills on Windows)")


if __name__ == "__main__":
    unittest.main()


class FeishuTaskList(unittest.TestCase):
    def test_an_empty_answer_is_no_tasks_not_an_error(self):
        from agent_matrix.sources import feishu_tasks
        from datetime import datetime
        for answer, n in (({}, 0), ({"has_more": False}, 0), ({"items": [{"guid": "a"}]}, 1), ({"tasks": [{"guid": "a"}]}, 1),
                          ({"data": {"items": [{"guid": "a"}, {"guid": "b"}]}}, 2), ([{"guid": "a"}], 1)):
            client = type("C", (), {"_exec": lambda self, args, a=answer: a})()
            self.assertEqual(len(feishu_tasks._list(client, complete=False, since=datetime(2026, 11, 1))), n, answer)
