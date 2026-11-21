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

from . import db as database
from .textmatch import similarity
from .timeutil import TZ

KIND_LABEL = {"decision": "定了", "requirement": "需求", "open_question": "还没定", "waiting": "等条件再定", "action": "要做"}
RECALL_MIN = 0.34


def _db(root):
    return database.open(root, "xinxiang")


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
        if m["kind"] not in kinds or m["status"] == "stale" or (exclude_source_id and m["source_id"] == exclude_source_id):
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


OWNER_SOURCES = ("seed", "本人")


def rules(root):
    db = _db(root)
    try:
        if not db.execute("SELECT count(*) FROM preferences").fetchone()[0]:
            with db:
                for rule, why in SEED_RULES:
                    db.execute("INSERT OR IGNORE INTO preferences (rule, why, source, created) VALUES (?,?,?,?)", (rule, why, "seed", _now()))
        names = {pid: name for pid, name in db.execute("SELECT id, name FROM projects")}
        return [{"id": i, "rule": r, "why": w, "source": s, "created": c[5:16].replace("T", " "),
                 "fresh": s not in OWNER_SOURCES and not ok,   # learned by the AI, the owner has not said it is right yet
                 "scope": sc or "", "scope_name": scope_name(sc, names), "wins": wn or 0, "losses": ls or 0}
                for i, r, w, s, c, ok, sc, wn, ls in db.execute(
                    "SELECT id, rule, why, source, created, confirmed, scope, wins, losses FROM preferences WHERE active=1 ORDER BY id")]
    finally:
        db.close()


def scope_name(scope, names=None):
    """"" → 全部；"project:3" → 项目「观己 2.0」；"type:方案" → 做「方案」时."""
    scope = str(scope or "")
    if scope.startswith("project:"):
        name = (names or {}).get(int(scope[8:]) if scope[8:].isdigit() else -1)
        return f"项目「{name}」" if name else "某个项目"
    if scope.startswith("type:"):
        return f"做「{scope[5:]}」时"
    return "全部"


def add_rule(root, rule, why="", source="本人", scope=""):
    rule = " ".join(str(rule or "").split())[:120]
    if not rule:
        return None
    db = _db(root)
    try:
        with db:
            db.execute("INSERT INTO preferences (rule, why, source, created, scope) VALUES (?,?,?,?,?) "
                       "ON CONFLICT(rule) DO UPDATE SET active=1, why=excluded.why, scope=excluded.scope",
                       (rule, str(why)[:200], source, _now(), str(scope or "")[:80]))
        return rule
    finally:
        db.close()


def credit(root, rule_id, won):
    """A deliverable that followed this rule passed (won) or came back for breaking it / despite it (lost)."""
    db = _db(root)
    try:
        with db:
            db.execute(f"UPDATE preferences SET {'wins' if won else 'losses'}={'wins' if won else 'losses'}+1 WHERE id=?", (int(rule_id),))
    finally:
        db.close()


# ---- 评审结果: every verdict on a deliverable is kept, so the AI learns what passes -----------------------------

def record_outcome(root, record_id, verdict, reason=""):
    """verdict: "通过" / "退回" / "要改". Kept with the deliverable version, its kind and its project."""
    work = Path(root) / "state" / "work" / record_id
    try:
        template = json.loads((work / "结果.json").read_text(encoding="utf-8")).get("template") or ""
    except (OSError, ValueError, AttributeError):
        template = ""
    db = _db(root)
    try:
        version = (db.execute("SELECT max(version) FROM deliverables WHERE record_id=?", (record_id,)).fetchone() or [None])[0]
        project = (db.execute("SELECT project_id FROM item_projects WHERE record_id=?", (record_id,)).fetchone() or [None])[0]
        with db:
            db.execute("INSERT INTO outcomes (record_id, at, verdict, reason, version, template, project_id) VALUES (?,?,?,?,?,?,?)",
                       (record_id, _now(), verdict, str(reason or "")[:500], version, str(template)[:20], project))
    finally:
        db.close()


def outcomes(root, since=""):
    db = _db(root)
    try:
        names = {pid: name for pid, name in db.execute("SELECT id, name FROM projects")}
        return [{"record_id": r, "at": a[5:16].replace("T", " "), "verdict": v, "reason": why or "", "version": ver,
                 "template": t or "", "project": names.get(p, "")}
                for r, a, v, why, ver, t, p in db.execute(
                    "SELECT record_id, at, verdict, reason, version, template, project_id FROM outcomes WHERE at>? ORDER BY id", (since,))]
    finally:
        db.close()


