"""心响 · 主动层：什么值得打扰本人、什么时候打扰、说什么。

Everything that reaches the owner first lands in the inbox (inbox.py). This layer decides which of
those messages are worth an interruption (an alert: system notification or 心响's own banner) and when:

    important  AI finished and waits for review · AI is stuck and asks a question · a new item that is the
               owner's to decide · something broke · (later) a waiting condition came due, a topic came back
    quiet      while the owner is in a meeting (calendar), alerts wait and arrive when it ends

Everything else stays in the inbox, unread, without a sound. The menu bar app (and the Windows tray)
only show what this layer returns: what needs the owner, what the AI is doing, the latest important news.
"""
from datetime import datetime, timedelta
import json
from pathlib import Path
import re
import sqlite3

from . import db as database
from .timeutil import TZ

HEADLINE = {"done": "AI 做完了，等你验收", "blocked": "AI 卡住了，要问你一句", "new": "新进来的事，等你定谁来做",
            "pending": "等的条件到了，这件事该定了", "recall": "这个话题又出现了，上次讨论到这里"}


def important(message):
    """Is this inbox message worth interrupting the owner for?"""
    kind, title = message.get("kind"), message.get("title") or ""
    if kind in {"done", "blocked", "pending", "recall"}:
        return True
    if kind == "new":
        return "你 #" in title or "你#" in title  # new items that are the owner's, not other people's
    if kind == "system":
        return title.startswith("⚠")
    return False


def _body(message):
    rows = [r.get("head") for b in message.get("blocks") or [] if b.get("type") == "section" for r in b.get("rows") or [] if r.get("head")]
    if rows:
        return "；".join(rows[:3]) + (f" 等 {len(rows)} 件" if len(rows) > 3 else "")
    text = message.get("summary") or ""
    return text if text else message.get("title") or ""


def alert_of(message):
    refs = message.get("refs") or []
    title = HEADLINE.get(message.get("kind")) or message.get("title") or "心响"
    body = _body(message) if message.get("kind") in HEADLINE else (message.get("summary") or "")
    if message.get("kind") == "new":
        body = message.get("title") or body
        mine = re.search(r"你\s*((?:#\d+\s*)+)", body)
        if mine:  # jump to the owner's item, not someone else's listed first
            refs = [int(x) for x in re.findall(r"#(\d+)", mine.group(1))] or refs
    return {"id": message["id"], "title": title, "body": str(body)[:140], "n": refs[0] if refs else None,
            "time": message.get("time"), "kind": message.get("kind")}


def _agenda(root):
    try:
        db = database.read(Path(root) / "state/state.db")
        row = db.execute("SELECT value FROM kv WHERE key='meeting_agenda_snapshot'").fetchone()
        db.close()
        return json.loads(row[0]).get("events", []) if row else []
    except (sqlite3.Error, ValueError):
        return []


def _when(value):
    if isinstance(value, dict):
        value = value.get("datetime") or value.get("date_time") or value.get("timestamp")
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            v = int(value)
            return datetime.fromtimestamp(v / 1000 if v > 10**11 else v, TZ)
        at = datetime.fromisoformat(str(value))
        return at if at.tzinfo else at.replace(tzinfo=TZ)
    except (TypeError, ValueError):
        return None


def quiet(root, now=None):
    """(True, why, until) while the owner is in a meeting by the calendar; alerts wait until it ends."""
    now = now or datetime.now(TZ)
    for e in _agenda(root):
        start, end = _when(e.get("start_time")), _when(e.get("end_time"))
        if start and end and start <= now < end:
            return True, f"你在开会：{str(e.get('summary') or '会议')[:20]}", end.isoformat(timespec="minutes")
    return False, "", None


def alerts(root, now=None, hours=24):
    """Unread important messages from the last day, oldest first, plus whether to hold them for now."""
    from . import inbox
    now = now or datetime.now(TZ)
    since = (now - timedelta(hours=hours)).isoformat()
    items = [alert_of(m) for m in reversed(inbox.messages(root, 200, unread_only=True)) if m["at"] >= since and important(m)]
    held, why, until = quiet(root, now)
    return {"quiet": held, "why": why, "until": until, "items": items}


def recent(root, limit=3):
    """The latest important news, read or not, for the one-glance menu."""
    from . import inbox
    out = []
    for m in inbox.messages(root, 120):
        if important(m):
            out.append({**alert_of(m), "read": m["read"]})
        if len(out) >= limit:
            break
    return out


def focus(need):
    """Turn the panel's 'needs you' list into four numbers anyone can read at a glance."""
    counts = {"review": 0, "ask": 0, "decide": 0, "other": 0}
    for x in need:
        label = x.get("label") or ""
        if label in {"待你验收", "待你复查"}:
            counts["review"] += 1
        elif label == "卡住" and x.get("who") == "AI":
            counts["ask"] += 1
        elif label in {"需要你定", "可能重复"}:
            counts["decide"] += 1
        else:
            counts["other"] += 1
    counts["total"] = sum(counts.values())
    return counts


def ai_line(root):
    """One line: who is doing what right now (any driver), or that the AI is free."""
    try:
        from .jobstore import open_jobs
        jobs = open_jobs(root)
    except Exception:
        jobs = []
    if not jobs:
        return {"busy": False, "line": "AI 空闲，有新的事会自动开始"}
    items = [j for j in jobs if j["kind"] == "item"]
    first = (items or jobs)[0]
    what = f"#{first['n']} {str(first.get('title') or '')[:16]}" if first.get("n") else str(first.get("title") or "")[:20]
    more = f" 等 {len(jobs)} 件" if len(jobs) > 1 else ""
    return {"busy": True, "line": f"{first.get('driver') or 'AI'} 正在做 {what}{more}", "count": len(jobs)}
