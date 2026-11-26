"""Items recorded in Notion (or only on this computer): no Feishu account is touched anywhere on the way."""
from pathlib import Path
import tempfile
import unittest

from agent_matrix import inbox, itemstore, notion, records, storemode
from agent_matrix.config import load, save
from agent_matrix.feishu import Feishu
from agent_matrix.models import Config, MatrixError
from agent_matrix.state import State
from agent_matrix.workflow import Gateway


def no_lark(*args, **kwargs):
    raise AssertionError("lark-cli must not run without Feishu")


class FakeNotion:
    def __init__(self):
        self.calls = []

    def __call__(self, tok, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "databases":
            return {"id": "db1", "url": "https://www.notion.so/db1"}
        if path == "pages":
            return {"id": f"page{len(self.calls)}"}
        return {}


class NotionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        save(self.root / "config.yaml", Config())

    def test_item_lands_locally_and_in_inbox_without_feishu_then_mirrors_to_notion(self):
        api = FakeNotion()
        out = notion.connect(self.root, " ntn_secret1234 ", "https://www.notion.so/My-Page-0123456789abcdef0123456789abcdef", api=api)
        self.assertEqual(out["tail"], "1234")
        method, path, body = api.calls[0]
        self.assertEqual(body["parent"]["page_id"], "0123456789abcdef0123456789abcdef")
        self.assertEqual(body["properties"]["标题"], {"title": {}})
        config = load(self.root / "config.yaml")
        self.assertEqual((config.item_store, config.notion_database_id, config.tables["事项表"]), ("notion", "db1", "local"))

        # The chat/meeting path: auth, ownership check, create, message — all without lark-cli.
        client = Feishu(config, self.root, runner=no_lark)
        client.verify_owner(client.auth())
        state = State(self.root / "state/state.db")
        try:
            gateway = Gateway(client, state)
            rid = gateway.create({"标题": "周五前发报价单", "状态": "待确认", "来源链接": "https://x.slack.com/archives/C1/p1",
                                  "截止日": "2026-11-20"}, "k1", "test")["record_id_list"][0]
            self.assertEqual([r["标题"] for r in gateway.rows()], ["周五前发报价单"])
            client.send_self("聊天 · 新增 1 件：你 #1", "k1")
        finally:
            state.close()
        self.assertEqual(inbox.messages(self.root, 5)[0]["title"][:4], "聊天 ·")

        push = notion.push(config, api=api)
        itemstore.backup(self.root, push=push)
        method, path, body = api.calls[-1]
        self.assertEqual((method, path), ("POST", "pages"))
        props = body["properties"]
        self.assertEqual(props["标题"]["title"][0]["text"]["content"], "周五前发报价单")
        self.assertEqual(props["状态"], {"select": {"name": "待确认"}})
        self.assertEqual(props["来源链接"], {"url": "https://x.slack.com/archives/C1/p1"})
        self.assertEqual(props["编号"], {"number": 1})
        self.assertTrue(props["截止日"]["date"]["start"].startswith("2026-11-20"))

        itemstore.update(self.root, rid, {"状态": "完成"})
        itemstore.backup(self.root, push=push)
        method, path, body = api.calls[-1]
        self.assertEqual((method, path), ("PATCH", "pages/page2"))  # same page, not a second one
        self.assertEqual(body["properties"]["状态"], {"select": {"name": "完成"}})
        self.assertEqual(itemstore.status(self.root)["pending"], 0)

    def test_choices_check_what_they_need(self):
        with self.assertRaises(MatrixError):
            storemode.choose(self.root, "notion")   # not connected yet
        with self.assertRaises(MatrixError):
            storemode.choose(self.root, "feishu")   # Feishu never set up
        self.assertEqual(storemode.choose(self.root, "device"), "device")
        self.assertTrue(itemstore.imported(self.root))
        with self.assertRaises(MatrixError):
            notion.connect(self.root, "xoxp-wrong", "https://www.notion.so/0123456789abcdef0123456789abcdef", api=FakeNotion())

    def test_ai_picks_up_its_items_without_a_feishu_account(self):
        from datetime import date
        from agent_matrix.agents.pre_worker import assess
        from agent_matrix.workflow import FIELDS
        config = Config(item_store="device", prework_enabled=True)
        row = {f: None for f in FIELDS}
        row.update({"record_id": "recL1", "标题": "测试服升级 tracks 包", "执行方": ["AI"], "状态": ["未开始"],
                    "负责人": [{"id": ""}], "截止日": "2026-11-21T00:00:00.000+08:00", "编号": "5"})
        out = assess(config, [row, {**row, "record_id": "recL2", "执行方": ["我"]}], today=date(2026, 11, 19))
        self.assertTrue(out["ok"])
        self.assertEqual([c["record_id"] for c in out["candidates"]], ["recL1"])  # only the AI's own item

    def test_a_reply_reaches_the_ai_without_feishu(self):
        """Local-only items are read in their full shape, so 补充材料 changes the AI's input and unblocks it."""
        from agent_matrix import itemstore
        from agent_matrix.feishu import Feishu
        config = Config(item_store="device")
        save(self.root / "config.yaml", config)
        state = State(self.root / "state/state.db")
        try:
            records.ensure_local(Feishu(config, self.root, runner=no_lark), state)
            self.assertEqual(state.get("brief-v1:migration-stage"), "v1_active")
        finally:
            state.close()

    def test_linked_items_and_people_read_as_text(self):
        kinds = notion.schema(["标题", "父事项", "负责人", "排除本人提醒", "方向"])
        props = notion.properties({"标题": "子事项", "父事项": [{"id": "recA"}], "负责人": [{"id": "ou_1", "name": "小王"}],
                                   "排除本人提醒": True, "方向": ["产品,运营"]}, kinds, {"recA": 7})
        self.assertEqual(props["父事项"]["rich_text"][0]["text"]["content"], "#7")
        self.assertEqual(props["负责人"]["rich_text"][0]["text"]["content"], "小王")
        self.assertEqual(props["排除本人提醒"], {"checkbox": True})
        self.assertEqual(props["方向"], {"select": {"name": "产品，运营"}})


if __name__ == "__main__":
    unittest.main()
