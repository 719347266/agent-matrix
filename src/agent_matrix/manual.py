"""An item the owner adds by hand in 心响: no meeting, chat or task tool behind it.

Works the same whether items are kept in the Feishu table, in Notion or only on this computer.
"""
from datetime import datetime
from uuid import uuid4

from .config import load
from .feishu import Feishu
from .itemstore import enabled
from .models import MatrixError
from .snapshot import save_snapshot
from .state import State, exclusive_lock
from .timeutil import TZ, parse_date
from .workflow import Gateway


def add(root, title, who="我", due="", note=""):
    """Create the item and return its number. who: "我" (the owner does it) or "AI" (the AI starts on it)."""
    title = " ".join(str(title or "").split())[:60]
    note = str(note or "").strip()[:1000]
    if not title:
        raise MatrixError("写一下这件事是什么。")
    if who not in ("我", "AI"):
        raise MatrixError("谁来做：选「我」或「AI」。")
    now = datetime.now(TZ)
    fields = {"标题": title, "类型": ["临时"], "执行方": [who], "状态": ["未开始"], "进度": "0/1 未开始", "下一步动作": note or title}
    due = str(due or "").strip()
    if due:
        day = parse_date(due, now.date())
        if not day:
            raise MatrixError("日期没看懂。可以写：明天、周五、11-25。")
        fields["截止日"] = fields["首次截止日"] = day.strftime("%Y-%m-%d 00:00")
    config = load(root / "config.yaml")
    if config.owner_open_id:
        fields["负责人"] = [{"id": config.owner_open_id}]
    client = Feishu(config, root)
    with exclusive_lock(root / "state", wait=30):
        state = State(root / "state/state.db")
        try:
            if not enabled(config):  # items live in the Feishu table itself: it must be reachable
                client.verify_owner(client.auth())
            gateway = Gateway(client, state, on_rows=lambda rows: save_snapshot(root, rows))
            record_id = gateway.create(fields, "manual:" + uuid4().hex, "manual")["record_id_list"][0]
            row = next((r for r in gateway.rows() if r["record_id"] == record_id), {})
        finally:
            state.close()
    if who == "AI":
        from .workenv import kick
        kick(root, config, force=True)
    return {"n": int(str(row.get("编号") or 0) or 0), "record_id": record_id}
