"""Local activity log for the monitor: every message sent (or failed), every error, and
when each part last ran. Separate small SQLite file so it never contends with state.db.

Best effort by design: logging must never break the real work.
"""
from datetime import datetime
import json
from pathlib import Path

from . import db as database
from .timeutil import TZ


def _db(root):
    return database.open(root, "activity", timeout=5)


def log(root, kind, title, *, detail="", status="ok", ref=""):
    try:
        db = _db(root)
        with db:
            db.execute("INSERT INTO events(at,kind,title,detail,status,ref) VALUES (?,?,?,?,?,?)",
                       (datetime.now(TZ).isoformat(timespec="seconds"), kind, str(title)[:120],
                        str(detail)[:500], status, str(ref)[:120]))
        db.close()
    except Exception:
        pass


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
