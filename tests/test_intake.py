"""What is taken in from other people (全能 / 全自动 / 手动), and items the owner adds by hand."""
import json
from pathlib import Path
import tempfile
import unittest

from agent_matrix import chat_review, manual
from agent_matrix.config import load, save
from agent_matrix.models import Config, MatrixError
from agent_matrix import itemstore

OWNER = dict(owner_name="王小明", owner_open_id="ou_me", owner_aliases=["小王"])


class ChatAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        folder = self.root / "state/chat-batches/b1"
        folder.mkdir(parents=True)
        base = {"chat_id": "c", "chat_name": "李", "chat_mode": "p2p", "sent_at": "2026-11-19 17:52", "kind": "找你的"}
        messages = [
            {**base, "message_id": "yes", "sender_name": "李", "text": "记得做季度总结", "after": ["[11-19 17:58 王小明] 好的"]},
            {**base, "message_id": "silent", "sender_name": "李", "text": "记得做竞品调研表", "after": ["[11-19 17:59 李] 好的"]},
            {**base, "message_id": "own", "sender_name": "我", "text": "周报我来写", "after": []},
        ]
        (folder / "candidates.json").write_text(json.dumps({"batch_id": "b1", "messages": messages}, ensure_ascii=False))

    def review(self, **quotes):
        def action(mid, title, quote, phrase):
            return chat_review.ChatAction(message_id=mid, title=title, quote=quote, commitment_phrase=phrase, owner_name="王小明",
                                          confidence=0.95, accepted_quote=quotes.get(mid))
        return chat_review.ChatReview(batch_id="b1", actions=[
            action("yes", "做季度总结", "记得做季度总结", "记得做"),
            action("silent", "做竞品调研表", "记得做竞品调研表", "记得做"),
            action("own", "写周报", "周报我来写", "我来")])

    def kept(self, mode, **quotes):
        config = Config(**OWNER, intake_mode=mode)
        return [a.message_id for a, _ in chat_review.validate(config, self.review(**quotes), self.root)]

    def test_all_mode_takes_everything_asked_of_the_owner(self):
        self.assertEqual(self.kept("all"), ["yes", "silent", "own"])

    def test_accepted_mode_takes_only_what_the_owner_agreed_to_or_said_themself(self):
        self.assertEqual(self.kept("accepted", yes="好的"), ["yes", "own"])

    def test_the_model_cannot_invent_the_owners_yes(self):
        # "好的" was said, but by the other person: it does not count as the owner agreeing
        self.assertEqual(self.kept("accepted", yes="好的", silent="好的"), ["yes", "own"])
        self.assertEqual(self.kept("accepted", yes="没问题我来做"), ["own"])  # words the owner never wrote


class ManualItemTests(unittest.TestCase):
    """No Feishu, no chat tool: the owner still adds items, on this computer only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        save(self.root / "config.yaml", Config(item_store="device", worker_enabled=False, intake_mode="manual"))

    def test_an_item_is_created_with_a_number_and_the_date_worked_out(self):
        first = manual.add(self.root, "  整理十二月  活动排期 ", who="我", due="明天", note="按渠道分")
        second = manual.add(self.root, "写复盘", who="AI")
        self.assertEqual((first["n"], second["n"]), (1, 2))
        rows = {int(str(r["编号"])): r for r in itemstore.rows(self.root)}
        self.assertEqual((rows[1]["标题"], rows[1]["执行方"], rows[1]["状态"], rows[1]["下一步动作"]), ("整理十二月 活动排期", ["我"], ["未开始"], "按渠道分"))
        self.assertTrue(rows[1]["截止日"])
        self.assertEqual(rows[2]["执行方"], ["AI"])

    def test_an_empty_title_or_an_unreadable_date_creates_nothing(self):
        for bad in (dict(title="  "), dict(title="写复盘", due="哪天有空"), dict(title="写复盘", who="别人")):
            with self.assertRaises(MatrixError):
                manual.add(self.root, **bad)
        self.assertEqual(itemstore.rows(self.root) if itemstore.imported(self.root) else [], [])


class ConfigDefaultsTests(unittest.TestCase):
    def test_an_existing_config_without_the_new_keys_keeps_todays_behaviour(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("owner_name: 王小明\n", encoding="utf-8")
            config = load(path)
            self.assertEqual((config.intake_mode, config.brief_channel), ("all", "feishu"))


if __name__ == "__main__":
    unittest.main()
