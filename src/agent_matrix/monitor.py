"""What the system is doing, for the menu bar app and the local page. Read-only, no model.

    python -m agent_matrix.monitor --json [--write-html]   # status for the menu bar app
    python -m agent_matrix.monitor --test-send              # send yourself one test message
"""
import argparse
from datetime import datetime, timedelta
from glob import glob
import html
import json
import os
from pathlib import Path
import sqlite3

from . import activity
from .timeutil import TZ

COMPONENTS = [("实时监听", 3, "常驻监听程序（每分钟心跳）"), ("回复轮询", 12, "Cindy「飞书回复轮询」（每 5 分钟）"),
              ("会议检查", 10, "Cindy「会议复盘与提前代做」（每 2 分钟）"), ("代做检查", 10, "同上"),
              ("聊天检查", 10, "同上")]
EVENT_NAMES = {"im.message.receive_v1": "私聊回复", "vc.meeting.participant_meeting_ended_v1": "会议结束",
               "vc.note.generated_v1": "逐字稿生成", "task.task.update_user_access_v2": "飞书任务变化"}
STATE_WORDS = {"live": "已连接", "not_subscribed": "未订阅", "connecting": "连接中", "reconnecting": "重连中"}
CINDY_SCHEDULES = ["事项中心｜每日周期事项", "事项中心｜工作日早间简报", "事项中心｜工作日明日计划",
                   "会议复盘与提前代做", "事项中心｜飞书回复轮询"]


def _ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)


def _kv(db, key, default=None):
    row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _age(at, now):
    try:
        return (now - datetime.fromisoformat(at)).total_seconds() / 60
    except (TypeError, ValueError):
        return None


def _queues(root):
    out = {"会议待处理": 0, "聊天候选": 0, "代做排队": 0, "等你验收（代做）": 0}
    path = Path(root) / "state/state.db"
    if not path.exists():
        return out, {}, None, {}
    db = _ro(path)
    try:
        q = lambda sql: (db.execute(sql).fetchone() or [0])[0]
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "meeting_queue" in tables:
            out["会议待处理"] = q("SELECT count(*) FROM meeting_queue WHERE status NOT IN ('done')")
        if "chat_candidates" in tables:
            out["聊天候选"] = q("SELECT count(*) FROM chat_candidates WHERE status='pending'")
        if "prework_queue" in tables:
            out["代做排队"] = q("SELECT count(*) FROM prework_queue WHERE status IN ('queued','running')")
            out["等你验收（代做）"] = q("SELECT count(*) FROM prework_queue WHERE status='prepared'")
        return out, _kv(db, "listen:status", {}) or {}, _kv(db, "listen:heartbeat"), {
            "board_url": _kv(db, "brief-v1:board-url")}
    finally:
        db.close()


def _cindy(now):
    """Last run of each Cindy routine, read-only from Cindy's own database (optional)."""
    files = glob(os.path.expanduser("~/Library/Application Support/Cindy/cindy-*.db"))
    if not files:
        return []
    out = []
    try:
        db = _ro(files[0])
        for name in CINDY_SCHEDULES:
            row = db.execute("""SELECT s.status, s.cron_expr, r.fired_at, r.finished_at, r.status, r.error_msg
                FROM schedules s LEFT JOIN schedule_runs r ON r.id = (SELECT id FROM schedule_runs WHERE schedule_id=s.id
                ORDER BY fired_at DESC LIMIT 1) WHERE s.name=?""", (name,)).fetchone()
            if not row:
                continue
            fired = datetime.fromtimestamp(row[2] / 1000, TZ).isoformat(timespec="seconds") if row[2] else None
            out.append({"name": name, "enabled": row[0] == "active", "cron": row[1], "last_run": fired,
                        "last_status": row[4], "error": (row[5] or "")[:160]})
        db.close()
    except sqlite3.Error:
        return out
    return out


