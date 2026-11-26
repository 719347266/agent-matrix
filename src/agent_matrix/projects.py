"""Projects: the owner's own way of sorting items ("数据看板", "2.3 版本").

Kept on this computer only (state/xinxiang.db: projects, item_projects); the Feishu table is not touched.
An item is in the project the owner put it in. One the owner has not placed is guessed, never written:
it follows the items it is strung to, or it goes where its title clearly belongs. A guess is shown as a
guess, so a wrong one is one click to fix, and every placement the owner makes sharpens the next guess.
"""
from datetime import datetime

from . import db as database
from . import itemlinks
from .models import MatrixError
from .textmatch import STRONG, similarity
from .timeutil import TZ

COLORS = ["#5856d6", "#ff9500", "#34c759", "#ff2d55", "#00c7be", "#af52de", "#007aff", "#ffcc00"]
OWNER, GUESS = "本人", "猜"


def _db(root):
    return database.open(root, "xinxiang")


def _now():
    return datetime.now(TZ).isoformat(timespec="seconds")


def _name(name):
    name = " ".join(str(name or "").split())[:20]
    if not name:
        raise MatrixError("项目要有个名字。")
    return name


def all(root):
    db = _db(root)
    try:
        return [{"id": i, "name": n, "color": c} for i, n, c in
                db.execute("SELECT id, name, color FROM projects WHERE active=1 ORDER BY id")]
    finally:
        db.close()


def create(root, name):
    """A new project; a name used before comes back as the same project."""
    name = _name(name)
    db = _db(root)
    try:
        with db:
            used = db.execute("SELECT count(*) FROM projects").fetchone()[0]
            db.execute("INSERT INTO projects (name, color, created) VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET active=1",
                       (name, COLORS[used % len(COLORS)], _now()))
            return db.execute("SELECT id FROM projects WHERE name=?", (name,)).fetchone()[0]
    finally:
        db.close()


def rename(root, project_id, name):
    name = _name(name)
    db = _db(root)
    try:
        with db:
            if db.execute("SELECT 1 FROM projects WHERE name=? AND id<>?", (name, int(project_id))).fetchone():
                raise MatrixError("已经有一个叫这个名字的项目。")
            db.execute("UPDATE projects SET name=? WHERE id=?", (name, int(project_id)))
    finally:
        db.close()


def remove(root, project_id):
    """The project goes; its items are simply unsorted again."""
    db = _db(root)
    try:
        with db:
            db.execute("UPDATE projects SET active=0 WHERE id=?", (int(project_id),))
            db.execute("DELETE FROM item_projects WHERE project_id=?", (int(project_id),))
    finally:
        db.close()


def assign(root, record_ids, project_id):
    """The owner puts items in a project. 0 means "none of them", which is also a choice and is not guessed over."""
    project_id = int(project_id or 0)
    db = _db(root)
    try:
        with db:
            if project_id and not db.execute("SELECT 1 FROM projects WHERE id=? AND active=1", (project_id,)).fetchone():
                raise MatrixError("找不到这个项目。")
            for rid in record_ids:
                db.execute("INSERT INTO item_projects (record_id, project_id, created) VALUES (?,?,?) "
                           "ON CONFLICT(record_id) DO UPDATE SET project_id=excluded.project_id, created=excluded.created",
                           (rid, project_id, _now()))
    finally:
        db.close()


def find(root, name):
    """An existing project by the name an AI wrote: exact, or one name inside the other ("示例" ↔ "示例 App"). None if none."""
    name = " ".join(str(name or "").split()).lower()
    if not name:
        return None
    live = all(root)
    exact = next((p for p in live if p["name"].lower() == name), None)
    if exact:
        return exact
    near = [p for p in live if name in p["name"].lower() or p["name"].lower() in name]
    return near[0] if len(near) == 1 else None


def chosen(root):
    """{record_id: project_id} as the owner placed them (0 = the owner said none)."""
    db = _db(root)
    try:
        return dict(db.execute("SELECT record_id, project_id FROM item_projects"))
    finally:
        db.close()


def place(root, rows):
    """{record_id: (project_id, OWNER | GUESS)} for every row that has a project. `rows` are table rows."""
    live = {p["id"]: p["name"] for p in all(root)}
    if not live:
        return {}
    mine = {rid: pid for rid, pid in chosen(root).items()}
    title = {r.get("record_id"): r.get("标题") or "" for r in rows}
    members = {}
    for rid, pid in mine.items():
        if pid in live and rid in title:
            members.setdefault(pid, []).append(title[rid])
    edges = itemlinks.pairs(root)
    out = {rid: (pid, OWNER) for rid, pid in mine.items() if pid in live and rid in title}
    for rid, text in title.items():
        if rid in mine:  # placed, or the owner said it belongs to none
            continue
        line = {rid} | set(itemlinks.upstream(root, rid, 20) if edges else []) | set(itemlinks.downstream(root, rid, 20) if edges else [])
        near = next((mine[x] for x in line if mine.get(x) in live), None)  # strung to an item that has a project
        if near:
            out[rid] = (near, GUESS)
            continue
        score, pid = max(((max([similarity(text, name)] + [similarity(text, t) for t in members.get(p, [])]), p)
                          for p, name in live.items()), default=(0, None))
        if pid and score >= STRONG:
            out[rid] = (pid, GUESS)
    return out
