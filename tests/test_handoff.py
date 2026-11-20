"""One item handed to one AI: by the owner or by the AI working on it. Only that AI takes it; the rest stays as set."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agent_matrix import aidrivers, drive, engines
from agent_matrix.thread import set_setting


class Handoff(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "state").mkdir()
        aidrivers.hello(self.root, "Antigravity", model="gemini-3")       # does everything by default

    def mine(self, driver, rids=("r1", "r2", "r3"), builtin=False):
        allow = drive.item_filter(self.root, driver, builtin=builtin)
        return [r for r in rids if allow(r)]

    def test_only_the_ai_an_item_was_handed_to_takes_it(self):
        self.assertEqual(self.mine("Antigravity"), ["r1", "r2", "r3"])
        aidrivers.hello(self.root, "Claude Code", take=False)               # came for one item: takes nothing else over
        self.assertEqual(aidrivers.owner_of(self.root, "r1"), "driver:Antigravity")
        aidrivers.assign(self.root, "r2", "driver:Claude Code")
        aidrivers.assign(self.root, "r3", "claude")                         # the built-in driver with local Claude
        self.assertEqual(self.mine("Antigravity"), ["r1"])
        self.assertEqual(self.mine("Claude Code"), ["r2"])
        self.assertEqual(self.mine("本机 GPT", builtin=True), ["r3"])
        self.assertIs(engines.item_engine(self.root, "r3"), engines.ClaudeRun)
        self.assertIs(engines.item_engine(self.root, "r1"), engines.CodexRun)
        aidrivers.assign(self.root, "r1", aidrivers.HANDOFF)                # waiting for whoever gets the message
        self.assertEqual(self.mine("Antigravity") + self.mine("Claude Code") + self.mine("x", builtin=True), ["r2", "r3"])
        aidrivers.assign(self.root, "r2", "")                               # back to the usual one
        self.assertEqual(self.mine("Antigravity"), ["r2"])

    def test_names_typed_by_an_ai_are_understood(self):
        aidrivers.hello(self.root, "Claude Code", take=False)
        for typed, value in (("claude code", "driver:Claude Code"), ("本机 Claude", "claude"), ("本机gemini", "gemini"),
                             ("Cindy", "cindy"), ("Cursor", "driver:Cursor"), ("", aidrivers.HANDOFF)):
            self.assertEqual(aidrivers.resolve(self.root, typed), value, typed)
        self.assertEqual([aidrivers.label(v) for v in ("driver:Cursor", "claude", "cindy", aidrivers.HANDOFF)],
                         ["Cursor", "本机 Claude", "Cindy", "等另一个 AI 来接"])

    def test_handing_over_a_running_item_stops_that_run_and_writes_why(self):
        job = {"id": "item-r1-aaaa", "kind": "item", "title": "写周报", "n": 7, "folder": str(self.root), "status": "open",
               "ref": {"record_id": "r1", "version": "v1"}, "driver": "Antigravity", "rounds": 0, "step": ""}
        drive._save(self.root, job)
        notes, released = [], []
        with patch("agent_matrix.prework.add_note", lambda root, rid, text: notes.append(text)), \
             patch("agent_matrix.itemwork.Item.release", lambda item: released.append(item.rid)):
            self.assertEqual(drive.handoff(self.root, None, job["id"], "Antigravity", "", "Antigravity")["error"], "这就是你自己。")
            out = drive.handoff(self.root, None, job["id"], "Cursor", "要改界面，Cursor 更顺手", "Antigravity")
        self.assertEqual((out["status"], out["to"]), ("handed", "Cursor"))
        self.assertIn('hello --name "Cursor"', out["message"])
        self.assertIn("--helper", out["message"]); self.assertIn("--n 7", out["message"]); self.assertIn("#7 写周报", out["message"])
        self.assertEqual((drive.get(self.root, job["id"])["status"], released), ("handed", ["r1"]))
        self.assertEqual(aidrivers.assigned(self.root, "r1"), "driver:Cursor")
        self.assertEqual(notes, ["Antigravity 把这件事交给了 Cursor：要改界面，Cursor 更顺手"])
        # the owner hands it on again while Cursor works on it: Cursor is told to stop, and why
        job2 = {**job, "id": "item-r1-bbbb", "driver": "Cursor"}
        drive._save(self.root, job2)
        with patch("agent_matrix.prework.add_note", lambda root, rid, text: notes.append(text)), \
             patch("agent_matrix.itemwork.Item.release", lambda item: released.append(item.rid)), \
             patch("agent_matrix.workenv.kick") as kick:
            out = drive.hand_over(self.root, None, "r1", "gemini", by="本人", n=7, title="写周报")
        kick.assert_called_once()
        self.assertNotIn("message", out)                                    # a local AI needs no message
        said = drive.submit(self.root, None, job2["id"])
        self.assertEqual(said["status"], "cancelled")
        self.assertIn("本人把这件事交给了 本机 Gemini", said["note"])

    def test_handing_it_to_the_usual_ai_makes_it_follow_the_settings_again(self):
        aidrivers.assign(self.root, "r1", "driver:Cursor")
        with patch("agent_matrix.prework.add_note"):
            out = drive.hand_over(self.root, None, "r1", "driver:Antigravity", by="本人", n=3, title="写周报")
        self.assertIsNone(aidrivers.assigned(self.root, "r1"))
        self.assertEqual(out["to"], "Antigravity")
        self.assertIn('hello --name "Antigravity"', out["message"])

    def test_a_helper_says_hello_without_taking_the_rest(self):
        (self.root / "config.yaml").write_text("{}", encoding="utf-8")
        with patch.object(drive, "install_shim"), redirect_stdout(io.StringIO()) as out:
            drive.main(["hello", "--name", "Cursor", "--model", "gpt-5", "--helper", "--config", str(self.root / "config.yaml")])
        self.assertNotIn("--wait", json.loads(out.getvalue())["next"])
        from agent_matrix.thread import setting
        self.assertEqual(setting(self.root, "engine"), "driver:Antigravity")
        self.assertTrue(aidrivers.is_known(self.root, "Cursor"))

    def test_an_item_handed_to_someone_else_cannot_be_taken_by_number(self):
        aidrivers.assign(self.root, "r1", "driver:Cursor")
        with self.assertRaises(Exception) as caught:
            drive.next_job(self.root, None, "Antigravity", only="r1")
        self.assertIn("交给了 Cursor", str(caught.exception))
        aidrivers.assign(self.root, "r1", aidrivers.HANDOFF)
        picked = {}
        def prepare(root, config, driver, skip=(), allow=None, name_for=None):
            picked["allowed"] = [r for r in ("r1", "r2") if allow(r)]
            return None
        with patch.object(drive, "_prepare_item", prepare):
            self.assertIsNone(drive.next_job(self.root, None, "Gemini CLI", only="r1"))
        self.assertEqual(picked["allowed"], ["r1"])


if __name__ == "__main__":
    unittest.main()
