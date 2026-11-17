"""Executable offline acceptance example. Never writes to Feishu."""
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from .automation import Item, Evidence, sync_one
from .digest import Notice, build_digest, delivery_plan, render_text
from .state import State


class DemoGateway:
    def __init__(self, items, evidence):
        self.items = {i.id: i for i in items}
        self.evidence = {e.item_id: e for e in evidence}
        self.writes = []

    def get_item(self, item_id):
        return self.items[item_id]

    def get_evidence(self, item):
        return self.evidence[item.id]

    def write_complete(self, item, evidence, proof_id):
        self.writes.append(item.id)
        self.items[item.id] = replace(item, status="完成", revision="after-sync", proof_id=proof_id)
        return {"fixture": True, "record_id": item.id}


def run_demo():
    now = 1_800_000_000_000
    items = [
        Item("demo_1", "提交评审材料", "进行中", "v1", now-60000, "demo_task", "task_completed", True),
        Item("demo_2", "完成设计验收", "进行中", "v1", now-60000, "demo_design", "task_completed", True),
        Item("demo_3", "确认本周排期", "待办", "v1", now-60000),
    ]
    evidence = [
        Evidence("done_1", "demo_1", "demo_task", "task_status", "completed", now-1000, now,
                 "https://example.invalid/task/demo_task", "关联任务状态：完成"),
        Evidence("message_2", "demo_2", "demo_design", "message", "completed", now-1000, now,
                 "https://example.invalid/message/demo_2", "稿子改好了"),
    ]
    notices = [
        Notice("m1", "demo_3", "排期变更", "评审可能调整时间，待最终确认", now-40000, "https://example.invalid/m1"),
        Notice("m2", "demo_3", "排期变更", "评审时间已确认调整，材料准备时间相应提前", now-20000, "https://example.invalid/m2"),
    ]
    gateway = DemoGateway(items, evidence)
    with TemporaryDirectory() as directory:
        state = State(Path(directory) / "demo.db")
        try:
            results = [sync_one(items[n], e, gateway, state, now_ms=now) for n, e in enumerate(evidence)]
            again = sync_one(items[0], evidence[0], gateway, state, now_ms=now)
            digest = build_digest(list(gateway.items.values()), notices, results)
            plan = delivery_plan(digest, window="demo-evening")
            duplicate = delivery_plan(digest, window="demo-evening", previous={**plan, "message_id": "demo_message"})
            return {"simulation": True, "remote_writes": 0, "test_table_writes": len(gateway.writes),
                    "repeat_status": again["status"], "duplicate_notification": duplicate["action"],
                    "digest": digest, "text": render_text(digest, demo=True)}
        finally:
            state.close()


if __name__ == "__main__":
    print(json.dumps(run_demo(), ensure_ascii=False, indent=2))
