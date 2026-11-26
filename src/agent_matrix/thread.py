"""One conversation per item, kept locally so every item can be traced back: who said what, what the
AI did step by step, and every version it delivered.

Stored in state/xinxiang.db (outside Git, its own SQLite file so writing never waits for the project
lock). The Feishu task table stays the source of truth for the item itself; this is its history.

    messages:      item record_id, time, from (me / ai / system), kind (say / step / ask / deliver /
                   feedback), text, url, driver (本机 GPT / Cindy / …)
    deliverables:  item record_id, version, time, summary, url, confirm, assumptions, sources

Everything that writes the item notebook (prework.add_note) also lands here, so owner replies,
merges, AI steps, blockers and deliveries all show up without each caller knowing about it.
"""
from datetime import datetime
import json
from pathlib import Path
import re

from . import db as database
from .timeutil import TZ


def _db(root):
    return database.open(root, "xinxiang")


def _now():
    return datetime.now(TZ).isoformat(timespec="seconds")


def add(root, record_id, sender, kind, text, *, url=None, driver=None, at=None, dedupe=None):
    """Append one line to an item's conversation. `dedupe` makes repeated writes (retries, imports) safe."""
    text = str(text or "").strip()
    if not record_id or not text:
        return None
    db = _db(root)
    try:
        with db:
            cur = db.execute("INSERT OR IGNORE INTO messages (record_id, at, sender, kind, text, url, driver, dedupe) VALUES (?,?,?,?,?,?,?,?)",
                             (record_id, at or _now(), sender, kind, text[:4000], url, driver, dedupe))
        return cur.lastrowid
    finally:
        db.close()


URL = re.compile(r"https?://[^\s；;，,)）]+")


def closed(root, record_id, by, how, note=None, at=None):
    """Who closed an item and how (你 / AI / 飞书任务 · 标记完成 / 验收通过 / 标为不做), with the owner's note.
    Also one line in the item's conversation, so the story shows it."""
    at = at or _now()
    db = _db(root)
    try:
        with db:
            db.execute("INSERT INTO closures (record_id, at, by, how, note) VALUES (?,?,?,?,?)",
                       (record_id, at, by, how, (note or "").strip()[:2000] or None))
    finally:
        db.close()
    add(root, record_id, "system", "closed", f"{by}{how}", at=at)


def closures(root):
    """The latest closure of every item: {record_id: {by, how, at, note}}."""
    db = _db(root)
    try:
        return {rid: {"by": by, "how": how, "at": at, "note": note}
                for rid, by, how, at, note in db.execute(
                    "SELECT record_id, by, how, at, note FROM closures WHERE id IN (SELECT max(id) FROM closures GROUP BY record_id)")}
    finally:
        db.close()


def classify(text):
    """Notebook line → (sender, kind, text, url): who said it and what kind of line it is."""
    t = str(text or "").strip()
    url = (URL.search(t) or [None])[0] if URL.search(t) else None
    if t.startswith("本人要改："):
        return "me", "feedback", t.removeprefix("本人要改："), None
    if t.startswith(("本人补充：", "本人退回：")):
        return "me", "feedback", ("退回：" if t.startswith("本人退回") else "") + t.split("：", 1)[1], None
    if t.startswith(("本人改期到", "本人交给")):
        return "me", "feedback", t[2:], None
    if t.startswith("（整理自"):
        return "me", "feedback", t, None
    if t.startswith("进度："):
        return "ai", "step", t[3:], None
    if t.startswith("卡住："):
        return "ai", "ask", t[3:], None
    if t.startswith("已交付"):  # "已交付：<url>；…" or "已交付 v2：<url>；…"
        return "ai", "deliver", t.split("；", 1)[-1] if "；" in t else t, url
    if "开始处理" in t or "并入（" in t or t.startswith(("这一轮超时", "GPT 没有启动")):
        return "system", "say", t, None
    return "ai", "say", t, url


