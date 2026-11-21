"""心响 · 看得见进步：AI 交付做得怎么样，按周算最近 8 周。只读本机数据（state/xinxiang.db）。

    一次通过率    有评审结果的事项里，第一版交付就被通过的比例（按事项第一次评审所在的周算）
    几轮才通过    通过的事项之前被退回/要改了几次（和通过时是第几版取大的那个）
    退回原因      退回/要改写的原因，按关键词粗分：太长/格式、方向不对、缺数据/背景、其他
    分组          按交付类型、按 AI（做这件事的 job 的 driver）、按项目各自的一次通过率；不到 3 件标「样本太少」
    卡住/没做成   AI 卡住问你（job 状态 blocked）和没做成（failed）的次数
"""
from datetime import datetime, timedelta
import json
import time

from . import db as database
from .timeutil import TZ

WEEKS = 8
FEW = 3
CACHE_SECONDS = 600
REASONS = [   # checked in this order; the first that matches wins
    ("太长/格式", ("太长", "冗长", "啰嗦", "小作文", "篇幅", "字数", "精简", "简洁", "格式", "排版", "表格", "一屏")),
    ("缺数据/背景", ("数据", "背景", "来源", "依据", "缺", "不全", "补充", "信息", "没查", "没找")),
    ("方向不对", ("方向", "不对", "跑偏", "偏了", "不是我要", "理解错", "搞错", "思路", "重点", "错了")),
]
OTHER = "其他"


def reason_kind(text):
    text = str(text or "")
    return next((label for label, words in REASONS if any(w in text for w in words)), OTHER)


def _at(value):
    try:
        t = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=TZ)


def _rate(first, total):
    return round(first * 100 / total) if total else None


def _group(rows, key):
    out = {}
    for r in rows:
        g = out.setdefault(r[key], {"name": r[key], "total": 0, "first": 0})
        g["total"] += 1
        g["first"] += r["first_pass"]
    return sorted(({**g, "rate": _rate(g["first"], g["total"]), "few": g["total"] < FEW} for g in out.values()),
                  key=lambda g: (-g["total"], g["name"]))


def compute(root, now=None):
    now = now or datetime.now(TZ)
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    starts = [monday - timedelta(weeks=WEEKS - 1 - i) for i in range(WEEKS)]

    def week_of(t):
        return next((i for i in range(WEEKS - 1, -1, -1) if t >= starts[i]), None) if t and t < monday + timedelta(weeks=1) else None

    db = database.open(root, "xinxiang")
    try:
        names = {pid: name for pid, name in db.execute("SELECT id, name FROM projects")}
        verdicts = db.execute("SELECT record_id, at, verdict, reason, version, template, project_id FROM outcomes ORDER BY id").fetchall()
        jobs = db.execute("SELECT kind, ref, driver, status, updated FROM jobs ORDER BY updated").fetchall()
    finally:
        db.close()

    driver_of = {}   # record_id → the AI that worked on it last
    for kind, ref, driver, _status, _updated in jobs:
        if kind == "item" and driver:
            try:
                rid = (json.loads(ref or "{}") or {}).get("record_id")
            except ValueError:
                rid = None
            if rid:
                driver_of[rid] = driver

    by_item = {}
    for rid, at, verdict, reason, version, template, project in verdicts:
        by_item.setdefault(rid, []).append({"at": _at(at), "verdict": verdict, "reason": reason or "", "version": version,
                                            "template": template or "", "project": project})

    items, passes, reasons = [], [], {}
    for rid, rows in by_item.items():
        first = rows[0]
        w = week_of(first["at"])
        passed = next((i for i, r in enumerate(rows) if r["verdict"] == "通过"), None)
        if passed is not None:
            rounds = max(sum(1 for r in rows[:passed] if r["verdict"] in ("退回", "要改")), (rows[passed]["version"] or 1) - 1)
            pw = week_of(rows[passed]["at"])
            if pw is not None:
                passes.append({"week": pw, "rounds": rounds})
        if w is None:
            continue
        items.append({"week": w, "first_pass": int(first["verdict"] == "通过" and (first["version"] or 1) <= 1),
                      "type": next((r["template"] for r in reversed(rows) if r["template"]), "") or "没标类型",
                      "ai": driver_of.get(rid) or "不知道是谁",
                      "project": names.get(next((r["project"] for r in reversed(rows) if r["project"]), None), "") or "没归项目"})
    for rows in by_item.values():
        for r in rows:
            if r["verdict"] in ("退回", "要改") and week_of(r["at"]) is not None:
                g = reasons.setdefault(reason_kind(r["reason"]), {"count": 0, "example": ""})
                g["count"] += 1
                g["example"] = g["example"] or r["reason"][:60]

    weeks = []
    for i, start in enumerate(starts):
        mine = [x for x in items if x["week"] == i]
        done = [p["rounds"] for p in passes if p["week"] == i]
        weeks.append({"start": start.date().isoformat()[5:], "total": len(mine), "first": sum(x["first_pass"] for x in mine),
                      "rate": _rate(sum(x["first_pass"] for x in mine), len(mine)),
                      "rounds": round(sum(done) / len(done), 1) if done else None, "blocked": 0, "failed": 0})
    for _kind, _ref, _driver, status, updated in jobs:
        w = week_of(_at(updated)) if status in ("blocked", "failed") else None
        if w is not None:
            weeks[w][status] += 1

    def span(ws):
        total, first = sum(w["total"] for w in ws), sum(w["first"] for w in ws)
        return {"total": total, "first": first, "rate": _rate(first, total)}

    all_rounds = [p["rounds"] for p in passes]
    return {"has": bool(items), "weeks": weeks, "recent": span(weeks[-4:]), "before": span(weeks[:-4]),
            "rounds": round(sum(all_rounds) / len(all_rounds), 1) if all_rounds else None,
            "reasons": [{"kind": k, **v} for k, v in sorted(reasons.items(), key=lambda kv: (-kv[1]["count"], kv[0]))],
            "by_type": _group(items, "type"), "by_ai": _group(items, "ai"), "by_project": _group(items, "project"),
            "blocked": sum(w["blocked"] for w in weeks), "failed": sum(w["failed"] for w in weeks),
            "at": now.strftime("%H:%M")}


_cache = {}


def cached(root, ttl=CACHE_SECONDS):
    """The status poll asks every few seconds; the numbers are worked out again at most every 10 minutes,
    or as soon as a new verdict comes in (one cheap lookup)."""
    db = database.open(root, "xinxiang")
    try:
        mark = db.execute("SELECT max(id) FROM outcomes").fetchone()[0]
    finally:
        db.close()
    key, hit = str(root), _cache.get(str(root))
    if hit and hit[1] == mark and time.monotonic() - hit[0] < ttl:
        return hit[2]
    value = compute(root)
    _cache[key] = (time.monotonic(), mark, value)
    return value
