"""Where each item came from and who it is about, for the page: 「新进来」 groups new items by the meeting, chat or
task list they came from, and 「事项 → 按人」 lists them by person, clients apart from colleagues.

    origin(...)   {"kind": "会议"/"聊天"/"飞书任务"/…, "group": the meeting topic or chat name, "person": the other
                  person or None, "client": a guess, "at": when it was said}
    set_kind      the owner says a person (or a group) is a client or a colleague; that wins over the guess

The guess, in code, no model: a Feishu chat marked external, and WeChat, are clients; everything else is a colleague.
Nothing here is written by the AI.
"""
from pathlib import Path
import json
import re

from . import db as database

QUOTE = re.compile(r"^\[(?P<chat>.*?)\s+(?P<at>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?\S*)\s+(?P<who>[^\]]*)\]")
KINDS = ("客户", "同事")
SETTING = "people:kind"


def _one(value):
    return (value[0] if value else "") if isinstance(value, list) else (value or "")


def known(root):
    """What the page needs from the local records, read once per page load: meeting topics by meeting id, and what
    心响 learned about each chat (its name, p2p or group, external) by chat id."""
    meetings, chats, external = {}, {}, set()
    try:
        db = database.read(Path(root) / "state/state.db")
    except Exception:
        return meetings, chats
    try:
        for mid, meta in db.execute("SELECT meeting_id, metadata FROM meeting_queue"):
            try:
                m = json.loads(meta or "{}")
            except ValueError:
                continue
            topic = re.sub(r"^\d{1,2}-\d{1,2}\s*\|\s*", "", str(m.get("topic") or "")).strip()   # "11-25 | 周会" → "周会"
            meetings[str(mid)] = {"topic": topic, "at": str(m.get("start_time") or "")[:16]}
        for cid, name, mode in db.execute("SELECT chat_id, chat_name, chat_mode FROM chat_candidates GROUP BY chat_id"):
            chats[cid] = {"name": name or "", "mode": mode or ""}
        row = db.execute("SELECT value FROM kv WHERE key='chats:external'").fetchone()
        external = set(json.loads(row[0])) if row else set()
    except Exception:
        pass
    finally:
        db.close()
    for cid in external:
        chats.setdefault(cid, {"name": "", "mode": ""})["external"] = True
    return meetings, chats


def _param(url, key):
    m = re.search(rf"[?&]{key}=([^&#\s]+)", str(url or ""))
    return m.group(1) if m else ""


def origin(row, url, meetings, chats, self_names, tags=None):
    """Where one item came from (row: the item record, url: its source link already cleaned)."""
    kind = _one(row.get("来源")) or "手动"
    tags = tags or {}
    out = {"kind": kind, "group": kind, "person": None, "client": False, "at": str(row.get("创建时间") or "")[:16].replace("T", " ")}
    if kind == "会议":
        m = meetings.get(_param(url, "meetingId")) or {}
        if m.get("topic"):
            out.update(group=m["topic"], at=m.get("at") or out["at"])
    elif kind == "聊天":
        q = QUOTE.match(str(row.get("原话引用") or ""))
        chat = chats.get(_param(url, "openChatId")) or {}
        name = (q.group("chat") if q else "") or chat.get("name") or ""
        who = (q.group("who").strip() if q else "") or ""
        wechat = not url   # a WeChat message has no link to open
        slack = "slack.com" in str(url or "")
        p2p = chat.get("mode") == "p2p" or (wechat and name and name == who)
        mine = who in self_names
        person = (name if mine else who) if p2p else (None if mine else who)
        out.update(group=name or ("微信" if wechat else "Slack" if slack else "聊天"), person=person or None,
                   client=bool(chat.get("external")) or wechat, at=(q.group("at")[:16].replace("T", " ") if q else out["at"]))
    for key in (out["person"], out["group"]):   # the owner's word beats the guess
        if key and tags.get(key) in KINDS:
            out["client"] = tags[key] == "客户"
            break
    return out


def tags(root):
    from .thread import setting
    value = setting(Path(root), SETTING, {}) or {}
    return value if isinstance(value, dict) else {}


def set_kind(root, name, kind):
    """The owner says who this is: 客户 or 同事. Empty kind forgets it and the guess applies again."""
    from .thread import set_setting
    name = " ".join(str(name or "").split())[:60]
    if not name:
        raise ValueError("要写是谁。")
    if kind not in (*KINDS, ""):
        raise ValueError("只能标成客户或同事。")
    value = tags(root)
    if kind:
        value[name] = kind
    else:
        value.pop(name, None)
    set_setting(Path(root), SETTING, value)
    return value
