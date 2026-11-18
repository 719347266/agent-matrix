"""Button latency, duplicate clicks, refresh/restart and verified completion."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from agent_matrix.commands import Commands
from agent_matrix.models import MatrixBusy
from test_v1_flow import applier, row
from agent_matrix.reply import parse, Op
from agent_matrix.timeutil import TZ
from datetime import datetime


def until(predicate, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("command did not settle")


class CommandQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.release = threading.Event()
        self.started = threading.Event()
        self.calls = []
        def slow(root, body, request_id, progress):
            self.calls.append((body, request_id))
            progress("正在保存到飞书")
            self.started.set()
            self.release.wait(3)
            return {"ok": True, "reply": "#1 完成 ✓"}
        self.commands = Commands(self.root, execute=slow)

    def tearDown(self):
        self.release.set()
        until(lambda: not self.commands.working)
        self.tmp.cleanup()

    def submit(self, request_id="click-one", text="1 完成"):
        return self.commands.submit({"text": text, "request_id": request_id})

    def test_ack_and_duplicate_clicks_do_not_wait_for_remote_write(self):
        start = time.monotonic()
        first = self.submit()
        self.assertLess(time.monotonic() - start, .5)
        self.assertTrue(self.started.wait(1))
        with self.commands.db() as db:
            self.assertEqual(db.execute("SELECT status FROM commands WHERE id=?", (first["id"],)).fetchone()[0], "running")
        # Even a second page using a different request ID cannot queue the same item.
        self.assertEqual(self.submit("click-two")["id"], first["id"])
        self.assertEqual(self.submit()["id"], first["id"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.commands.recent()[0]["message"], "正在保存到飞书")
        self.release.set()
        until(lambda: self.commands.recent()[0]["status"] == "done")
        self.assertEqual(self.submit()["status"], "done")
        self.assertEqual(len(self.calls), 1)

    def test_different_items_queue_without_dropping_either(self):
        self.submit()
        self.assertTrue(self.started.wait(1))
        second = self.submit("click-two", "2 完成")
        self.assertEqual(second["status"], "queued")
        self.release.set()
        until(lambda: all(c["status"] == "done" for c in self.commands.recent()))
        self.assertEqual(len(self.calls), 2)

    def test_failed_retry_keeps_original_id_and_survives_refresh(self):
        until(lambda: not self.commands.working)
        ids = []
        def fail_once(root, body, request_id, progress):
            ids.append(request_id)
            if len(ids) == 1:
                raise MatrixBusy("busy")
            return {"ok": True, "reply": "#1 完成 ✓"}
        self.commands.execute = fail_once
        first = self.submit()
        until(lambda: self.commands.recent()[0]["status"] == "failed")
        self.assertIn("尚未执行", self.commands.recent()[0]["message"])
        self.commands.retry(first["id"])
        until(lambda: self.commands.recent()[0]["status"] == "done")
        self.assertEqual(ids, [first["id"], first["id"]])
        self.commands.dismiss(first["id"])
        self.assertEqual(self.commands.recent(), [])

    def test_restart_does_not_replay_an_uncertain_write(self):
        until(lambda: not self.commands.working)
        now = time.time()
        with self.commands.db() as db:
            db.execute("INSERT INTO commands VALUES (?,?,?,?,?,?,?,0)",
                       ("interrupted", json.dumps({"kind":"cmd","text":"1 完成","n":None}), "[1]", "running", "saving", now, now))
        recovered = Commands(self.root, execute=lambda *a: self.fail("must not replay"))
        until(lambda: not recovered.working)
        result = recovered.recent()[0]
        self.assertEqual(result["status"], "failed")
        self.assertIn("核对", result["message"])

    def test_request_id_cannot_be_reused_for_different_content(self):
        self.submit()
        with self.assertRaises(ValueError):
            self.submit(text="2 完成")

    def test_http_returns_202_before_write_and_requires_local_token(self):
        from agent_matrix.app import make_server
        server = make_server(self.root, 0)
        until(lambda: not server.commands.working)
        server.commands = self.commands
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        def post(token):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            c.request("POST", "/api/commands", body=json.dumps({"text":"1 完成"}),
                      headers={"Content-Type":"application/json","X-Xinxiang-Token":token})
            r = c.getresponse()
            data = json.loads(r.read())
            c.close()
            return r.status, data
        try:
            self.assertEqual(post("wrong")[0], 403)
            status, data = post(server.token)
            self.assertEqual(status, 202)
            self.assertIn(data["command"]["status"], {"queued", "running"})
            self.assertFalse(self.release.is_set())
        finally:
            server.shutdown()
            server.server_close()


class ImmediateCompletionTests(unittest.TestCase):
    def test_optional_document_search_does_not_delay_completion_or_stop(self):
        a = applier([row(1, "进行中", 标题="撰写标签体系需求文档")])
        a.immediate, a.deferred = True, []
        with patch("agent_matrix.reply.find_deliverable", side_effect=AssertionError("must defer")), patch.object(a, "stop_ai", return_value="已通知停止") as stop:
            reply = a.one(Op([1], "complete"), 1, a.find(1))
        self.assertEqual(a.gateway.data["rec1"]["状态"], ["完成"])
        self.assertEqual(a.gateway.data["rec1"]["交付情况"], ["待补交付"])
        self.assertEqual(a.deferred[0]["record_id"], "rec1")
        self.assertIn("✓", reply)
        stop.assert_called_once()

    def test_failed_write_does_not_schedule_search_or_claim_completion(self):
        a = applier([row(1, "进行中", 标题="撰写标签体系需求文档")])
        a.immediate, a.deferred = True, []
        with patch.object(a, "write", side_effect=RuntimeError("readback unavailable")):
            with self.assertRaises(RuntimeError):
                a.one(Op([1], "complete"), 1, a.find(1))
        self.assertEqual(a.deferred, [])
        self.assertEqual(a.gateway.data["rec1"]["状态"], ["进行中"])

    def test_lost_restore_receipt_is_reconciled_after_status_changed(self):
        from unittest.mock import Mock
        a = applier([row(1, "待确认")])
        a.immediate = True
        a.state = Mock()
        a.state.operation.return_value = {"status": "pending"}
        a.state.get.return_value = {"record_id": "rec1", "patch": {"状态": ["待确认"]}}
        with patch.object(a.gateway, "update", return_value={"verified_by_readback": True}) as update:
            result = a.one(Op([1], "restore"), 1, a.find(1))
        self.assertIn("上次操作已保存 ✓", result)
        update.assert_called_once()

    def test_repeated_completion_is_a_success_without_a_new_write(self):
        a = applier([row(1, "完成", 标题="跟进交接", 交付情况=["无交付"])])
        a.immediate, a.deferred = True, []
        self.assertIn("✓", a.one(Op([1], "complete"), 1, a.find(1)))
        self.assertEqual(a.gateway.writes, [])


class VerifiedSnapshotTests(unittest.TestCase):
    def test_verified_write_updates_snapshot_before_success_is_returned(self):
        from agent_matrix.workflow import Gateway
        from agent_matrix.monitor import save_snapshot, _snapshot
        from agent_matrix.state import State
        from agent_matrix.models import Config
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = State(root / "state/state.db")
            original = row(1, "进行中")
            actual = dict(original)
            client = SimpleNamespace(config=Config(owner_open_id="ou_me", tables={"事项表":"table"}), root=root)
            def call(*args, **kwargs):
                actual.update({"状态":["完成"],"进度":"完成"})
                return {}
            client.call = call
            phases = []
            try:
                with patch("agent_matrix.workflow.require_owned"), patch("agent_matrix.workflow.read_records", side_effect=lambda *a:[dict(actual)]), patch("agent_matrix.workflow.validate_registry", return_value={}):
                    gateway = Gateway(client, state, on_rows=lambda rows:save_snapshot(root, rows), progress=phases.append)
                    receipt = gateway.update(original, {"状态":["完成"],"进度":"完成"}, "test", "test")
                    self.assertTrue(receipt["verified_by_readback"])
                    self.assertEqual(_snapshot(root)["rows"][0]["状态"], "完成")
                    self.assertEqual(phases, ["正在读取最新状态","正在保存","正在核对保存结果"])
            finally:
                state.close()
