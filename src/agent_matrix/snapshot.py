"""The items as the pages show them: from the local store when it is on, else the last table read
(state/tasks.json, written by the listener)."""
from datetime import datetime
import json
from pathlib import Path

from . import itemstore
from .config import load
from .itemstore import enabled
from .timeutil import TZ
from .values import draft_url

SNAPSHOT_FIELDS = ["编号", "标题", "执行方", "状态", "进度", "卡点", "截止日", "交付物链接", "验收结果",
                   "退回意见", "排除本人提醒", "更新时间", "预做结果摘要", "交付情况", "完成时间",
                   # for the 心响 item page: where it came from and what was asked
                   "原话引用", "来源", "来源链接", "完成标准", "补充材料", "下一步动作", "创建时间", "关联任务链接"]


_STORE = {}


def _local_store(root):
    """Is `item_store: local` on? (config.yaml re-read only when it changes)"""
    path = Path(root) / "config.yaml"
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return False
    if _STORE.get(path) is None or _STORE[path][0] != stamp:
        try:
            _STORE[path] = (stamp, enabled(load(path)))
        except Exception:
            _STORE[path] = (stamp, False)
    return _STORE[path][1]


def save_snapshot(root, rows):
    path = Path(root) / "state/tasks.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"at": datetime.now(TZ).isoformat(timespec="seconds"), "rows": snapshot_rows(rows)},
                              ensure_ascii=False, default=str))
    tmp.replace(path)


def snapshot_rows(rows):
    def one(v):
        if isinstance(v, list) and not v:
            return None  # an empty select reads back as []
        return v[0] if isinstance(v, list) and len(v) == 1 and isinstance(v[0], str) else v
    out = []
    for r in rows:
        item = {k: one(r.get(k)) for k in SNAPSHOT_FIELDS}
        item["交付物链接"] = draft_url(r.get("交付物链接"))
        item["record_id"] = r.get("record_id")
        item["父事项"] = [x.get("id") or x.get("record_id") for x in r.get("父事项") or [] if isinstance(x, dict)]
        out.append(item)
    return out


def _snapshot(root):
    """The items as the pages show them: straight from the local store when it is on, else the last table read."""
    if _local_store(root):
        if itemstore.imported(root):
            return {"at": datetime.now(TZ).isoformat(timespec="seconds"), "rows": snapshot_rows(itemstore.rows(root))}
    try:
        return json.loads((Path(root) / "state/tasks.json").read_text())
    except (OSError, ValueError):
        return {"at": None, "rows": []}
