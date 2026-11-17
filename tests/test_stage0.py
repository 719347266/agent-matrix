from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent_matrix import bitable
from agent_matrix.cli import main
from agent_matrix.feishu import Feishu, FeishuError
from agent_matrix.models import Config, MatrixError
from agent_matrix.schema import TABLES, SEEDS
from agent_matrix.state import State, exclusive_lock


class FakeFeishu:
    def __init__(self):
        self.tables = {}
        self.fields = {}
        self.views = {}
        self.filters = {}
        self.writes = []

    def auth(self):
        return {"app_id": "cli_fixture", "user": {"openId": "ou_fixture_owner", "userName": "示例用户"}}

    def verify_owner(self, status):
        pass

    def call(self, command, *args, write=False):
        flags = dict(zip(args[::2], args[1::2]))
        if write:
            self.writes.append(command)
        if command == "+base-create":
            self.tables["事项表"] = "tbl_tasks"
            self.fields["tbl_tasks"] = json.loads(flags["--fields"])
            return {"base_token": "base_fixture", "url": "https://fixture.invalid/base/base_fixture"}
        if command == "+table-list":
            return {"items": [{"name": n, "table_id": i} for n, i in self.tables.items()]}
        if command == "+table-create":
            tid = "tbl_" + str(len(self.tables))
            self.tables[flags["--name"]] = tid
            self.fields[tid] = json.loads(flags["--fields"])
            return {"table_id": tid}
        if command == "+field-list":
            return {"items": self.fields[flags["--table-id"]]}
        if command == "+table-get":
            return {"fields": self.fields[flags["--table-id"]]}
        if command == "+field-create":
            self.fields[flags["--table-id"]].append(json.loads(flags["--json"]))
            return {"field_id": "fld_week"}
        if command == "+view-list":
            return {"items": [{"name": n, "view_id": i} for n, i in self.views.items()]}
        if command == "+view-create":
            name = json.loads(flags["--json"])["name"]
            self.views[name] = "vew_" + str(len(self.views))
            return {"view_id": self.views[name]}
        if command == "+view-set-filter":
            name = next(n for n, i in self.views.items() if i == flags["--view-id"])
            self.filters[name] = json.loads(flags["--json"])
            return {}
        if command == "+record-batch-create":
            return {"record_id_list": ["rec_" + str(i) for i in range(len(json.loads(flags["--json"])["create_records"]))]}
        raise AssertionError(command)


