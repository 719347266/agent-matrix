"""Items the owner strung together: "this one comes after that one".

One row per link, earlier item → later item (state/xinxiang.db, table item_links). Kept on this computer
only; the Feishu table is not touched. Whoever works on an item is shown what came before it and what it
feeds into (background.linked_items).
"""
from datetime import datetime

from . import db as database
from .models import MatrixError
from .timeutil import TZ


def _db(root):
    return database.open(root, "xinxiang")


def link(root, earlier, later, by="本人"):
    """`later` continues `earlier`. Linking the same two the other way round replaces the old direction."""
    if not earlier or not later or earlier == later:
        raise MatrixError("要串起来的得是两件不同的事。")
    others = [e for e in pairs(root) if e != (later, earlier)]  # turning one link round is fine; a longer loop is not
    if later in [r for r, _ in _walk(root, earlier, "up", 50, others)]:
        raise MatrixError("这样串会绕成一个圈：后面那件事已经排在前面了。")
    db = _db(root)
    try:
        with db:
            db.execute("DELETE FROM item_links WHERE earlier=? AND later=?", (later, earlier))
            db.execute("INSERT OR IGNORE INTO item_links (earlier, later, created, by) VALUES (?,?,?,?)",
                       (earlier, later, datetime.now(TZ).isoformat(timespec="seconds"), by))
    finally:
        db.close()


def unlink(root, a, b):
    db = _db(root)
    try:
        with db:
            db.execute("DELETE FROM item_links WHERE (earlier=? AND later=?) OR (earlier=? AND later=?)", (a, b, b, a))
    finally:
        db.close()


def pairs(root):
    db = _db(root)
    try:
        return db.execute("SELECT earlier, later FROM item_links ORDER BY created").fetchall()
    finally:
        db.close()


def _walk(root, record_id, direction, limit, edges=None):
    """[(record_id, distance)] going up (what came before) or down (what follows), nearest first."""
    edges = pairs(root) if edges is None else edges
    seen, out, frontier, depth = {record_id}, [], [record_id], 0
    while frontier and len(out) < limit:
        depth += 1
        step = []
        for a, b in edges:
            near, far = (b, a) if direction == "up" else (a, b)
            if near in frontier and far not in seen:
                seen.add(far)
                step.append(far)
                out.append((far, depth))
        frontier = step
    return out[:limit]


def upstream(root, record_id, limit=6):
    """Everything this item builds on, nearest first (the whole line, not only the direct one)."""
    return [r for r, _ in _walk(root, record_id, "up", limit)]


def downstream(root, record_id, limit=6):
    return [r for r, _ in _walk(root, record_id, "down", limit)]


def direct(root, record_id):
    """{"before": [...], "after": [...]}: the items linked to this one directly."""
    edges = pairs(root)
    return {"before": [a for a, b in edges if b == record_id], "after": [b for a, b in edges if a == record_id]}
