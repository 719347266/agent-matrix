"""Project → version → items: work kept the way the owner's team plans it ("2.3 版本", "11 月迭代").

A version belongs to a project and has a status: 当前 (current), 下一版 (next), 规划中 (planned), 已完成 (done).
Items sit in one version; each version also keeps what is expected to come up in it (预计会遇到的事). The AI that
drives 心响 proposes all of this from the owner's project table and items (drive: kind "organize"); the owner's own
placing always wins over the AI's. Kept on this computer only (state/xinxiang.db).
"""
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re

from . import db as database
from . import projects as project_store
from .timeutil import TZ

STATUS = {"current": "当前", "next": "下一版", "planned": "规划中", "done": "已完成"}
ORDER = ["current", "next", "planned", "done"]
OWNER, AI = "本人", "AI"


def _db(root):
    return database.open(root, "xinxiang")


def _now():
    return datetime.now(TZ).isoformat(timespec="seconds")


def status_of(text):
    """"当前" / "current" / "进行中" → current … anything unknown is planned."""
    text = str(text or "").strip().lower()
    for key, label in STATUS.items():
        if text in (key, label):
            return key
    return {"进行中": "current", "开发中": "current", "已上线": "done", "已发布": "done", "完成": "done", "下个版本": "next", "下一个": "next"}.get(text, "planned")


def upsert(root, project, name, status="planned", period="", note="", source=AI):
    """A version of a project (the project is made if it is new). Returns its id."""
    name = " ".join(str(name or "").split())[:40]
    if not name:
        return None
    pid = project if isinstance(project, int) else project_store.create(root, project)
    db = _db(root)
    try:
        with db:
            db.execute("INSERT INTO versions (project_id, name, status, period, note, source, created) VALUES (?,?,?,?,?,?,?) "
                       "ON CONFLICT(project_id, name) DO UPDATE SET status=excluded.status, "
                       "period=coalesce(nullif(excluded.period,''), period), note=coalesce(nullif(excluded.note,''), note)",
                       (pid, name, status_of(status), str(period or "")[:40], str(note or "")[:200], source, _now()))
            if status_of(status) == "current":   # one current version per project
                db.execute("UPDATE versions SET status='done' WHERE project_id=? AND name<>? AND status='current'", (pid, name))
            return db.execute("SELECT id FROM versions WHERE project_id=? AND name=?", (pid, name)).fetchone()[0]
    finally:
        db.close()


def place(root, record_id, version_id, by=AI):
    """Put an item in a version. What the owner placed is not moved by the AI."""
    db = _db(root)
    try:
        had = db.execute("SELECT by FROM item_versions WHERE record_id=?", (record_id,)).fetchone()
        if had and had[0] == OWNER and by != OWNER:
            return False
        with db:
            if version_id:
                db.execute("INSERT INTO item_versions (record_id, version_id, by, created) VALUES (?,?,?,?) ON CONFLICT(record_id) "
                           "DO UPDATE SET version_id=excluded.version_id, by=excluded.by", (record_id, int(version_id), by, _now()))
                pid = db.execute("SELECT project_id FROM versions WHERE id=?", (int(version_id),)).fetchone()
                if pid and (by == OWNER or not db.execute("SELECT 1 FROM item_projects WHERE record_id=?", (record_id,)).fetchone()):
                    db.execute("INSERT INTO item_projects (record_id, project_id, created) VALUES (?,?,?) ON CONFLICT(record_id) "
                               "DO UPDATE SET project_id=excluded.project_id", (record_id, pid[0], _now()))
            else:
                db.execute("DELETE FROM item_versions WHERE record_id=?", (record_id,))
        return True
    finally:
        db.close()


def due_date(text, today=None):
    """"2026-11-25" / "11-25" / "11月25日" → a date (no year: this year, or next year if that is long past). Else None."""
    text = str(text or "").strip()
    today = today or datetime.now(TZ).date()
    try:
        if m := re.match(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", text):
            return date(int(m[1]), int(m[2]), int(m[3]))
        if m := re.match(r"(\d{1,2})[-/.月](\d{1,2})", text):
            day = date(today.year, int(m[1]), int(m[2]))
            return day.replace(year=today.year + 1) if (today - day).days > 180 else day
    except ValueError:
        return None
    return None


def expect(root, version_id, title, why="", due=""):
    """Something expected to come up in a version. Said again: a new reason or date fills in what was missing
    or out of date; whether it is still open is left alone."""
    title = " ".join(str(title or "").split())[:80]
    if not title or not version_id:
        return
    day = due_date(due)
    due = day.isoformat() if day else str(due or "").strip()[:20]
    db = _db(root)
    try:
        with db:
            db.execute("INSERT INTO expected (version_id, title, why, due, created) VALUES (?,?,?,?,?) ON CONFLICT(version_id, title) "
                       "DO UPDATE SET why=coalesce(nullif(excluded.why,''), why), due=coalesce(nullif(excluded.due,''), due)",
                       (int(version_id), title, str(why or "")[:200], due, _now()))
    finally:
        db.close()


def expected_due(root, today=None):
    """Open expected things whose date is tomorrow or today: one message in 心响 the day before and one on the day.
    Each (thing, day) is noted in meta once told, so running this often still tells each only once a day."""
    from . import inbox
    today = today or datetime.now(TZ).date()
    db = _db(root)
    try:
        rows = db.execute("SELECT e.id, e.title, e.why, e.due, v.name, p.name FROM expected e JOIN versions v ON v.id=e.version_id "
                          "JOIN projects p ON p.id=v.project_id WHERE e.status='open' AND coalesce(e.due,'')<>''").fetchall()
        told = []
        for eid, title, why, due, version, project in rows:
            day = due_date(due, today)
            if day not in (today, today + timedelta(days=1)):
                continue
            key = f"expected-due:{eid}:{today.isoformat()}"
            if db.execute("SELECT 1 FROM meta WHERE key=?", (key,)).fetchone():
                continue
            when = "今天" if day == today else "明天"
            inbox.add(root, key,
                      f"预计会遇到的事{when}到期：{title}\n{project} · {version}，{day.month} 月 {day.day} 日" + (f"\n为什么：{why}" if why else ""),
                      kind="system")
            with db:
                db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?,?)", (key, _now()))
            told.append(eid)
        return told
    finally:
        db.close()


