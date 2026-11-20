"""The owner tidies an item's deliverables by hand: add one, point one at another link, reword it, take it away.

The AI writes the link when it delivers; the owner often ends up with a document of their own. Whatever the
versions say afterwards, the item's own 交付物链接 follows the newest one, so every place that opens "the
deliverable" opens the right thing.
"""
from uuid import uuid4

from . import thread
from .config import load
from .feishu import Feishu
from .itemstore import enabled
from .models import MatrixError
from .snapshot import _snapshot, save_snapshot
from .state import State, exclusive_lock
from .values import draft_url
from .workflow import Gateway


def _url(value):
    url = draft_url(str(value or "").strip())
    if not url:
        raise MatrixError("这不是一个能打开的链接（要以 http 开头）。")
    return url


def change(root, n, op, *, version=None, url=None, summary=None):
    """op: "add" (url, optional summary), "edit" (version, url and/or summary), "remove" (version)."""
    root = __import__("pathlib").Path(root)
    row = next((r for r in _snapshot(root).get("rows") or [] if str(r.get("编号")) == str(n)), None)
    if not row:
        raise MatrixError("找不到这件事。")
    rid = row["record_id"]
    text = None if summary is None else " ".join(str(summary).split())[:300]
    old = draft_url(row.get("交付物链接"))
    if old and op in ("add", "edit", "remove") and not (thread.history(root).get(rid) or {}).get("deliverables"):
        # delivered before versions were kept: what the page shows as v1 becomes a real v1 first
        thread.add_deliverable(root, rid, summary=str(row.get("预做结果摘要") or "交付")[:300], url=old)
    if op == "add":
        thread.add_deliverable(root, rid, summary=text or "你加的交付", url=_url(url))
    elif op == "edit":
        if not thread.edit_deliverable(root, rid, int(version or 0), url=_url(url) if url is not None else None, summary=text):
            raise MatrixError("找不到这一版交付。")
    elif op == "remove":
        if not thread.remove_deliverable(root, rid, int(version or 0)):
            raise MatrixError("找不到这一版交付。")
    else:
        raise MatrixError("不认识这个操作。")
    versions = (thread.history(root).get(rid) or {}).get("deliverables") or []
    newest = versions[-1]["url"] if versions else None
    if draft_url(row.get("交付物链接")) != newest:   # the item itself points at the newest version
        config = load(root / "config.yaml")
        client = Feishu(config, root)
        with exclusive_lock(root / "state", wait=30):
            state = State(root / "state/state.db")
            try:
                if not enabled(config):
                    client.verify_owner(client.auth())
                gateway = Gateway(client, state, on_rows=lambda rows: save_snapshot(root, rows))
                fresh = next((r for r in gateway.rows() if r["record_id"] == rid), row)
                gateway.update(fresh, {"交付物链接": newest}, "deliverable:" + uuid4().hex, "deliverable")
            finally:
                state.close()
    return {"versions": len(versions), "url": newest}
