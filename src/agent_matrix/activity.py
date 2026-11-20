"""Local activity log for the monitor: every message sent (or failed), every error, and
when each part last ran. Separate small SQLite file so it never contends with state.db.

Best effort by design: logging must never break the real work.
"""
from datetime import datetime, timedelta
import json
from pathlib import Path

from . import db as database
from .timeutil import TZ


def _db(root):
    return database.open(root, "activity", timeout=5)


REPEAT_QUIET_MINUTES = 30


def log(root, kind, title, *, detail="", status="ok", ref=""):
    try:
        db = _db(root)
        with db:
            if kind == "error":   # the same failure again and again (every minute): one line, its time moved on
                since = (datetime.now(TZ) - timedelta(minutes=REPEAT_QUIET_MINUTES)).isoformat(timespec="seconds")
                same = db.execute("SELECT id FROM events WHERE kind='error' AND title=? AND detail=? AND status=? AND at>=? "
                                  "ORDER BY id DESC LIMIT 1", (str(title)[:120], str(detail)[:500], status, since)).fetchone()
                if same:
                    db.execute("UPDATE events SET at=? WHERE id=?", (datetime.now(TZ).isoformat(timespec="seconds"), same[0]))
                    db.close()
                    return
            db.execute("INSERT INTO events(at,kind,title,detail,status,ref) VALUES (?,?,?,?,?,?)",
                       (datetime.now(TZ).isoformat(timespec="seconds"), kind, str(title)[:120],
                        str(detail)[:500], status, str(ref)[:120]))
        db.close()
    except Exception:
        pass


def swallowed(root, where, exc):
    """Something optional failed and the caller carries on. Leave a trace instead of nothing: it shows in the
    activity log as kind "error", status "swallowed" (not "failed", so it raises no alert)."""
    log(root, "error", "已跳过的错误：" + where, detail=f"{type(exc).__name__}: {exc}", status="swallowed")


def resolve(root, ref):
    """A later success settles earlier failures of the same thing; they stop showing as problems."""
    try:
        db = _db(root)
        with db:
            db.execute("UPDATE events SET status='resolved' WHERE ref=? AND status='failed'", (ref,))
        db.close()
    except Exception:
        pass


def beat(root, component, summary=""):
    try:
        db = _db(root)
        with db:
            db.execute("INSERT OR REPLACE INTO beats VALUES (?,?,?)",
                       (component, datetime.now(TZ).isoformat(timespec="seconds"), str(summary)[:200]))
        db.close()
    except Exception:
        pass


def preview(text_or_card):
    """A one-line description of what was sent."""
    if isinstance(text_or_card, dict):
        header = text_or_card.get("header", {}).get("title", {}).get("content")
        summary = text_or_card.get("config", {}).get("summary", {}).get("content")
        return header or summary or "卡片"
    first = next((line for line in str(text_or_card).splitlines() if line.strip()), "")
    return first.strip()


def recent(root, *, since=None, limit=200):
    db = _db(root)
    try:
        rows = db.execute("SELECT id,at,kind,title,detail,status,ref FROM events "
                          + ("WHERE at >= ? " if since else "") + "ORDER BY id DESC LIMIT ?",
                          ((since, limit) if since else (limit,))).fetchall()
        beats = db.execute("SELECT component,at,summary FROM beats").fetchall()
    finally:
        db.close()
    keys = ("id", "at", "kind", "title", "detail", "status", "ref")
    return [dict(zip(keys, r)) for r in rows], {c: {"at": a, "summary": s} for c, a, s in beats}


def problems(root, minutes=20):
    """What keeps failing right now, read from the recent errors, for the setup check to turn into steps:
    {"proxy": "127.0.0.1:7890"} a local proxy that is not running, {"scopes": [...]} permissions Feishu refuses,
    {"fields": "预做凭据、…"} columns the item table lacks. A problem not seen for `minutes` is over."""
    import re
    since = (datetime.now(TZ) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    try:
        db = _db(root)
        rows = [r[0] for r in db.execute("SELECT detail FROM events WHERE kind='error' AND status='failed' AND at>=?", (since,))]
        db.close()
    except Exception:
        return {}
    out, scopes = {}, set()
    for text in rows:
        if m := re.search(r"代理 ((?:127\.0\.0\.1|localhost):\d+) 没开", text or ""):
            out["proxy"] = m.group(1)
        if m := re.search(r"少了权限：([^）)]+)", text or ""):
            scopes.update(s for s in m.group(1).split("、") if s)
        elif "missing_scope" in (text or ""):
            out.setdefault("scopes", [])
        if m := re.search(r"事项表缺少必要字段：([^；;\n]+)", text or ""):
            out["fields"] = m.group(1).strip()
    if scopes:
        out["scopes"] = sorted(scopes)
    return out
