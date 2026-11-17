from contextlib import redirect_stdout, redirect_stderr
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from agent_matrix.cli import main
from agent_matrix.config import save
from agent_matrix.models import Config
from agent_matrix.feishu import FeishuError
from agent_matrix.state import State


class PreviewBoundaryTests(unittest.TestCase):
    def test_authorization_failure_in_preview_never_sends_alert(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            save(root / "config.yaml", Config(owner_open_id="ou_test"))
            client = Mock()
            client.verify_owner.side_effect = FeishuError("user_needs_refresh", authorization=True)
            with patch("agent_matrix.cli.Feishu", return_value=client), redirect_stderr(io.StringIO()):
                self.assertEqual(main(["brief", "--preview", "--config", str(root / "config.yaml")]), 1)
            client.send_self.assert_not_called()
            state = State(root / "state/state.db")
            self.assertEqual(state.pending(), [])
            state.close()

    def test_brief_preview_selects_edition_without_ping(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            save(root / "config.yaml", Config(base_token="base_test", owner_open_id="ou_test"))
            state = State(root / "state/state.db")
            state.set("owned_base", "base_test"); state.set("setup_complete", True); state.close()
            client = Mock()
            with patch("agent_matrix.cli.Feishu", return_value=client), \
                 patch("agent_matrix.agents.daily_brief.run", return_value={"preview": True, "sent": False}) as run, \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(main(["brief", "--evening", "--preview", "--config", str(root / "config.yaml")]), 0)
            self.assertEqual(run.call_args.kwargs, {"edition": "evening", "preview": True})
            client.send_self.assert_not_called()


if __name__ == "__main__":
    unittest.main()