def from_note(root, record_id, text, *, stamp, driver=None):
    """One notebook line ("[MM-DD HH:MM] text") into the conversation. The same line is stored once,
    whether it arrives live from add_note or later from the backfill."""
    from hashlib import sha1
    sender, kind, body, url = classify(text)
    month, day, hm = stamp[:2], stamp[3:5], stamp[6:11]
    at = f"{datetime.now(TZ).year}-{month}-{day}T{hm}:00+08:00"
    key = f"note:{record_id}:{stamp}:{sha1(str(text).encode()).hexdigest()[:12]}"
    import os
    driver = driver or os.environ.get("XINXIANG_DRIVER") or "本机 GPT"  # which AI is driving this process
    return add(root, record_id, sender, kind, body, url=url, at=at, driver=driver if sender == "ai" else None, dedupe=key)


def add_deliverable(root, record_id, *, summary, url, confirm="", assumptions=(), sources=(), at=None):
    """Record the next version (v1, v2, …) of an item's deliverable; returns the version number."""
    db = _db(root)
    try:
        with db:
            same = db.execute("SELECT version FROM deliverables WHERE record_id=? AND url=?", (record_id, url)).fetchone()
            if same:
                return same[0]
            version = (db.execute("SELECT max(version) FROM deliverables WHERE record_id=?", (record_id,)).fetchone()[0] or 0) + 1
            db.execute("INSERT INTO deliverables (record_id, version, at, summary, url, confirm, assumptions, sources) VALUES (?,?,?,?,?,?,?,?)",
                       (record_id, version, at or _now(), summary, url, confirm, json.dumps(list(assumptions), ensure_ascii=False),
                        json.dumps(list(sources), ensure_ascii=False)))
        return version
    finally:
        db.close()


def edit_deliverable(root, record_id, version, *, url=None, summary=None):
    """The owner corrects one version in place (its link, its wording). False when there is no such version."""
    db = _db(root)
    try:
        with db:
            if not db.execute("SELECT 1 FROM deliverables WHERE record_id=? AND version=?", (record_id, version)).fetchone():
                return False
            if url is not None:
                db.execute("UPDATE deliverables SET url=? WHERE record_id=? AND version=?", (url, record_id, version))
            if summary is not None:
                db.execute("UPDATE deliverables SET summary=? WHERE record_id=? AND version=?", (summary, record_id, version))
        return True
    finally:
        db.close()


def remove_deliverable(root, record_id, version):
    """Take one version away (a link the AI made up, a duplicate). The other versions keep their numbers."""
    db = _db(root)
    try:
        with db:
            return db.execute("DELETE FROM deliverables WHERE record_id=? AND version=?", (record_id, version)).rowcount > 0
    finally:
        db.close()


def assumptions_of(markdown):
    """The bullet list under「我做的假设」in a delivery."""
    part = markdown.split("我做的假设", 1)[1] if "我做的假设" in markdown else ""
    part = re.split(r"^#{1,6}\s", part.split("\n", 1)[1] if "\n" in part else "", flags=re.M)[0]
    bullets = [re.sub(r"^\s*(?:[-*]|\d+[.、])\s*", "", x).strip() for x in part.splitlines() if re.match(r"^\s*(?:[-*]|\d+[.、])\s*\S", x)]
    return (bullets or [x.strip() for x in part.splitlines() if x.strip()])[:8]  # a paragraph counts too