def build(root, now=None):
    now = now or datetime.now(TZ)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    events, beats = activity.recent(root, since=today)
    sends = [e for e in events if e["kind"] == "send"]
    errors = [e for e in events if e["status"] == "failed"]
    queues, streams, heartbeat, links = _queues(root)
    issues, level = [], "ok"

    def flag(text, severity):
        nonlocal level
        issues.append({"text": text, "level": severity})
        if severity == "error" or (severity == "warn" and level == "ok"):
            level = severity

    hb_age = _age(heartbeat, now)
    if hb_age is None or hb_age > 3:
        flag("实时监听没在运行：私聊回复、会前卡片会变慢。点「重启监听」。", "error")
    for key, st in streams.items():
        if st == "not_subscribed":
            flag(f"飞书「{EVENT_NAMES.get(key, key)}」推送还没订阅，暂时靠定时检查。", "warn")
    working_hours = now.weekday() < 5 and 8 <= now.hour < 22
    for name, limit, where in COMPONENTS[1:]:
        age = _age((beats.get(name) or {}).get("at"), now)
        if working_hours and (age is None or age > limit):
            flag(f"「{name}」超过 {limit} 分钟没跑了，看看 {where} 是否正常。", "warn")
    recent_fail = [e for e in errors if _age(e["at"], now) is not None and _age(e["at"], now) <= 60]
    if any(e["kind"] == "send" for e in recent_fail):
        flag("最近 1 小时有消息发送失败。", "error")
    elif recent_fail:
        flag(f"最近 1 小时有 {len(recent_fail)} 次出错，详见下方。", "warn")
    cindy = _cindy(now)
    for c in cindy:
        if c["last_status"] in {"failed", "error"}:
            flag(f"Cindy「{c['name']}」上次运行失败。", "warn")
    beats_out = []
    for name, limit, where in COMPONENTS:
        b = beats.get(name) or {}
        age = _age(b.get("at"), now)
        beats_out.append({"name": name, "at": b.get("at"), "minutes_ago": None if age is None else round(age, 1),
                          "summary": b.get("summary", ""), "where": where})
    return {"generated_at": now.isoformat(timespec="seconds"), "level": level, "issues": issues,
            "headline": {"ok": "一切正常", "warn": "有地方要注意", "error": "出错了"}[level],
            "sends_today": [{"id": e["id"], "time": e["at"][11:16], "title": e["title"], "ok": e["status"] == "ok"} for e in sends],
            "errors_today": [{"id": e["id"], "time": e["at"][11:16], "title": e["title"], "detail": e["detail"][:200]} for e in errors[:20]],
            "latest_event_id": max((e["id"] for e in events), default=0),
            "events_today": [{"id": e["id"], "time": e["at"][11:16], "kind": e["kind"], "title": e["title"], "ok": e["status"] == "ok"}
                             for e in events[:60]],
            "components": beats_out, "queues": queues,
            "streams": [{"name": EVENT_NAMES.get(k, k), "state": STATE_WORDS.get(v, v)} for k, v in streams.items()],
            "cindy": cindy, "board_url": links.get("board_url")}


PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8"><meta http-equiv="refresh" content="15">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>事项中心监控</title><style>
:root{{--bg:#f6f7f9;--card:#fff;--ink:#1d2129;--muted:#86909c;--line:#e5e6eb;--ok:#00b42a;--warn:#ff7d00;--err:#f53f3f}}
@media (prefers-color-scheme:dark){{:root{{--bg:#17181a;--card:#232427;--ink:#e5e6eb;--muted:#8a8f98;--line:#33353a}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,"PingFang SC",sans-serif}}
main{{max-width:880px;margin:0 auto;padding:20px 16px}} h1{{font-size:22px;margin:0 0 4px}}
.sub{{color:var(--muted);font-size:12px}} .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin-top:14px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}} .card h2{{font-size:14px;margin:0 0 8px}}
.row{{display:flex;justify-content:space-between;gap:8px;padding:4px 0;border-top:1px solid var(--line)}} .row:first-of-type{{border-top:0}}
.muted{{color:var(--muted)}} .ok{{color:var(--ok)}} .warn{{color:var(--warn)}} .error{{color:var(--err)}}
.dot{{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;vertical-align:middle}}
.issue{{padding:6px 10px;border-radius:6px;margin:6px 0;background:var(--bg)}}</style></head><body><main>
<h1><span class="dot" style="background:var(--{level})"></span>{headline}</h1><div class="sub">更新于 {generated}，每 15 秒自动刷新</div>
{issues}<div class="grid">
<div class="card"><h2>今天发给你的消息（{send_count}）</h2>{sends}</div>
<div class="card"><h2>各环节最后一次运行</h2>{components}</div>
<div class="card"><h2>排队中</h2>{queues}</div>
<div class="card"><h2>飞书实时推送</h2>{streams}</div>
<div class="card"><h2>Cindy 例行任务</h2>{cindy}</div>
<div class="card"><h2>今天的报错（{error_count}）</h2>{errors}</div>
</div></main></body></html>"""


def render(s):
    e = html.escape
    row = lambda a, b, cls="": f'<div class="row"><span>{a}</span><span class="{cls}">{b}</span></div>'
    none = '<div class="muted">暂无</div>'
    ago = lambda m: "从未" if m is None else ("刚刚" if m < 1 else f"{m:.0f} 分钟前" if m < 120 else f"{m/60:.1f} 小时前")
    return PAGE.format(
        level=s["level"] if s["level"] != "ok" else "ok", headline=e(s["headline"]), generated=e(s["generated_at"][11:19]),
        issues="".join(f'<div class="issue {i["level"]}">{e(i["text"])}</div>' for i in s["issues"]),
        send_count=len(s["sends_today"]),
        sends="".join(row(f'{x["time"]}　{e(x["title"])}', "✓ 已送达" if x["ok"] else "✗ 失败", "ok" if x["ok"] else "error")
                      for x in reversed(s["sends_today"])) or none,
        components="".join(row(f'{e(c["name"])}<br><span class="muted">{e(c["summary"])}</span>', ago(c["minutes_ago"])) for c in s["components"]),
        queues="".join(row(e(k), v) for k, v in s["queues"].items()),
        streams="".join(row(e(x["name"]), e(x["state"]), "ok" if x["state"] == "已连接" else "warn") for x in s["streams"]) or none,
        cindy="".join(row(f'{e(c["name"])}<br><span class="muted">{e(c["cron"] or "")}</span>',
                          f'{(c["last_run"] or "")[11:16]} {e(c["last_status"] or "未运行")}',
                          "ok" if c["last_status"] in {"success", "completed", "ok", "skipped", "idle"} else "warn") for c in s["cindy"]) or none,
        error_count=len(s["errors_today"]),
        errors="".join(row(f'{x["time"]}　{e(x["title"])}<br><span class="muted">{e(x["detail"])}</span>', "") for x in s["errors_today"]) or none)


def main(argv=None):
    p = argparse.ArgumentParser(description="事项中心本地监控")
    p.add_argument("--config", type=Path, default=Path("config.yaml"))
    p.add_argument("--json", action="store_true")
    p.add_argument("--write-html", action="store_true")
    p.add_argument("--test-send", action="store_true")
    args = p.parse_args(argv)
    root = args.config.resolve().parent
    if args.test_send:
        from .config import load
        from .feishu import Feishu
        client = Feishu(load(args.config.resolve()), root)
        client.verify_owner(client.auth())
        now = datetime.now(TZ)
        receipt = client.send_self(f"【监控测试】{now:%H:%M} 发送通道正常，这条消息说明系统能给你发飞书。",
                                   "monitor-test-" + now.strftime("%Y%m%d%H%M%S"))
        print(json.dumps({"ok": True, "message_id": receipt.get("message_id")}, ensure_ascii=False))
        return 0
    status = build(root)
    if args.write_html:
        out = root / "state/monitor.html"
        out.write_text(render(status))
        status["page"] = str(out)
    print(json.dumps(status, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
