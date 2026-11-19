"""Exercise the same public job/session data used by the sidebar and item detail."""
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agent_matrix import app, drive, local_sessions, sessions, thread
from agent_matrix.models import Config, MatrixError


class SessionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config = Config()

    def job(self, jid, driver="Cindy", lease=20):
        return drive._open(self.root, {"id": jid, "kind": "item", "n": 12, "ref": {"record_id": "rec12"},
                          "folder": str(self.root), "lease_until": (drive.now()+timedelta(minutes=lease)).isoformat(),
                          "instructions": "", "output": []}, driver)

    def test_bound_conversation_in_live_sidebar_and_history(self):
        job = self.job("task-a")
        url = "https://example.org/conversations/one"
        sessions.bind(self.root, job["id"], url)
        state = app.build_state(self.root)
        self.assertEqual([(s["n"], s["link"], s["can_open"]) for s in state["running"]], [(12, url, True)])
        self.assertEqual(sessions.by_item(self.root)["rec12"][0]["link"], url)
        self.assertEqual(local_sessions.open_session(self.root, self.config, job["id"]), {"link": url})

    def test_expired_lease_disappears_but_keeps_its_original_link(self):
        job = self.job("expired", lease=-3)
        sessions.bind(self.root, job["id"], "https://example.org/c/12")
        self.assertEqual(sessions.running(self.root), [])
        history = sessions.by_item(self.root)["rec12"]
        self.assertEqual(history[0]["status"], "expired")
        self.assertTrue(history[0]["can_open"])

    def test_local_resume_reports_launch_failure(self):
        job = self.job("claude-job", "本机 Claude")
        sessions.bind(self.root, job["id"], "demo-123")
        with patch("agent_matrix.workenv.claude_binary", return_value="/usr/bin/claude"), patch(
            "agent_matrix.sessions.subprocess.run", side_effect=OSError("Terminal unavailable")):
            with self.assertRaises(MatrixError):
                local_sessions.open_session(self.root, self.config, job["id"])

    def test_private_notes_visible_in_item_but_omitted_from_ai_context(self):
        thread.add(self.root, "rec12", "me", "feedback", "这条发给 AI")
        thread.add(self.root, "rec12", "me", "note", "只给我看的普通备注")
        thread.closed(self.root, "rec12", "你", "标记完成", "这段也是私人的")
        thread.add(self.root, "rec12", "system", "closed", "旧版完成备注：私有内容")
        result = drive._history_text(self.root, "rec12", limit=1)
        self.assertIn("这条发给 AI", result)
        for secret in ("普通备注", "这段也是私人的", "旧版完成备注"):
            self.assertNotIn(secret, result)
        audit = thread.closures(self.root)["rec12"]
        self.assertEqual((audit["by"], audit["note"]), ("你", "这段也是私人的"))
        self.assertTrue(audit["at"])
        self.assertTrue(any(m["kind"]=="note" for m in thread.history(self.root)["rec12"]["messages"]))
