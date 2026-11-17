import io
import json
from contextlib import redirect_stdout, redirect_stderr
import unittest
from unittest.mock import patch
from agent_matrix.models import Config
from agent_matrix.scheduled import main


class ScheduledTests(unittest.TestCase):
    def invoke(self, argv, code=0, data=None, enabled=True):
        def run(command):
            print(json.dumps(data or {"ok": True, "enabled_count": 0, "created_count": 0}))
            return code
        out, err = io.StringIO(), io.StringIO()
        with patch("agent_matrix.scheduled.load", return_value=Config(scheduling_enabled=enabled)), \
             patch("agent_matrix.scheduled.run_cli", side_effect=run) as cli, \
             patch("agent_matrix.activity.log"), \
             redirect_stdout(out), redirect_stderr(err):
            status = main(argv)
        return status, out.getvalue(), err.getvalue(), cli

    def test_complete_frame_only(self):
        code, output, error, cli = self.invoke(["recurring"])
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertEqual(len(output.splitlines()), 1)
        self.assertEqual(json.loads(output)["protocol"], "cindy-script/1")

    def test_preview_passed_through(self):
        _, _, _, cli = self.invoke(["evening", "--preview"])
        self.assertEqual(cli.call_args.args[0][:2], ["brief", "--evening"])
        self.assertIn("--preview", cli.call_args.args[0])

    def test_failure_has_no_complete_frame(self):
        code, output, error, _ = self.invoke(["morning"], code=1)
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertTrue(error)

    def test_master_switch_prevents_business_call(self):
        code, output, _, cli = self.invoke(["recurring"], enabled=False)
        self.assertEqual(code, 0)
        cli.assert_not_called()
        self.assertIn("已关闭", output)


if __name__ == "__main__":
    unittest.main()
