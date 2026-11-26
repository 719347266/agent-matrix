"""记忆增强: Hy Memory with the owner's own API, written in the background, recalled into an AI job's background."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_matrix import hymem, memory
from agent_matrix.models import Config
from datetime import datetime
from agent_matrix.timeutil import TZ

NOW = datetime(2026, 11, 21, 18, 0, tzinfo=TZ)


class Fake:
    def __init__(self):
        self.added, self.closed = [], False

    def add(self, text, **kw):
        if "炸" in text:
            raise RuntimeError("接口出错 401")
        self.added.append((text, kw))

    def search(self, q, user_ids=None, limit=8):
        return {"memories": {"profile": [{"content": "本人是示例的产品负责人"}],
                             "normal": [{"content": t} for t, _ in self.added if q[:2] in t][:limit], "proactive": []}}

    def close(self):
        self.closed = True


def cfg(**kw):
    base = dict(memory_boost_enabled=True, memory_base_url="https://api.example.com/v1", memory_api_key="sk-x",
                memory_llm_model="deepseek-chat", memory_embed_model="text-embedding-v3")
    return Config(**{**base, **kw})


class Boost(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        (self.root / "state").mkdir()
        self.fake = Fake()
        p1 = patch.object(hymem, "installed", return_value=True)
        p2 = patch.object(hymem, "client", return_value=self.fake)
        p1.start(); p2.start(); self.addCleanup(p1.stop); self.addCleanup(p2.stop)

    def test_off_or_half_filled_does_nothing(self):
        self.assertFalse(hymem.ready(cfg(memory_boost_enabled=False)))
        self.assertFalse(hymem.ready(cfg(memory_api_key="")))
        self.assertFalse(hymem.remember(self.root, "verdict", "x"), "switched off: nothing is queued")
        self.assertEqual(hymem.flush(self.root, cfg(memory_boost_enabled=False)), (0, 0))
        self.assertEqual(hymem.search(self.root, cfg(memory_embed_model=""), "会员"), [])

    def test_queued_once_written_in_the_background_and_failures_retried_then_given_up(self):
        hymem.mark(self.root, True)
        self.assertTrue(hymem.remember(self.root, "verdict", "本人评审「会员方案」：退回，原因：太长", workspace="示例 App", ref="v:1"))
        self.assertFalse(hymem.remember(self.root, "verdict", "同一条再来一次", ref="v:1"))   # same ref: kept once
        hymem.remember(self.root, "meeting", "会议炸了", ref="m:1")
        self.assertEqual(hymem.flush(self.root, cfg()), (1, 1))
        self.assertEqual(self.fake.added[0][1]["metadata"], {"project": "示例 App", "kind": "verdict"})
        for _ in range(hymem.MAX_TRIES):
            hymem.flush(self.root, cfg())
        st = hymem.status(self.root)
        self.assertEqual((st["written"], st["waiting"], st["stuck"]), (1, 0, 1))
        self.assertIn("401", st["error"])

    def test_search_gives_plain_lines_profile_first(self):
        hymem.mark(self.root, True)
        hymem.remember(self.root, "deliverable", "会员方案第 2 版：权益分三档", ref="d:1")
        hymem.flush(self.root, cfg())
        self.assertEqual(hymem.search(self.root, cfg(), "会员"), ["本人是示例的产品负责人", "会员方案第 2 版：权益分三档"])

    def test_switching_on_starts_from_what_xinxiang_knows(self):
        hymem.mark(self.root, True)
        memory.set_profile(self.root, "岗位：产品负责人")
        memory.record_outcome(self.root, "r1", "退回", "方向不对")
        self.assertEqual(hymem.backfill(self.root), 2)
        self.assertEqual(hymem.backfill(self.root), 0, "already queued: not twice")

    def test_profile_sync_keeps_only_new_information_and_can_revert(self):
        hymem.mark(self.root, True)
        original = "## 工作\n产品经理\n\n## 格式\n先给结论"
        edited = original.replace("产品经理", "产品和增长")
        memory.set_profile(self.root, original)
        self.assertTrue(hymem.remember_profile(self.root, original))
        memory.set_profile(self.root, edited)
        self.assertTrue(hymem.remember_profile(self.root, edited, original))
        self.assertFalse(hymem.remember_profile(self.root, edited))
        memory.set_profile(self.root, original)
        self.assertFalse(hymem.remember_profile(self.root, edited, original))
        self.assertTrue(hymem.remember_profile(self.root, original, edited))
        self.assertEqual(hymem.flush(self.root, cfg()), (3, 0))
        self.assertNotIn("先给结论", self.fake.added[1][0])
        self.assertIn("更新为：产品经理", self.fake.added[2][0])

    def test_settings_go_to_hy_memory_with_the_data_kept_in_this_project(self):
        env = hymem._env(self.root, cfg(memory_embed_base_url="https://emb.example.com/v1"))
        self.assertEqual(env["MEMORY_DATA_DIR"], str(self.root / "state" / "hy_memory"))
        self.assertEqual((env["MEMORY_LLM_BASE_URL"], env["MEMORY_EMBEDDER_BASE_URL"], env["MEMORY_EMBEDDER_API_KEY"]),
                         ("https://api.example.com/v1", "https://emb.example.com/v1", "sk-x"))
        self.assertEqual(env["MEMORY_ENABLE_GRAPH"], "false")

    def test_the_ai_job_background_gets_what_it_recalls(self):
        from agent_matrix import background
        hymem.mark(self.root, True)
        with patch.object(hymem, "search_via_app", return_value=["11-20 例会：付费归因并入 2.3"]):
            text = background.build(self.root, None, [], {"record_id": "r1", "编号": 1, "标题": "付费归因埋点"}, NOW)
        self.assertIn("记忆增强想起来的", text); self.assertIn("付费归因并入 2.3", text)
        hymem.mark(self.root, False)
        with patch.object(hymem, "search_via_app", return_value=["x"]) as asked:
            background.build(self.root, None, [], {"record_id": "r1", "编号": 1, "标题": "付费归因埋点"}, NOW)
        asked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
