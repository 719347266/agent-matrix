"""Fault injection with real CLI orchestration, isolated SQLite, no network."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent_matrix.cli import main
from agent_matrix.config import save
from agent_matrix.feishu import Feishu, FeishuError
from agent_matrix.alerts import alert_key, clear_recovered
from agent_matrix.models import Config, MatrixError
from agent_matrix.state import State, exclusive_lock


class FaultRunner:
    def __init__(self, config):
        self.config = config
        self.calendar_denied = True
        self.user_ready = True
        self.missing_receipt = False
        self.sent_keys = []
        self.error_code = "calendar_scope_missing"
        self.before_send = None

    def __call__(self, argv, **kwargs):
        domain, command = argv[1:3]
        if domain == "auth":
            body = {"appId": self.config.app_id, "identities": {
                "user": {"status": "ready" if self.user_ready else "needs_refresh",
                         "available": True, "verified": self.user_ready,
                         "openId": self.config.owner_open_id},
                "bot": {"status": "ready", "available": True, "verified": True}}}
        elif domain == "calendar":
            if self.calendar_denied:
                error = {"ok": False, "error": {"type": "authorization", "code": self.error_code}}
                return SimpleNamespace(returncode=1, stdout="", stderr=json.dumps(error))
            body = {"ok": True, "data": []}
        elif domain == "im":
            if "--dry-run" in argv:
                body = {"ok": True, "data": {}}
            else:
                if self.before_send:
                    self.before_send()
                self.sent_keys.append(argv[argv.index("--idempotency-key") + 1])
                body = {"ok": True, "data": {} if self.missing_receipt else {"message_id": f"om_{len(self.sent_keys)}"}}
        elif command == "+base-get":
            body = {"ok": True, "data": {}}
        elif command == "+table-list":
            body = {"ok": True, "data": {"items": [{"table_id": v} for v in self.config.tables.values()]}}
        else:
            raise AssertionError(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps(body), stderr="")


class AuthorizationAlerts(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = Config(app_id="cli_fixture", owner_open_id="ou_fixture_owner", base_token="base_fixture", notify_channel="feishu",
                             tables={"事项表": "tbl_a", "周期规则表": "tbl_b", "会议处理记录表": "tbl_c", "运行日志表": "tbl_d"})
        save(self.root / "config.yaml", self.config)
        self.state = State(self.root / "state/state.db"); self.addCleanup(self.state.close)
        self.state.set("owned_base", self.config.base_token); self.state.set("setup_complete", True)
        self.runner = FaultRunner(self.config)
        self.addCleanup(patch.stopall)
        patch("agent_matrix.cli.Feishu", side_effect=lambda c, root: Feishu(c, root, self.runner)).start()
        patch("agent_matrix.records.read_records", return_value=[]).start()

    def invoke(self, *args):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = main([*args, "--config", str(self.root / "config.yaml")])
        return code, json.loads(error.getvalue() or output.getvalue())

    def test_repeated_calendar_permission_failure_sends_one_alert(self):
        self.assertEqual(self.invoke("brief", "--morning")[0], 1)
        self.assertEqual(self.invoke("brief", "--evening")[0], 1)
        self.assertEqual(len(self.runner.sent_keys), 1)

    def test_health_success_does_not_reset_calendar_incident(self):
        self.invoke("brief")
        self.invoke("health")
        self.invoke("brief")
        self.assertEqual(len(self.runner.sent_keys), 1)

    def test_real_calendar_recovery_allows_one_new_outage_alert(self):
        self.invoke("brief")
        self.runner.calendar_denied = False
        self.assertEqual(self.invoke("brief", "--preview")[0], 0)
        self.runner.calendar_denied = True
        self.invoke("brief")
        self.invoke("brief")
        self.assertEqual(len(self.runner.sent_keys), 2)
        self.assertNotEqual(*self.runner.sent_keys)

    def test_cached_brief_does_not_prove_calendar_recovered(self):
        from datetime import datetime
        from agent_matrix.timeutil import TZ
        self.invoke("brief")
        key = f"brief:morning:{datetime.now(TZ).date().isoformat()}"
        self.state.once(key, lambda: {"message_id": "om_previous"})
        self.assertEqual(self.invoke("brief")[0], 0)
        self.invoke("brief", "--evening")
        self.assertEqual(len(self.runner.sent_keys), 1)

    def test_missing_alert_receipt_is_pending_and_not_claimed_or_resent(self):
        self.runner.user_ready = False
        self.runner.missing_receipt = True
        for _ in range(2):
            code, result = self.invoke("brief")
            self.assertEqual(code, 1)
            self.assertNotEqual(result.get("owner_alert"), "已通知或已有通知")
        self.assertEqual(len(self.runner.sent_keys), 1)
        key = self.runner.sent_keys[0]
        self.assertEqual(self.state.operation(key)["status"], "pending")

    def test_ping_missing_receipt_is_pending(self):
        self.runner.missing_receipt = True
        self.assertEqual(self.invoke("ping")[0], 1)
        self.assertEqual(self.state.operation("ping")["status"], "pending")
        self.assertEqual(self.invoke("ping")[0], 1)
        self.assertEqual(len(self.runner.sent_keys), 1)

    def test_legacy_invalid_ping_receipt_requires_reconciliation(self):
        self.state.once("ping", lambda: {})
        self.assertEqual(self.invoke("ping")[0], 1)
        self.assertEqual(self.state.operation("ping")["status"], "pending")
        self.assertEqual(self.runner.sent_keys, [])

    def test_changing_permission_error_code_does_not_create_new_alert(self):
        self.invoke("brief")
        self.runner.error_code = "permission_denied_again"
        self.invoke("brief")
        self.assertEqual(len(self.runner.sent_keys), 1)

    def test_another_resource_does_not_resolve_failed_resource(self):
        denied = SimpleNamespace(returncode=1, stdout="", stderr=json.dumps({
            "ok": False, "error": {"type": "authorization", "code": "403"}}))
        success = SimpleNamespace(returncode=0, stdout='{"ok":true,"data":{}}', stderr="")
        client = Feishu(self.config, self.root, Mock(side_effect=[denied, success, denied]))
        def failed():
            try:
                client.call("+table-get", "--table-id", "table_private")
            except FeishuError as error:
                return alert_key(self.state, error, "health")
            self.fail("expected permission failure")
        first = failed()
        client.call("+table-get", "--table-id", "table_public")
        clear_recovered(self.state, client)
        self.assertEqual(first, failed())

    def test_native_dry_run_does_not_resolve_live_permission_failure(self):
        self.invoke("brief")
        self.runner.calendar_denied = False
        client = Feishu(self.config, self.root, self.runner)
        client._exec(["calendar", "+agenda", "--as", "user", "--dry-run"])
        clear_recovered(self.state, client)
        self.runner.calendar_denied = True
        self.invoke("brief")
        self.assertEqual(len(self.runner.sent_keys), 1)

    def test_legacy_invalid_alert_is_not_replayed_or_reported_delivered(self):
        self.state.set("authorization_incident", "auth-alert-old")
        self.state.once("auth-alert-old", lambda: {})
        for _ in range(2):
            _, result = self.invoke("brief")
            self.assertNotIn("owner_alert_message_id", result)
        self.assertEqual(self.state.operation("auth-alert-old")["status"], "pending")
        self.assertEqual(self.runner.sent_keys, [])

    def test_error_notification_keeps_the_project_lock(self):
        def check_lock():
            with self.assertRaises(MatrixError):
                with exclusive_lock(self.root / "state"):
                    pass
        self.runner.before_send = check_lock
        self.invoke("brief")
        self.assertEqual(len(self.runner.sent_keys), 1)


if __name__ == "__main__":
    unittest.main()
