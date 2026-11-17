"""Deterministic completion rules. Remote text is evidence, never an instruction.

The gateway must re-read a bound source, and write only to the project's Base.
This module does not discover chats, run a model, start a scheduler or send messages.
"""
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal, Protocol

from .models import MatrixError


@dataclass(frozen=True)
class Item:
    id: str
    title: str
    status: str
    revision: str
    updated_ms: int
    source_id: str = ""
    rule: str = "manual"
    auto_enabled: bool = False
    proof_id: str = ""


@dataclass(frozen=True)
class Evidence:
    id: str
    item_id: str
    source_id: str
    kind: Literal["task_status", "approval_status", "message", "artifact"]
    outcome: str
    happened_ms: int
    observed_ms: int
    source_url: str
    quote: str = ""


@dataclass(frozen=True)
class Decision:
    action: Literal["complete", "review", "skip"]
    reason: str


RULES = {"task_completed": ("task_status", "completed"),
         "approval_passed": ("approval_status", "approved")}


def decide(item: Item, evidence: Evidence, *, now_ms: int) -> Decision:
    if item.status in {"完成", "取消"}:
        return Decision("skip", "已完成或取消，保留现有状态")
    if evidence.item_id != item.id or evidence.source_id != item.source_id or not item.source_id:
        return Decision("skip", "证据未绑定到同一事项和同一来源")
    if not item.auto_enabled or item.rule == "manual":
        return Decision("review", "该事项尚未开启自动判定")
    if item.status not in {"待办", "进行中"}:
        return Decision("review", "事项仍待确认或有阻塞，需确认后才能结束")
    expected = RULES.get(item.rule)
    if expected is None or (evidence.kind, evidence.outcome) != expected:
        if evidence.kind in {"message", "artifact"}:
            return Decision("review", "消息或文件变化只能提示进展，不能代替完成条件")
        return Decision("skip", "来源尚未达到该事项的完成条件")
    if not evidence.id or not evidence.source_url:
        return Decision("review", "缺少可追溯的证据标识或来源链接")
    if not (0 < evidence.happened_ms <= evidence.observed_ms <= now_ms):
        return Decision("review", "证据时间缺失或先后关系异常")
    if now_ms - evidence.observed_ms > 5 * 60 * 1000:
        return Decision("review", "来源状态已过期，需要重新读取")
    if item.updated_ms <= 0 or evidence.happened_ms <= item.updated_ms:
        return Decision("review", "事项被修改后未出现新的完成证据，保留手动状态")
    return Decision("complete", "关联来源明确完成，且满足该事项预设条件")


def evidence_key(item: Item, evidence: Evidence) -> str:
    # Excludes record revision: manually reopening a row must not replay old evidence.
    raw = "\0".join((item.id, evidence.source_id, evidence.kind, evidence.id))
    return "complete:" + sha256(raw.encode()).hexdigest()


class Gateway(Protocol):
    def get_item(self, item_id: str) -> Item: ...
    def get_evidence(self, item: Item) -> Evidence: ...
    def write_complete(self, item: Item, evidence: Evidence, proof_id: str) -> dict: ...


def sync_one(item: Item, evidence: Evidence, gateway: Gateway, state, *, now_ms: int) -> dict:
    """Caller holds exclusive_lock. A write receipt alone never means checked.

    Uncertain writes stay pending and are reconciled only by reading a matching
    remote proof. Bitable lacks compare-and-swap; re-read reduces but cannot
    eliminate the tiny concurrent-edit window. Never blindly retry an ambiguous write.
    """
    clock = now_ms if callable(now_ms) else lambda: now_ms
    decision = decide(item, evidence, now_ms=clock())
    result = {"item_id": item.id, "title": item.title, "checked": False,
              "status": decision.action, "reason": decision.reason}
    key = evidence_key(item, evidence)
    previous = state.operation(key)
    if decision.action != "complete" and not previous:
        return result
    current = gateway.get_item(item.id)
    if previous:
        if current.status == "完成" and current.proof_id == key:
            if previous["status"] != "done":
                state.confirm(key, {"verified_by_readback": True, "item_id": item.id})
            return {**result, "status": "already_synced", "checked": True, "reason": "已同步，不重复写入"}
        if previous["status"] != "not_sent":
            return {**result, "status": "review", "reason": "旧证据已有写入记录，保留当前状态，等待核对"}
    if decision.action != "complete":
        return result
    if current != item:
        return {**result, "status": "review", "reason": "事项刚被修改，本次不覆盖"}
    # Read the authoritative source again immediately before writing, not event text.
    fresh = gateway.get_evidence(current)
    if (fresh.id, fresh.source_id, fresh.kind, fresh.outcome, fresh.happened_ms) != (
            evidence.id, evidence.source_id, evidence.kind, evidence.outcome, evidence.happened_ms):
        return {**result, "status": "review", "reason": "来源状态已变化，本次不写入"}
    if decide(current, fresh, now_ms=clock()).action != "complete":
        return {**result, "status": "review", "reason": "重新读取后证据未通过检查"}
    state.once(key, lambda: gateway.write_complete(current, fresh, key))
    after = gateway.get_item(item.id)
    if after.status != "完成" or after.proof_id != key:
        return {**result, "status": "pending_verification", "reason": "写入已返回，尚未读到一致状态；不显示完成"}
    return {**result, "status": "synced", "checked": True,
            "reason": "事项状态和完成证据已读回确认", "evidence_url": fresh.source_url}