def set_expected(root, expected_id, status):
    db = _db(root)
    try:
        with db:
            db.execute("UPDATE expected SET status=? WHERE id=?", (status if status in ("open", "done", "dropped") else "open", int(expected_id)))
    finally:
        db.close()


def all(root):
    """Every project's versions, newest plan first, each with its items and what is expected in it."""
    db = _db(root)
    try:
        names = {pid: (name, color) for pid, name, color in db.execute("SELECT id, name, color FROM projects WHERE active=1")}
        items = {}
        for rid, vid, by in db.execute("SELECT record_id, version_id, by FROM item_versions"):
            items.setdefault(vid, []).append({"rid": rid, "by": by})
        exp = {}
        for eid, vid, title, why, due, status in db.execute("SELECT id, version_id, title, why, due, status FROM expected ORDER BY id"):
            exp.setdefault(vid, []).append({"id": eid, "title": title, "why": why or "", "due": due or "", "status": status})
        out = []
        for vid, pid, name, status, period, note in db.execute("SELECT id, project_id, name, status, period, note FROM versions ORDER BY id DESC"):
            if pid not in names:
                continue
            out.append({"id": vid, "project_id": pid, "project": names[pid][0], "color": names[pid][1], "name": name, "status": status,
                        "status_label": STATUS.get(status, status), "period": period or "", "note": note or "",
                        "items": items.get(vid, []), "expected": exp.get(vid, [])})
        out.sort(key=lambda v: (v["project_id"], ORDER.index(v["status"]) if v["status"] in ORDER else 9))
        return out
    finally:
        db.close()


def of(root, record_id):
    db = _db(root)
    try:
        row = db.execute("SELECT v.id, v.name, v.status, p.name FROM item_versions iv JOIN versions v ON v.id=iv.version_id "
                         "JOIN projects p ON p.id=v.project_id WHERE iv.record_id=?", (record_id,)).fetchone()
        return {"id": row[0], "name": row[1], "status": row[2], "project": row[3]} if row else None
    finally:
        db.close()


def context_text(root, record_id, rows_by_rid=None, limit=12):
    """For the AI doing one item: which version it belongs to, what else is in that version and what is expected."""
    me = of(root, record_id)
    if not me:
        return ""
    version = next((v for v in all(root) if v["id"] == me["id"]), None)
    rows_by_rid = rows_by_rid or {}
    others = [x["rid"] for x in (version or {}).get("items", []) if x["rid"] != record_id][:limit]
    lines = [f"## 这件事属于：{me['project']} · {me['name']}（{STATUS.get(me['status'], me['status'])}）",
             "做的时候要和这个版本的其他事对得上：口径、命名、范围一致；不要做进下个版本的东西。"]
    if version and version.get("note"):
        lines.append(f"版本说明：{version['note']}")
    if others:
        lines.append("同一版本的其他事：")
        for rid in others:
            row = rows_by_rid.get(rid) or {}
            title = row.get("标题") or rid
            state = row.get("状态") or ""
            state = state[0] if isinstance(state, list) and state else state
            lines.append(f"- {title}{'（' + state + '）' if state else ''}")
    open_exp = [e for e in (version or {}).get("expected", []) if e["status"] == "open"]
    if open_exp:
        lines.append("这个版本预计还会遇到：")
        lines += [f"- {e['title']}{'（' + e['why'] + '）' if e['why'] else ''}" for e in open_exp[:6]]
    return "\n".join(lines) + "\n\n"


# ---- asking the AI to sort it all out ------------------------------------------------------------------------

def _ask_path(root):
    return Path(root) / "state" / "organize_ask.json"


def ask_organize(root):
    path = _ask_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"at": _now()}), encoding="utf-8")


def organize_asked(root):
    try:
        return json.loads(_ask_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def organize_done(root):
    _ask_path(root).unlink(missing_ok=True)
