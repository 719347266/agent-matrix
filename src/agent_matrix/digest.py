"""Merge already-scoped updates into one digest without claiming message delivery."""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json


@dataclass(frozen=True)
class Notice:
    id: str
    item_id: str
    topic: str
    text: str
    timestamp_ms: int
    source_url: str
    necessary: bool = True


def merge_notices(notices: list[Notice], allowed_item_ids: set[str]) -> list[dict]:
    # Deduplicate by stable message ID; same topic merges only within the same item.
    unique = {}
    for n in notices:
        if n.necessary and n.item_id in allowed_item_ids and n.id and n.source_url:
            if n.id not in unique or n.timestamp_ms > unique[n.id].timestamp_ms:
                unique[n.id] = n
    groups = {}
    for n in sorted(unique.values(), key=lambda n: (n.timestamp_ms, n.id)):
        key = (n.item_id, n.topic)
        groups.setdefault(key, []).append(n)
    merged = []
    for (item_id, topic), group in groups.items():
        # Retain contradictory/earlier messages as source evidence, not erase them.
        merged.append({"item_id": item_id, "topic": topic, "latest": group[-1].text,
                       "messages": [asdict(n) for n in group], "count": len(group)})
    return sorted(merged, key=lambda g: (-g["messages"][-1]["timestamp_ms"], g["item_id"], g["topic"]))


def build_digest(items, notices, results):
    rows = {i.id: i for i in items}
    verified = {r["item_id"] for r in results if r.get("checked") is True}
    return {
        "todo": [{"id": i.id, "title": i.title, "status": i.status} for i in items
                 if i.status not in {"完成", "取消"} and i.id not in verified],
        "necessary_messages": merge_notices(notices, set(rows)),
        "completed": [r for r in results if r.get("checked") is True and r["item_id"] in rows],
        "needs_confirmation": [r for r in results if r["status"] in {"review", "pending_verification"}
                               and r["item_id"] in rows],
    }


def digest_fingerprint(digest):
    return sha256(json.dumps(digest, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def delivery_plan(digest, *, window: str, previous: dict | None = None) -> dict:
    """Pure outbox plan. Save fingerprint ONLY after the sender gets a receipt."""
    fingerprint = digest_fingerprint(digest)
    key = "digest:" + window
    if not any(digest.values()):
        action = "skip_empty"
    elif previous and previous.get("window") == window and previous.get("fingerprint") == fingerprint:
        action = "skip_unchanged"
    elif previous and previous.get("window") == window and previous.get("message_id"):
        action = "update_existing"
    else:
        action = "send_once"
    return {"action": action, "idempotency_key": key, "window": window,
            "fingerprint": fingerprint, "message_id": (previous or {}).get("message_id")}


def render_text(digest, *, demo=False):
    lines = ["【模拟·合并简报】" if demo else "【工作简报】"]
    sections = [("需要你处理", digest["todo"], lambda r: "□ " + r["title"]),
                ("必要消息", digest["necessary_messages"],
                 lambda r: f"• {r['topic']}：{r['latest']}（合并 {r['count']} 条）"),
                ("已自动完成", digest["completed"], lambda r: "✓ " + r["title"]),
                ("待确认", digest["needs_confirmation"], lambda r: r["title"] + "：" + r["reason"])]
    for heading, rows, formatter in sections:
        if rows:
            lines.extend(["", heading, *[formatter(r) for r in rows]])
    if demo:
        lines.extend(["", "全部为虚构测试数据；写入发生在内存测试表中，未改变真实事项。"])
    return "\n".join(lines)
