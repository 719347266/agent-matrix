import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent_matrix.linked_sync import LinkedGateway, parse_row, task_guid, run
from agent_matrix.automation import Decision
from agent_matrix.models import Config, MatrixError
from agent_matrix.state import State


GUID = "12345678-1234-1234-1234-123456789abc"
URL = "https://applink.feishu.cn/client/todo/task?guid=" + GUID


class LinkedSyncTests(unittest.TestCase):
    def test_production_gateway_never_writes_legacy_complete(self):
        with TemporaryDirectory() as directory:
            state = State(Path(directory)/"state.db"); state.set("owned_base", "base_owned")
            client = Mock(); client.config = Config(base_token="base_owned", tables={"事项表": "tbl_owned"}); client.root = Path(directory)
            gateway = LinkedGateway(client, state)
            with self.assertRaisesRegex(MatrixError, "不能直接写完成"):
                gateway.write_complete(Mock(), Mock(), "proof")
            client.call.assert_not_called()
            state.close()

    def test_apply_converts_completed_source_to_read_only_review_during_transition(self):
        row = {"record_id": "r1", "标题": "别人交付", "状态": ["待办"], "完成判定": ["关联飞书任务完成"],
               "自动检测": True, "关联任务链接": URL, "更新时间": "2026-11-16T10:00:00+08:00"}
        with patch("agent_matrix.linked_sync.LinkedGateway") as factory, patch("agent_matrix.linked_sync.decide", return_value=Decision("complete", "源任务已完成")):
            gateway = factory.return_value; gateway.read_rows.return_value = [row]
            for apply in (False, True):
                result = run(Mock(), Mock(), apply=apply)
                self.assertEqual(result["results"][0]["action"], "review")
                self.assertFalse(result["results"][0]["written"])
                self.assertFalse(result["results"][0]["checked"])
                self.assertTrue(result["completion_writes_disabled"])
            gateway.write_complete.assert_not_called()

    def test_task_link_must_be_exact_trusted_host_and_valid_guid(self):
        self.assertEqual(task_guid(URL), GUID)
        self.assertEqual(task_guid(URL.replace("applink.feishu.cn", "applink.larksuite.com")), GUID)   # Lark (international)
        for url in (URL.replace("applink.feishu.cn", "applink.feishu.cn.evil.invalid"),
                    URL.replace("https:", "http:"), URL+"&guid="+GUID,
                    URL.replace(GUID, "not-a-guid"), "file:///etc/passwd"):
            with self.assertRaises(MatrixError): task_guid(url)

    def test_decode_native_record_without_guessing_unknown_rule_or_timezone(self):
        row = {"record_id": "r1", "标题": "任务", "状态": ["待办"], "完成判定": ["关联飞书任务完成"],
               "自动检测": True, "关联任务链接": URL, "更新时间": "2026-11-16T10:00:00+08:00"}
        item = parse_row(row)
        self.assertEqual(item.source_id, GUID)
        self.assertEqual(item.rule, "task_completed")
        self.assertGreater(item.updated_ms, 0)
        self.assertEqual(parse_row({**row, "更新时间": "2026-11-16 10:00"}).updated_ms, 0)
        self.assertEqual(parse_row({**row, "完成判定": ["陌生规则"]}).rule, "manual")

    def test_empty_full_table_is_valid_and_partial_snapshot_is_not(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = State(root/"state/state.db")
            state.set("owned_base", "base_owned")
            config = Config(base_token="base_owned", tables={"事项表": "tbl_owned"})
            metadata = {"base_token": "base_owned", "table_id": "tbl_owned", "has_more": False,
                        "records_count": 0, "rev": 1}
            def execute(argv, **kwargs):
                path = root/argv[argv.index("--output")+1]
                path.write_text("")
                path.with_suffix(".manifest.json").write_text(json.dumps(metadata))
                return {}
            gateway = LinkedGateway(SimpleNamespace(root=root, config=config, _exec=execute), state)
            self.assertEqual(gateway.read_rows(), [])
            metadata["has_more"] = True
            with self.assertRaises(MatrixError): gateway.read_rows()
            metadata["has_more"] = False
            metadata["base_token"] = "wrong_base"
            with self.assertRaises(MatrixError): gateway.read_rows()
            state.close()

    def test_missing_record_cannot_be_updated(self):
        with TemporaryDirectory() as directory:
            state = State(Path(directory)/"state.db")
            state.set("owned_base", "base_owned")
            client = SimpleNamespace(root=Path(directory), config=Config(base_token="base_owned", tables={"事项表": "tbl_owned"}))
            gateway = LinkedGateway(client, state)
            gateway.read_rows = lambda *args: []
            with self.assertRaises(MatrixError): gateway.get_item("nonexistent")
            state.close()


if __name__ == "__main__":
    unittest.main()
