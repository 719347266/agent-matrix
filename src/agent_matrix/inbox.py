"""Local inbox: every message the system used to send on Feishu lands here instead.

Feishu stays the database (the task table) and a read-only source (meetings, chats, tasks);
nothing is sent there. Briefs, meeting prep, new items, AI results, blockers, the Friday review
and system alerts all become inbox messages, shown by the local client (app.py) and announced
with a system notification by the menu bar app / tray.

Messages are stored in state/inbox.db (its own SQLite file, so writing never waits for the
project lock). The same key is stored once, so retries and two schedulers never duplicate a
message. Card JSON is converted into plain blocks: sections → rows → actions.
"""
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3

from .timeutil import TZ


def _db(root):
    path = Path(root) / "state/inbox.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE,
        at TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
        blocks TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0)""")
    return db


# ---- turning what used to be a Feishu message into blocks --------------------------------------------

def _plain(text):
    text = re.sub(r"<[^>]+>", "", str(text or ""))
    return re.sub(r"\*\*(.*?)\*\*", r"\1", text).strip()


LINK = re.compile(r"\[([^\]]*)\]\((https?://[^\s)]+)\)")


def _refs(text):
    return [int(x) for x in dict.fromkeys(re.findall(r"#(\d{1,5})", text or ""))]


def from_text(text):
    lines = [x for x in str(text or "").strip().splitlines()]
    title = (lines[0] if lines else "消息").strip("【】 ")[:80]
    body = "\n".join(lines[1:]).strip()
    blocks = [{"type": "text", "text": body}] if body else []
    return title, blocks, _refs(text)


def _row(column_set):
    """One card row: bold head, grey hint, then「打开」links (a button or a plain link) and callback buttons."""
    head, hint, url, actions = "", "", None, []
    for col in column_set.get("columns") or []:
        for el in col.get("elements") or []:
            if el.get("tag") == "markdown":
                content = str(el.get("content") or "")
                link = LINK.search(content)  # read-only cards carry the document as a plain link
                url = url or (link.group(2) if link else None)
                text = _plain(LINK.sub("", content)).strip(" ·")
                if not text:
                    continue
                if not head:
                    head = text
                else:
                    hint = (hint + " · " if hint else "") + text
            elif el.get("tag") == "button":
                label = _plain((el.get("text") or {}).get("content"))
                for b in el.get("behaviors") or []:
                    if b.get("type") == "open_url":
                        url = b.get("default_url") or url
                    elif b.get("type") == "callback" and (b.get("value") or {}).get("cmd"):
                        actions.append({"label": label.lstrip("✓ ").strip() or "完成", "cmd": b["value"]["cmd"]})
    return {"head": head, "hint": hint, "url": url, "actions": actions, "refs": _refs(head)}


def from_card(card):
    header = (card.get("header") or {})
    title = _plain(((header.get("title") or {}).get("content")) or "消息")
    subtitle = _plain(((header.get("subtitle") or {}).get("content")) or "")
    blocks = [{"type": "text", "text": subtitle}] if subtitle else []
    refs = []
    for el in ((card.get("body") or {}).get("elements") or card.get("elements") or []):
        if el.get("element_id") == "footer":  # a note for the Feishu reader only (see brief_v1.card)
            continue
        tag = el.get("tag")
        if tag == "interactive_container":
            section = {"type": "section", "title": "", "rows": [], "more": ""}
            for inner in el.get("elements") or []:
                if inner.get("tag") == "markdown":
                    text = _plain(inner.get("content"))
                    if not section["title"] and not section["rows"]:
                        section["title"] = text
                    elif text:  # notes under the rows (meetings, what was decided, "还有 N 件"): keep every line
                        section["more"] = (section["more"] + "\n" if section["more"] else "") + text
                elif inner.get("tag") == "column_set":
                    row = _row(inner)
                    section["rows"].append(row)
                    refs += row["refs"]
            blocks.append(section)
        elif tag == "markdown":
            blocks.append({"type": "text", "text": _plain(el.get("content"))})
        elif tag == "column_set":
            row = _row(el)
            if row["actions"] or row["refs"]:
                blocks.append({"type": "section", "title": "", "rows": [row], "more": ""})
            elif row["head"]:
                blocks.append({"type": "hint", "text": row["head"] + (" · " + row["hint"] if row["hint"] else "")})
    return title, blocks, list(dict.fromkeys(refs))


KINDS = [("brief", r"早报|晚报|明日计划|今日计划"), ("prep", r"会前|开会前|会议准备"), ("done", r"做完了|请你验收|已交付"),
         ("blocked", r"卡住"), ("new", r"新增|聊天 ·|新待办"), ("weekly", r"复盘|本周"), ("system", r"⚠|测试|监控|AI 员工已经"),
         ("reply", r"→|✓")]


def kind_of(title, text=""):
    for kind, pattern in KINDS:
        if re.search(pattern, title or ""):
            return kind
    for kind, pattern in KINDS:
        if re.search(pattern, text or ""):
            return kind
    return "other"


# ---- reading and writing ----------------------------------------------------------------------------

def add(root, key, content, *, kind=None):
    """Store one message (text or card JSON) once per key; returns a receipt like a sent message."""
    if isinstance(content, dict):
        title, blocks, refs = from_card(content)
        flat = json.dumps(content, ensure_ascii=False)
    else:
        title, blocks, refs = from_text(content)
        flat = str(content)
    summary = " ".join(b.get("text", "") for b in blocks if b.get("type") == "text")[:120]
    kind = kind or kind_of(title, flat)
    db = _db(root)
    try:
        with db:
            db.execute("INSERT OR IGNORE INTO messages (key, at, kind, title, summary, blocks) VALUES (?,?,?,?,?,?)",
                       (key, datetime.now(TZ).isoformat(timespec="seconds"), kind, title, summary,
                        json.dumps({"blocks": blocks, "refs": refs}, ensure_ascii=False)))
        mid = db.execute("SELECT id FROM messages WHERE key=?", (key,)).fetchone()[0]
    finally:
        db.close()
    return {"message_id": f"local_{mid}", "channel": "local", "title": title}


def messages(root, limit=100, unread_only=False):
    db = _db(root)
    try:
        rows = db.execute("SELECT id, at, kind, title, summary, blocks, read FROM messages WHERE key!='history-imported' " +
                          ("AND read=0 " if unread_only else "") + "ORDER BY at DESC, id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        db.close()
    out = []
    for mid, at, kind, title, summary, blocks, read in rows:
        data = json.loads(blocks)
        out.append({"id": mid, "at": at, "time": at[11:16], "day": at[:10], "kind": kind, "title": title,
                    "summary": summary, "blocks": data.get("blocks", []), "refs": data.get("refs", []), "read": bool(read)})
    return out


def unread(root):
    db = _db(root)
    try:
        return db.execute("SELECT count(*) FROM messages WHERE read=0").fetchone()[0]
    finally:
        db.close()


def mark_read(root, ids=None):
    db = _db(root)
    try:
        with db:
            if ids is None:
                db.execute("UPDATE messages SET read=1 WHERE read=0")
            else:
                db.executemany("UPDATE messages SET read=1 WHERE id=?", [(int(i),) for i in ids])
    finally:
        db.close()


def import_history(root, days=3):
    """Once: messages already sent on Feishu before the local inbox existed, so history continues (marked read)."""
    from datetime import timedelta
    db = _db(root)
    try:
        if db.execute("SELECT 1 FROM messages WHERE key='history-imported'").fetchone():
            return 0
    finally:
        db.close()
    since = (datetime.now(TZ) - timedelta(days=days)).isoformat()
    count = 0
    try:
        adb = sqlite3.connect(Path(root) / "state/activity.db", timeout=10)
        rows = adb.execute("SELECT id, at, title, detail FROM events WHERE kind='send' AND status='ok' AND at>=? ORDER BY id",
                           (since,)).fetchall()
        adb.close()
    except sqlite3.Error:
        rows = []
    db = _db(root)
    try:
        with db:
            for eid, at, title, detail in rows:
                text = (title or "") + ("\n" + detail if detail and not str(detail).startswith("{") else "")
                t, blocks, refs = from_text(text)
                cur = db.execute("INSERT OR IGNORE INTO messages (key, at, kind, title, summary, blocks, read) VALUES (?,?,?,?,?,?,1)",
                                 (f"activity:{eid}", at, kind_of(t, text), t,
                                  " ".join(b.get("text", "") for b in blocks)[:120], json.dumps({"blocks": blocks, "refs": refs}, ensure_ascii=False)))
                count += cur.rowcount
            db.execute("INSERT OR IGNORE INTO messages (key, at, kind, title, blocks, read) VALUES ('history-imported', ?, 'system', '', '{}', 1)",
                       (datetime.now(TZ).isoformat(),))
    finally:
        db.close()
    return count
