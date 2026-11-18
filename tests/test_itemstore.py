"""本地事项库: clicks change the local store at once; the Feishu table only gets a background copy."""
import json
from pathlib import Path
import tempfile
import unittest

from agent_matrix import itemstore
from agent_matrix.feishu import Feishu
from agent_matrix.models import Config, MatrixError
from agent_matrix.records import read_records
from agent_matrix.state import State
from agent_matrix.workflow import FIELDS, Gateway, matches


def feishu_row(n, status="未开始", **extra):
    row = {f: None for f in FIELDS}
    row.update({"record_id": f"rec{n:04d}", "编号": str(n), "标题": f"事项{n}", "状态": [status], "执行方": ["我"],
                "截止日": "2026-11-20T00:00:00.000+08:00", "创建时间": "2026-11-16T10:00:00.000+08:00",
                "更新时间": "2026-11-16T10:00:00.000+08:00", **extra})
    return row


def no_feishu(*args, **kwargs):
    raise AssertionError("a click must not call Feishu")


class FakeFeishu:
    """Stands in for the Feishu table during a backup: remembers what was written."""
    def __init__(self, fail=False):
        self.calls, self.fail, self.n = [], fail, 0

    def call(self, command, *args, write=False):
        if self.fail:
            raise MatrixError("飞书调用失败（timeout）")
        body = json.loads(args[args.index("--json") + 1])
        self.calls.append((command, body))
        if command == "+record-batch-create":
            self.n += 1
            return {"record_id_list": [f"recFeishu{self.n}"]}
        return {}


class ItemStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "state").mkdir()
        self.config = Config(owner_open_id="ou_me", owner_name="我", base_token="base", item_store="local",
                             tables={"事项表": "tblItems", "周期规则表": "tblRules"})
        self.state = State(self.root / "state/state.db")
        self.state.set("owned_base", "base")
        itemstore.import_rows(self.root, [feishu_row(1), feishu_row(2, "待验收")], FIELDS)
        self.client = Feishu(self.config, self.root, runner=no_feishu)
        self.feishu = FakeFeishu()
        self.feishu.config = self.config

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def test_a_click_saves_locally_without_calling_feishu_and_reads_back_the_feishu_shape(self):
        gateway = Gateway(self.client, self.state)
        before = next(r for r in gateway.rows() if r["编号"] == "2")
        patch = {"状态": "完成", "验收结果": "通过", "完成时间": "2026-11-18T12:30:00+08:00",
                 "交付物链接": "https://example.com/doc", "卡点": None}
        receipt = gateway.update(before, patch, "test:done:2", "test")
        self.assertTrue(receipt["verified_by_readback"])
        after = next(r for r in read_records(self.client, self.state, "事项表", ["状态", "交付物链接", "完成时间"])
                     if r["record_id"] == before["record_id"])
        self.assertEqual(after["状态"], ["完成"])
        self.assertEqual(after["交付物链接"], "[https://example.com/doc](https://example.com/doc)")
        self.assertTrue(after["完成时间"].startswith("2026-11-18T12:30:00"))
        self.assertTrue(matches(after, {"状态": "完成", "完成时间": "2026-11-18T12:30:00+08:00"}))
        self.assertEqual(itemstore.status(self.root)["pending"], 1)

    def test_a_new_item_gets_the_next_number_and_a_local_id(self):
        gateway = Gateway(self.client, self.state)
        made = gateway.create({"标题": "新事项", "状态": "待确认", "执行方": "我", "截止日": "2026-11-21",
                               "父事项": [{"id": "rec0001"}]}, "test:new", "test")
        rid = made["record_id_list"][0]
        self.assertTrue(rid.startswith("recL"))
        row = next(r for r in gateway.rows() if r["record_id"] == rid)
        self.assertEqual(row["编号"], "3")
        self.assertEqual(row["截止日"], "2026-11-21T00:00:00.000+08:00")
        self.assertEqual(row["父事项"], [{"id": "rec0001"}])
        self.assertTrue(row["创建时间"])

    def test_other_tables_still_go_to_feishu(self):
        with self.assertRaises(AssertionError):  # the runner is the real Feishu here
            self.client.call("+record-batch-create", "--base-token", "base", "--table-id", "tblRules",
                             "--json", json.dumps({"create_records": [{"名称": "x"}]}), write=True)

    def test_backup_sends_changes_in_order_and_maps_new_ids(self):
        gateway = Gateway(self.client, self.state)
        rid = gateway.create({"标题": "新事项", "状态": "待确认"}, "test:new", "test")["record_id_list"][0]
        child = next(r for r in gateway.rows() if r["编号"] == "1")
        gateway.update(child, {"父事项": [{"id": rid}]}, "test:link", "test")
        out = itemstore.backup(self.root, self.feishu)
        self.assertEqual(out["pushed"], 2)
        self.assertEqual(out["pending"], 0)
        (c1, created), (c2, updated) = self.feishu.calls
        self.assertEqual(c1, "+record-batch-create")
        self.assertNotIn("编号", created["create_records"][0])     # Feishu numbers its own rows
        self.assertEqual(updated["update_records"], {"rec0001": {"父事项": [{"id": "recFeishu1"}]}})

    def test_a_failing_backup_keeps_the_local_change_and_tells_once_after_retries(self):
        from agent_matrix import inbox
        before = next(r for r in Gateway(self.client, self.state).rows() if r["编号"] == "1")
        Gateway(self.client, self.state).update(before, {"进度": "进行中"}, "test:p", "test")
        broken = FakeFeishu(fail=True)
        broken.config = self.config
        for _ in range(itemstore.BACKUP_GIVE_UP):
            itemstore.backup(self.root, broken)
        st = itemstore.status(self.root)
        self.assertEqual((st["pending"], st["failed"]), (0, 1))
        self.assertEqual(next(r for r in itemstore.rows(self.root) if r["编号"] == "1")["进度"], "进行中")
        self.assertEqual(len([m for m in inbox.messages(self.root) if "飞书备份" in m["title"] + str(m.get("summary"))]), 1)
        self.assertEqual(itemstore.retry_failed(self.root), 1)
        self.assertEqual(itemstore.backup(self.root, self.feishu)["pending"], 0)


if __name__ == "__main__":
    unittest.main()
