"""心响 · 经历层与记忆层：让过去的讨论在需要的时候回来。

    经历 (moments)    every meeting leaves what was decided, asked, required, and what waits on a condition
                      ("先放着，等周五数据出来再定"), each with the exact words, the meeting and the time
    召回 (recall)     given a topic, the related moments and items from before, most related and recent first
    未决 (waiting)    when its date comes, or the same topic shows up in a new meeting, the owner is told
    偏好 (rules)      the owner's standing rules ("不是我负责的不用管"); every AI reads them before any work

Stored in state/xinxiang.db next to the conversations. Feishu stays the source of truth for the items;
this is the context around them. Matching is cheap code (character-pair similarity), no model call.
"""
from datetime import date, datetime, timedelta
import json
from pathlib import Path

from .merge import similarity
from .timeutil import TZ

KIND_LABEL = {"decision": "定了", "requirement": "需求", "open_question": "还没定", "waiting": "等条件再定", "action": "要做"}
RECALL_MIN = 0.34


def _db(root):
    from .thread import _db as base
    db = base(root)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS moments (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, source_id TEXT NOT NULL,
            source_title TEXT, at TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, quote TEXT, stamp TEXT,
            wait_for TEXT, due TEXT, status TEXT NOT NULL DEFAULT 'open', created TEXT NOT NULL,
            UNIQUE(source, source_id, kind, text));
        CREATE TABLE IF NOT EXISTS preferences (id INTEGER PRIMARY KEY AUTOINCREMENT, rule TEXT NOT NULL UNIQUE, why TEXT,
            source TEXT, created TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS recalls (waiting_id INTEGER NOT NULL, source_id TEXT NOT NULL, PRIMARY KEY (waiting_id, source_id));
    """)
    return db


def _now():
    return datetime.now(TZ).isoformat(timespec="seconds")


# ---- 经历：what a meeting leaves behind ---------------------------------------------------------------

def record_meeting(root, review, meta, announce=True):
    """Keep a published meeting's decisions, requirements, open questions, waiting items and actions.
    Returns the new moments; any older waiting item on the same topic is brought back (recall)."""
    at = str(meta.get("start_time") or _now())
    title = str(meta.get("topic") or "会议")
    rows = []
    for kind, entries in (("decision", review.decisions), ("requirement", review.requirements),
                          ("open_question", review.open_questions), ("waiting", getattr(review, "waiting", []) or []),
                          ("action", review.actions)):
        for e in entries:
            text = getattr(e, "title", None) if kind == "action" else e.text
            rows.append((kind, text or e.text, e.quote, e.timestamp, getattr(e, "wait_for", None), getattr(e, "due_date", None)))
    new = _insert(root, "meeting", review.meeting_id, title, at, rows)
    if announce:
        _recall_waiting(root, "meeting", review.meeting_id, title, new)
    return new


def backfill_meetings(root):
    """Once: earlier meetings' extractions become history too (quietly: no recall alerts for old news)."""
    import sqlite3
    from .meeting_review import Review
    db = _db(root)
    try:
        if db.execute("SELECT value FROM meta WHERE key='moments-backfill-v1'").fetchone():
            return 0
    finally:
        db.close()
    count = 0
    try:
        st = sqlite3.connect(f"file:{Path(root) / 'state/state.db'}?mode=ro", uri=True, timeout=10)
        queue = {mid: json.loads(meta) for mid, meta in st.execute("SELECT meeting_id, metadata FROM meeting_queue")}
        st.close()
    except sqlite3.Error:
        queue = {}
    for path in sorted((Path(root) / "state/meeting-transcripts").glob("*/review.json")):
        mid = path.parent.name
        try:
            review = Review.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        count += len(record_meeting(root, review, {**queue.get(mid, {}), "meeting_id": mid}, announce=False))
    db = _db(root)
    try:
        with db:
            db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('moments-backfill-v1', ?)", (_now(),))
    finally:
        db.close()
    return count


def _insert(root, source, source_id, title, at, rows):
    db = _db(root)
    new = []
    try:
        with db:
            for kind, text, quote, stamp, wait_for, due in rows:
                cur = db.execute("""INSERT OR IGNORE INTO moments (source, source_id, source_title, at, kind, text, quote, stamp,
                    wait_for, due, created) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                                 (source, source_id, title, at, kind, str(text)[:500], str(quote or "")[:600], stamp, wait_for, due, _now()))
                if cur.rowcount:
                    new.append({"id": cur.lastrowid, "kind": kind, "text": str(text)[:500]})
    finally:
        db.close()
    return new


def moments(root, *, status=None, kind=None, limit=500):
    db = _db(root)
    try:
        q, args = "SELECT id, source, source_id, source_title, at, kind, text, quote, stamp, wait_for, due, status FROM moments", []
        cond = []
        if status:
            cond.append("status=?"); args.append(status)
        if kind:
            cond.append("kind=?"); args.append(kind)
        if cond:
            q += " WHERE " + " AND ".join(cond)
        rows = db.execute(q + " ORDER BY at DESC, id DESC LIMIT ?", (*args, limit)).fetchall()
    finally:
        db.close()
    keys = ("id", "source", "source_id", "source_title", "at", "kind", "text", "quote", "stamp", "wait_for", "due", "status")
    return [dict(zip(keys, r)) for r in rows]


# ---- 召回：what was said about this before --------------------------------------------------------------

def recall(root, topic, *, limit=5, exclude_source_id=None, kinds=("decision", "waiting", "open_question", "requirement")):
    """Moments about the same topic, best match first (ties: most recent). Cheap; no model call."""
    scored = []
    for m in moments(root, limit=800):
        if m["kind"] not in kinds or (exclude_source_id and m["source_id"] == exclude_source_id):
            continue
        s = max(similarity(topic, m["text"]), similarity(m["text"], topic))
        if s >= RECALL_MIN:
            scored.append((s, m["at"], m))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [m for _, _, m in scored[:limit]]


def recall_lines(root, topic, **kw):
    """The same, as short lines for an AI's background pack or a prep card."""
    return [f"[{str(m['at'])[5:16]} {m['source_title']}] {KIND_LABEL.get(m['kind'], '')}：{m['text']}"
            + (f"（等：{m['wait_for']}）" if m.get("wait_for") else "") + (f"\n  原话：{m['quote'][:160]}" if m.get("quote") else "")
            for m in recall(root, topic, **kw)]


# ---- 未决：bring a waiting item back when its condition comes -----------------------------------------

def _recall_waiting(root, source, source_id, title, new):
    """The same topic shows up again (a new meeting): tell the owner what was waiting, with the old words."""
    from . import inbox
    open_waiting = [m for m in moments(root, status="open", kind="waiting") if m["source_id"] != source_id]
    if not open_waiting or not new:
        return
    db = _db(root)
    try:
        for w in open_waiting:
            hit = next((x for x in new if max(similarity(w["text"], x["text"]), similarity(x["text"], w["text"])) >= 0.45), None)
            if not hit or db.execute("SELECT 1 FROM recalls WHERE waiting_id=? AND source_id=?", (w["id"], source_id)).fetchone():
                continue
            with db:
                db.execute("INSERT INTO recalls VALUES (?,?)", (w["id"], source_id))
            inbox.add(root, f"recall:{w['id']}:{source_id}",
                      f"「{title}」又谈到了：{hit['text'][:60]}\n上次在「{w['source_title']}」（{str(w['at'])[5:10]}）说的是：{w['text'][:80]}"
                      + (f"\n当时在等：{w['wait_for']}" if w.get("wait_for") else "")
                      + (f"\n原话：{w['quote'][:120]}" if w.get("quote") else ""), kind="recall")
    finally:
        db.close()


def waiting_due(root, today=None):
    """Waiting items whose date has come: told once, then marked."""
    from . import inbox
    today = (today or datetime.now(TZ).date()).isoformat()
    told = []
    for w in moments(root, status="open", kind="waiting"):
        if not w.get("due") or str(w["due"])[:10] > today:
            continue
        inbox.add(root, f"waiting-due:{w['id']}",
                  f"「{w['text'][:40]}」该定了：当时说等 {w.get('wait_for') or '这个日子'}\n出自「{w['source_title']}」（{str(w['at'])[5:10]}）"
                  + (f"\n原话：{w['quote'][:120]}" if w.get("quote") else ""), kind="pending")
        set_status(root, w["id"], "alerted")
        told.append(w["id"])
    return told


def set_status(root, moment_id, status):
    db = _db(root)
    try:
        with db:
            db.execute("UPDATE moments SET status=? WHERE id=?", (status, int(moment_id)))
    finally:
        db.close()


# ---- 偏好：the owner's standing rules, read by every AI before any work ----------------------------------

SEED_RULES = [
    ("不是本人负责的事不用管，也不要替别人做。", "本人说：不是我负责的不用管（2026-11-18）"),
    ("交付别写成小作文：第一屏给结论，能用表格就不用段落，不写铺垫和方法论。", "本人说：报告都和写小作文一样（2026-11-16）"),
    ("没有人明确承接的话不算待办，不要硬凑。", "本人说：硬凑的干掉（2026-11-16）"),
    ("先自己把背景找全再动手，不要反复问本人要信息。", "本人说：它都不会立刻去找背景（2026-11-17）"),
]


def rules(root):
    db = _db(root)
    try:
        if not db.execute("SELECT count(*) FROM preferences").fetchone()[0]:
            with db:
                for rule, why in SEED_RULES:
                    db.execute("INSERT OR IGNORE INTO preferences (rule, why, source, created) VALUES (?,?,?,?)", (rule, why, "seed", _now()))
        return [{"id": i, "rule": r, "why": w, "source": s, "created": c[5:16].replace("T", " ")}
                for i, r, w, s, c in db.execute("SELECT id, rule, why, source, created FROM preferences WHERE active=1 ORDER BY id")]
    finally:
        db.close()


def add_rule(root, rule, why="", source="本人"):
    rule = " ".join(str(rule or "").split())[:120]
    if not rule:
        return None
    db = _db(root)
    try:
        with db:
            db.execute("INSERT INTO preferences (rule, why, source, created) VALUES (?,?,?,?) "
                       "ON CONFLICT(rule) DO UPDATE SET active=1, why=excluded.why", (rule, str(why)[:200], source, _now()))
        return rule
    finally:
        db.close()


def remove_rule(root, rule_id):
    db = _db(root)
    try:
        with db:
            db.execute("UPDATE preferences SET active=0 WHERE id=?", (int(rule_id),))
    finally:
        db.close()


def rules_text(root):
    """The block every AI gets on top of its instructions."""
    lines = [f"- {r['rule']}" for r in rules(root)]
    return ("## 本人的规矩（先读，必须遵守；和下面的说明冲突时以这里为准）\n" + "\n".join(lines) + "\n\n") if lines else ""