class Stage0Tests(unittest.TestCase):
    def test_dry_run_neither_calls_service_nor_writes_any_file(self):
        with TemporaryDirectory() as d, patch("agent_matrix.cli.Feishu") as client:
            p = Path(d)
            for cmd in ("setup", "ping", "health", "recurring", "brief"):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main([cmd, "--dry-run", "--config", str(p / "config.yaml")]), 0)
            client.assert_not_called()
            self.assertEqual(list(p.iterdir()), [])

    def test_setup_twice_does_not_duplicate_or_reset_user_edits(self):
        with TemporaryDirectory() as d:
            p = Path(d); state = State(p / "state.db"); fake = FakeFeishu(); config = Config()
            result = bitable.setup(config, p / "config.yaml", fake, state)
            self.assertEqual(len(result["tables"]), 4)
            self.assertEqual(len(result["views"]), 5)
            before = list(fake.writes)
            fake.filters["今天"] = {"conditions": []}  # user changed view
            bitable.setup(config, p / "config.yaml", fake, state)
            self.assertEqual(fake.writes, before)
            self.assertEqual(fake.filters["今天"], {"conditions": []})
            state.close()

    def test_uncertain_mutation_is_never_replayed(self):
        with TemporaryDirectory() as d:
            state = State(Path(d) / "state.db"); fn = Mock(side_effect=FeishuError("timeout"))
            with self.assertRaises(FeishuError): state.once("create-base", fn)
            with self.assertRaises(MatrixError): state.once("create-base", fn)
            self.assertEqual(fn.call_count, 1); state.close()

    def test_completed_message_reuses_receipt(self):
        with TemporaryDirectory() as d:
            state = State(Path(d) / "state.db"); fn = Mock(return_value={"message_id": "om_fixture"})
            self.assertEqual(state.once("ping", fn), state.once("ping", fn))
            self.assertEqual(fn.call_count, 1); state.close()

    def test_proven_unsent_preview_can_be_fixed_and_retried(self):
        with TemporaryDirectory() as d:
            state = State(Path(d) / "state.db")
            error = FeishuError("invalid_argument"); error.not_sent = True
            with self.assertRaises(FeishuError): state.once("new-field", Mock(side_effect=error))
            self.assertEqual(state.once("new-field", lambda: {"id": "fld_fixed"}), {"id": "fld_fixed"})
            state.close()

    def test_no_message_before_account_verification(self):
        runner = Mock()
        client = Feishu(Config(owner_open_id="ou_fixture"), Path.cwd(), runner)
        with self.assertRaises(MatrixError): client.send_self("hello", "test-key")
        runner.assert_not_called()

    def test_refuse_foreign_base(self):
        with TemporaryDirectory() as d:
            state = State(Path(d) / "state.db")
            with self.assertRaises(MatrixError): bitable.require_owned(Config(base_token="foreign"), state)
            state.close()

    def test_parse_success_by_ok_not_upstream_code(self):
        runner = Mock(return_value=SimpleNamespace(returncode=0, stdout='{"ok":true,"identity":"user","data":{"items":[]}}', stderr=""))
        client = Feishu(Config(), Path.cwd(), runner)
        self.assertEqual(client.call("+table-list", "--base-token", "fixture"), {"items": []})

    def test_error_uses_stderr_and_does_not_disclose_raw_body(self):
        runner = Mock(return_value=SimpleNamespace(returncode=1, stdout="", stderr='{"ok":false,"error":{"type":"authorization","code":999,"message":"SECRET_TRANSCRIPT"}}'))
        client = Feishu(Config(), Path.cwd(), runner)
        with self.assertRaises(FeishuError) as e: client.call("+table-list")
        self.assertTrue(e.exception.authorization)
        self.assertNotIn("SECRET", str(e.exception))

    def test_ndjson_has_a_manifest_not_a_normal_api_envelope(self):
        manifest = {"manifest_version": "v1", "format": "ndjson", "records_count": 0, "has_more": False}
        runner = Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps(manifest), stderr=""))
        client = Feishu(Config(), Path.cwd(), runner)
        self.assertEqual(client._exec(["base", "+record-list"], manifest=True), manifest)
        with self.assertRaises(FeishuError): client._exec(["base", "+record-list"])

    def test_high_risk_gate_is_not_retried(self):
        runner = Mock(return_value=SimpleNamespace(returncode=10, stdout="", stderr='{"ok":false,"error":{"hint":"append --yes"}}'))
        client = Feishu(Config(), Path.cwd(), runner)
        with self.assertRaises(FeishuError): client.call("+base-create", write=True)
        self.assertEqual(runner.call_count, 1)
        self.assertNotIn("--yes", runner.call_args.args[0])

    def test_unapproved_commands_never_run(self):
        runner = Mock(); client = Feishu(Config(), Path.cwd(), runner)
        with self.assertRaises(MatrixError): client.call("+table-delete", write=True)
        runner.assert_not_called()

    def test_auth_identity_mismatch(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/auth-ready.json").read_text())
        runner = Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps(fixture), stderr=""))
        client = Feishu(Config(owner_open_id="ou_another"), Path.cwd(), runner)
        status = client.auth()
        with self.assertRaises(MatrixError): client.verify_owner(status)

    def test_later_phases_stay_disabled(self):
        with TemporaryDirectory() as d, patch("agent_matrix.cli.Feishu") as client:
            for cmd in ("followup", "prep", "review", "prework"):
                with redirect_stderr(io.StringIO()): self.assertEqual(main([cmd, "--config", str(Path(d) / "config.yaml")]), 1)
            client.assert_not_called()

    def test_concurrent_run_lock(self):
        with TemporaryDirectory() as d:
            with exclusive_lock(Path(d)):
                with self.assertRaises(MatrixError):
                    with exclusive_lock(Path(d)): pass

    def test_example_rules_do_not_guess_frequency_or_owner(self):
        self.assertEqual(len(SEEDS), 6)
        for row in SEEDS:
            self.assertFalse(row["启用"])
            self.assertFalse(row["能否AI预做"])
            self.assertNotIn("频率", row)
            self.assertNotIn("默认负责人", row)


if __name__ == "__main__":
    unittest.main()