def history(root, record_ids=None):
    """{record_id: {"messages": [...], "deliverables": [...]}} for the client."""
    db = _db(root)
    try:
        q, args = "", ()
        if record_ids is not None:
            ids = list(record_ids)
            if not ids:
                return {}
            q, args = f" WHERE record_id IN ({','.join('?' * len(ids))})", tuple(ids)
        out = {}
        for rid, at, sender, kind, text, url, driver, dedupe in db.execute(
                "SELECT record_id, at, sender, kind, text, url, driver, dedupe FROM messages" + q + " ORDER BY at, id", args):
            if sender == "ai" and kind == "say" and (dedupe or "").startswith("note:") and text.startswith("本人要改："):
                sender, kind, text, url = classify(text)
                driver = None
            out.setdefault(rid, {"messages": [], "deliverables": []})["messages"].append(
                {"at": at[5:16].replace("T", " "), "iso_at": at, "from": sender, "kind": kind, "text": text, "url": url, "driver": driver})
        for rid, version, at, summary, url, confirm, assumptions, sources in db.execute(
                "SELECT record_id, version, at, summary, url, confirm, assumptions, sources FROM deliverables" + q + " ORDER BY version", args):
            out.setdefault(rid, {"messages": [], "deliverables": []})["deliverables"].append(
                {"v": version, "at": at[5:16].replace("T", " "), "summary": summary, "url": url, "confirm": confirm,
                 "assumptions": json.loads(assumptions or "[]"), "sources": json.loads(sources or "[]")})
        return out
    finally:
        db.close()


def backfill(root):
    """Once: import the item notebooks and the AI employee's earlier deliveries into the conversations."""
    db = _db(root)
    try:
        if db.execute("SELECT value FROM meta WHERE key='backfill-v1'").fetchone():
            return 0
    finally:
        db.close()
    count = 0
    notes = Path(root) / "state/prework/notes"
    for path in sorted(notes.glob("*.md")) if notes.is_dir() else []:
        rid = path.stem
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
            m = re.match(r"\[(\d\d)-(\d\d) (\d\d:\d\d)\]\s*(.*)", line)
            if not m:
                continue
            month, day, hm, text = m.groups()
            at = f"{datetime.now(TZ).year}-{month}-{day}T{hm}:00+08:00"
            # Before the AI employee existed (2026-11-17 15:00) the work was done by the bot in Cindy.
            driver = "本机 GPT" if (month, day, hm) >= ("11", "17", "15:00") else "Cindy"
            if from_note(root, rid, text, stamp=f"{month}-{day} {hm}", driver=driver):
                count += 1
            sender, kind, body, url = classify(text)
            if kind == "deliver" and url:
                work = Path(root) / "state/work" / rid
                result = {}
                try:
                    result = json.loads((work / "结果.json").read_text(encoding="utf-8"))
                    doc = (work / "交付.md").read_text(encoding="utf-8")
                except (OSError, ValueError):
                    doc = ""
                add_deliverable(root, rid, summary=result.get("summary") or body[:60], url=url, confirm=result.get("confirmation", ""),
                                assumptions=assumptions_of(doc), sources=[s for s in result.get("sources", []) if str(s).startswith("http")],
                                at=at)
    db = _db(root)
    try:
        with db:
            db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('backfill-v1', ?)", (_now(),))
    finally:
        db.close()
    return count


# ---- owner switches and the AI's judgments on new items -------------------------------------------------

def setting(root, key, default=None):
    db = _db(root)
    try:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default
    finally:
        db.close()


def set_setting(root, key, value):
    db = _db(root)
    try:
        with db:
            db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))
    finally:
        db.close()


def judge(root, record_id, decision, reason):
    db = _db(root)
    try:
        with db:
            db.execute("INSERT OR REPLACE INTO judgments (record_id, at, decision, reason) VALUES (?,?,?,?)",
                       (record_id, _now(), decision, reason))
    finally:
        db.close()


def forget_judgment(root, record_id):
    """Restoring a closed item makes its earlier automatic dismissal obsolete."""
    db = _db(root)
    try:
        with db:
            db.execute("DELETE FROM judgments WHERE record_id=?", (record_id,))
    finally:
        db.close()


def judgments(root):
    db = _db(root)
    try:
        return {rid: {"decision": d, "reason": r, "at": at[5:16].replace("T", " ")}
                for rid, at, d, r in db.execute("SELECT record_id, at, decision, reason FROM judgments")}
    finally:
        db.close()