def confirm_rule(root, rule_id):
    db = _db(root)
    try:
        with db:
            db.execute("UPDATE preferences SET confirmed=1 WHERE id=?", (int(rule_id),))
    finally:
        db.close()


def remove_rule(root, rule_id):
    db = _db(root)
    try:
        with db:
            db.execute("UPDATE preferences SET active=0 WHERE id=?", (int(rule_id),))
    finally:
        db.close()


def rules_text(root, project_id=None):
    """The block every AI gets on top of its instructions: the rules for everything, the ones for this item's project,
    and the ones for a kind of deliverable (marked, so the AI applies them only when it makes that kind)."""
    lines = []
    for r in rules(root):
        scope = r.get("scope") or ""
        if scope.startswith("project:") and scope != f"project:{project_id}":
            continue
        tag = f"（{r['scope_name']}）" if scope else ""
        lines.append(f"- {r['rule']}{tag}")
    return ("## 本人的规矩（先读，必须遵守；和下面的说明冲突时以这里为准）\n" + "\n".join(lines) + "\n\n") if lines else ""


# ---- 关于你: who the owner is, what they do, how they like work done -----------------------------------------
# One plain Markdown file the owner can read and edit (state/profile.md). Every AI reads it before any work, so a
# deliverable fits the owner's job, products, people and habits instead of a generic answer.

PROFILE_MAX = 6000


def _profile_path(root):
    return Path(root) / "state" / "profile.md"


def profile(root):
    path = _profile_path(root)
    if not path.is_file():
        return {"text": "", "at": ""}
    return {"text": path.read_text(encoding="utf-8"), "at": datetime.fromtimestamp(path.stat().st_mtime, TZ).strftime("%m-%d %H:%M")}


def set_profile(root, text):
    text = str(text or "").strip()[:PROFILE_MAX]
    path = _profile_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if text:
        path.write_text(text + "\n", encoding="utf-8")
    elif path.exists():
        path.unlink()
    return profile(root)


def profile_text(root):
    """The block every AI gets on top of its instructions, right after the rules."""
    text = profile(root)["text"].strip()
    return ("## 本人是谁、在做什么（先读：按他的岗位、产品和习惯来做，用他的说法，交付要能直接拿去用）\n"
            + text + "\n\n") if text else ""


# ---- 让 AI 认识我: the owner ticks where the AI may look, the AI writes a draft, the owner decides ----------------

SCAN_SOURCES = {"feishu": "飞书近 30 天", "folders": "本机项目文件夹", "wechat": "微信近期聊天", "ai_memory": "Cindy/Claude 记忆"}


def _scan_path(root):
    return Path(root) / "state" / "profile_scan.json"


def _draft_path(root):
    return Path(root) / "state" / "profile_draft.md"


def ask_scan(root, sources):
    sources = [s for s in sources if s in SCAN_SOURCES]
    if not sources:
        raise ValueError("至少选一个地方")
    path = _scan_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sources": sources, "at": datetime.now(TZ).isoformat(timespec="seconds")}, ensure_ascii=False), encoding="utf-8")


def scan_asked(root):
    try:
        return json.loads(_scan_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def cancel_scan(root):
    _scan_path(root).unlink(missing_ok=True)


def save_draft(root, text, by=""):
    path = _draft_path(root)
    path.write_text(str(text).strip() + "\n", encoding="utf-8")
    (path.with_suffix(".json")).write_text(json.dumps({"by": by, "at": datetime.now(TZ).strftime("%m-%d %H:%M")}, ensure_ascii=False), encoding="utf-8")
    cancel_scan(root)


def draft(root):
    path = _draft_path(root)
    if not path.is_file():
        return None
    try:
        meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    return {"text": path.read_text(encoding="utf-8"), **meta}


def drop_draft(root, use=False):
    """The owner decided: use the draft as 关于你 (use=True), or throw it away."""
    d = draft(root)
    if d and use:
        set_profile(root, d["text"])
    _draft_path(root).unlink(missing_ok=True)
    _draft_path(root).with_suffix(".json").unlink(missing_ok=True)
    return profile(root)
