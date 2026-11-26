"""What is going on, seen from the owner's side: tasks first, system only when it needs you.

    python -m agent_matrix.monitor --json [--write-html]   # for the menu bar app / local page
    python -m agent_matrix.monitor --test-send              # send yourself one test message

Sections:
  需要你     items waiting for the owner: review, re-check after a rejection, decide, give material
  正在进行   everything in motion with who does it, progress, and a plain status label
  今天       timeline: detections (meetings / chats / Feishu tasks), replies, messages sent
  系统问题   only real problems, each with what happened / what the system did / what you do

Self-repair: a stopped listener is restarted by the menu bar app; an AI employee that stopped
checking is reported to the owner on Feishu once. Nothing calls a model.
"""
import argparse
from datetime import datetime, timedelta
from glob import glob
import html
import json
import os
from pathlib import Path
import sqlite3

from .snapshot import SNAPSHOT_FIELDS, save_snapshot, snapshot_rows, _local_store, _snapshot, _STORE  # noqa: F401
from . import db as database
from . import activity
from .timeutil import TZ

EVENT_NAMES = {"card.action.trigger": "卡片按钮", "im.message.receive_v1": "私聊回复", "vc.meeting.participant_meeting_ended_v1": "会议结束",
               "vc.note.generated_v1": "逐字稿生成", "task.task.update_user_access_v2": "飞书任务变化"}
CINDY = {"routine": "会议复盘与提前代做", "poll": "事项中心｜飞书回复轮询",
         "jobs": ["事项中心｜每日周期事项", "事项中心｜工作日早间简报", "事项中心｜工作日明日计划"]}
STALE_AI_HOURS = 3


# ---- task snapshot (written by the listener every 10 minutes) --------------------











def _day(value):
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, TZ).date()
        return datetime.fromisoformat(str(value)).date() if value else None
    except ValueError:
        return None


def _when(value):
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, TZ)
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _flat(r):
    """Select cells may arrive as "x", ["x"] or []; make them plain values."""
    out = dict(r)
    for k in ("状态", "执行方", "交付情况", "验收结果"):
        v = out.get(k)
        if isinstance(v, list):
            out[k] = v[0] if v and isinstance(v[0], str) else None
    return out


def classify(rows, now):
    """Turn table rows into the owner's view: what needs me, what is moving, and how it is going."""
    rows = [_flat(r) for r in rows]
    today = now.date()
    need, moving, backlog, done = [], [], [], []
    for r in rows:
        st, ex = r.get("状态"), r.get("执行方")
        if r.get("排除本人提醒") or st == "取消" or not r.get("编号"):
            continue
        try:
            n = int(str(r["编号"]).strip())
        except ValueError:
            continue
        if st == "完成":
            finished = _when(r.get("完成时间"))
            if not finished or now - finished > timedelta(days=7):
                continue
            delivered = r.get("交付情况")
            item = {"n": n, "title": str(r.get("标题") or "")[:26], "who": {"我": "你", "AI": "AI", "别人": "别人"}.get(ex, ""),
                    "due": f"{finished:%m-%d %H:%M} 完成", "late": False, "url": r.get("交付物链接"), "at": finished.isoformat(),
                    "label": {"有交付": "已交付", "无交付": "无需交付", "待补交付": "待补交付"}.get(delivered, "已完成"),
                    "tone": "orange" if delivered == "待补交付" else "green" if delivered == "有交付" else "gray", "note": ""}
            done.append(item)
            if delivered == "待补交付":
                need.append({**item, "label": "补交付", "note": "做完了，但交付文件还没挂上", "reply": f"{n} + 链接 / {n} 无交付"})
            continue
        title = str(r.get("标题") or "")[:26]
        due = _day(r.get("截止日"))
        late = (today - due).days if due else None
        due_text = (f"逾期 {late} 天" if late and late > 0 else "今天到期" if late == 0 else
                    "明天到期" if late == -1 else f"{due:%m-%d} 到期" if due else "")
        item = {"n": n, "title": title, "who": {"我": "你", "AI": "AI", "别人": "别人"}.get(ex, "待定"),
                "due": due_text, "late": bool(late and late > 0), "url": r.get("交付物链接"), "note": ""}
        if st == "待验收":
            again = r.get("验收结果") == "退回" or bool(r.get("退回意见"))
            item.update(label="待你复查" if again else "待你验收", tone="purple" if again else "orange",
                        note=("按你上次的意见改过了，再看一眼" if again else str(r.get("预做结果摘要") or "")[:30]),
                        reply=f"{n} 过 / {n} 退 意见", cmd=f"{n} 过", cmd_label="通过")
            need.append(item)
        elif st == "待确认":
            item.update(label="需要你定", tone="orange", reply=f"{n} 我来 / {n} AI做 / {n} 周五 / {n} 不做",
                        note="谁来做或什么时候做完还没定")
            need.append(item)
        elif st == "卡住":
            blocker = str(r.get("卡点") or "原因没写")
            if ex == "AI":
                item.update(label="卡住", tone="red", note=f"AI 缺：{blocker[:24]}", reply=f"{n} + 材料")
                need.append(item)
            elif "#" in blocker and ex == "我":
                # The child AI item is already in "need you"; the parent just waits for it.
                item.update(label="等 AI 备稿", tone="gray", note=blocker[:28])
                moving.append(item)
            else:
                item.update(label="卡住", tone="red", note=blocker[:28])
                moving.append(item)
        else:
            if ex == "AI":
                updated = _when(r.get("更新时间"))
                stale = updated and now - updated > timedelta(hours=STALE_AI_HOURS) and st == "进行中"
                item.update(label="可能卡住" if stale else "AI 在做", tone="red" if stale else "blue",
                            note=(f"超过 {STALE_AI_HOURS} 小时没进展，系统会重新排队" if stale else str(r.get("进度") or "")[:24]))
            elif ex == "别人":
                if not (late is not None and late >= 0):
                    continue  # others' work only matters here once it is due
                item.update(label="等对方", tone="gray", note=str(r.get("进度") or "")[:24])
            else:
                if (late is None or late < -1) and st != "进行中":
                    backlog.append(item)  # not dated or not due soon: counted, not listed
                    continue
                if late is not None and late > 0:
                    label = "逾期"
                elif st == "进行中":
                    label = "进行中"
                else:
                    label = "今天要做" if late == 0 else "明天要做"
                item.update(label=label, tone="red" if label == "逾期" else "blue", note="",
                            cmd=f"{n} 完成", cmd_label="完成")
            moving.append(item)
    order = {"red": 0, "purple": 1, "orange": 2, "blue": 3, "gray": 4}
    need.sort(key=lambda x: (order.get(x["tone"], 5), x["n"]))
    moving.sort(key=lambda x: (order.get(x["tone"], 5), not x["late"], x["n"]))
    done.sort(key=lambda x: x["at"], reverse=True)
    return need, moving, backlog, done


# ---- system checks: plain words, what was done, what you do ----------------------

def _ro(path):
    return database.read(path, timeout=3)


def _kv(db, key, default=None):
    row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _age(at, now):
    try:
        return (now - datetime.fromisoformat(at)).total_seconds() / 60
    except (TypeError, ValueError):
        return None


def _state(root):
    path = Path(root) / "state/state.db"
    if not path.exists():
        return {}, None, None, {}
    db = _ro(path)
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {}
        if "meeting_queue" in tables:
            counts["meetings_waiting"] = db.execute("SELECT count(*) FROM meeting_queue WHERE status!='done'").fetchone()[0]
        if "chat_candidates" in tables:
            counts["chat_pending"] = db.execute("SELECT count(*) FROM chat_candidates WHERE status='pending'").fetchone()[0]
        counts["links"] = _kv(db, "listen:links", {}) or {}
        counts["distinct"] = _kv(db, "merge:distinct", []) or []
        return (_kv(db, "listen:status", {}) or {}), _kv(db, "listen:heartbeat"), _kv(db, "brief-v1:board-url"), counts
    finally:
        db.close()


def _cindy_runs():
    files = glob(os.path.expanduser("~/Library/Application Support/Cindy/cindy-*.db"))
    out = {}
    if not files:
        return out
    try:
        db = _ro(files[0])
        for name in [CINDY["routine"], CINDY["poll"], *CINDY["jobs"]]:
            rows = db.execute("""SELECT r.fired_at, r.status, r.error_msg FROM schedule_runs r JOIN schedules s ON s.id=r.schedule_id
                                 WHERE s.name=? ORDER BY r.fired_at DESC LIMIT 5""", (name,)).fetchall()
            enabled = db.execute("SELECT status FROM schedules WHERE name=?", (name,)).fetchone()
            out[name] = {"enabled": bool(enabled and enabled[0] == "active"),
                         "runs": [{"at": datetime.fromtimestamp(f / 1000, TZ), "status": s, "error": e or ""} for f, s, e in rows]}
        db.close()
    except sqlite3.Error:
        pass
    return out


PLAIN_ERRORS = [("已有实例正在执行", "整点时和别的检查撞车了。已改成排队等待，下次会自动跑。"),
                ("user_needs_refresh", "飞书登录刚好在自动续期，下一轮会自动恢复。"),
                ("授权", "飞书授权有问题，需要你重新登录 lark-cli。"),
                ("timeout", "飞书接口超时，下一轮会自动重试。")]


def _plain(error):
    for key, words in PLAIN_ERRORS:
        if key in (error or ""):
            return words
    return (error or "原因未记录")[:60]


def _acks(root):
    try:
        return json.loads((Path(root) / "state/monitor-acks.json").read_text())
    except (OSError, ValueError):
        return {}


def clear_resolved(root, now=None):
    """Owner pressed「清掉已处理的」: forget failures that are already fixed or settled."""
    now = now or datetime.now(TZ)
    acks = _acks(root)
    acks["cindy_failures_before"] = now.isoformat(timespec="seconds")
    (Path(root) / "state/monitor-acks.json").write_text(json.dumps(acks, ensure_ascii=False))
    try:
        db = activity._db(root)
        with db:
            db.execute("UPDATE events SET status='resolved' WHERE status='failed' AND kind!='send'")
        db.close()
    except Exception:
        pass


def problems(root, now, streams, heartbeat, beats, errors_today, cindy, links=None):
    out = []
    def add(level, title, what, done, you="", action=None):
        out.append({"level": level, "title": title, "what": what, "done": done, "you": you, "action": action})
    hb = _age(heartbeat, now)
    if hb is None or hb > 3:
        add("error", "实时监听停了", "你在飞书的回复、会前卡片会变慢。",
            "菜单栏应用正在自动重启它。", "如果 2 分钟后还是这样，点「重启监听」。", {"kind": "restart_listener"})
    working = now.weekday() < 5 and 8 <= now.hour < 22
    routine = cindy.get(CINDY["routine"], {})
    last_run = routine.get("runs", [{}])[0].get("at") if routine.get("runs") else None
    beat = _age((beats.get("会议检查") or {}).get("at"), now)
    if working and (beat is None or beat > 15):
        add("warn", "AI 员工没在检查", f"已经 {int(beat) if beat else '很久'} 分钟没检查会议和事项，会后待办、AI 代做都在等。",
            "已在飞书提醒你一次（2 小时内不重复）。", "点「重启监听」，恢复后台检查。", {"kind": "restart_listener"})
    acks = _acks(root)
    for name in CINDY["jobs"]:
        runs = cindy.get(name, {}).get("runs", [])
        cleared = acks.get("cindy_failures_before")
        if runs and cleared and runs[0]["at"].isoformat() <= cleared:
            continue
        if runs and runs[0]["status"] == "failed" and now - runs[0]["at"] < timedelta(hours=24):
            add("info", f"「{name.split('｜')[-1]}」{runs[0]['at']:%H:%M} 那次没跑成", _plain(runs[0]["error"]),
                "已修复，下次会正常运行；当时的简报已由补发机制送达。" if name.endswith("明日计划") else "已修复，下次会正常运行。")
    missing = [EVENT_NAMES.get(k, k) for k, v in streams.items() if v == "not_subscribed"]
    if missing:
        add("setup", "一次性设置：订阅飞书推送", "、".join(missing) + " 还没订阅，现在靠定时检查，会慢一些。",
            "订阅后自动生效，不用重启。", "点「去订阅」，在飞书开放平台各点一次确认。",
            {"kind": "subscribe", "urls": [u for k, u in (links or {}).items() if streams.get(k) == "not_subscribed"]})
    sends_failed = [e for e in errors_today if e["kind"] == "send"]
    if sends_failed:
        add("error", "有消息没发出去", f"{sends_failed[0]['at'][11:16]} 的「{sends_failed[0]['title'][:20]}」发送失败。",
            "为防止重复发送，结果不明的不会自动重发。", "点「立即检查」，或在飞书里看这条有没有收到。")
    other = [e for e in errors_today if e["kind"] != "send" and _age(e["at"], now) is not None and _age(e["at"], now) < 120]
    if other:
        add("warn", f"最近 2 小时出错 {len(other)} 次", f"最近一次：{other[0]['title']}（{_plain(other[0]['detail'])}）",
            "出错的那一轮会在下一轮自动重试。", "如果反复出现同一个错，告诉我。")
    return out


# ---- summary ---------------------------------------------------------------------

def _proactive(root, need):
    try:
        from . import proactive
        return {"alerts": proactive.alerts(root), "recent": proactive.recent(root), "focus": proactive.focus(need),
                "ai": proactive.ai_line(root)}
    except Exception as exc:
        activity.log(root, "error", "主动层出错", detail=f"{type(exc).__name__}: {exc}", status="failed")
        return {"alerts": {"quiet": False, "items": []}, "recent": [], "focus": {"total": len(need)}}


def _unread(root):
    try:
        from . import inbox
        return inbox.unread(root)
    except Exception:
        return 0


def worker_view(root, rows, moving, now):
    """Exactly one「AI 正在做」(with its step and minutes), the rest of the AI queue as「排队第 N」.

    Returns the AI employee's state for its own panel section:
    {"state": working|extracting|checking|idle, "line", "n", "title", "step", "minutes", "queue", "last"}."""
    from .workenv import is_running, read_status
    ws = read_status(root)
    running = is_running(root)
    n_of, title_of = {}, {}
    for r in rows:
        try:
            n_of[r.get("record_id")] = int(str(r.get("编号")).strip())
            title_of[r.get("record_id")] = str(r.get("标题") or "")[:26]
        except (TypeError, ValueError):
            pass
    by_n = {x["n"]: x for x in moving}
    info = {"state": "idle", "line": "AI 员工：空闲，有新的事会自动开始", "n": None, "title": "", "step": "", "minutes": 0}
    if running and ws.get("kind") == "item" and ws.get("record_id"):
        n = n_of.get(ws["record_id"])
        started = _when(ws.get("item_started") or ws.get("stage_at"))
        minutes = max(0, int((now - started).total_seconds() // 60)) if started else 0
        step = ws.get("step") or ws.get("stage") or ""
        info.update(state="working", n=n, title=title_of.get(ws["record_id"]) or ws.get("title") or "", step=step, minutes=minutes,
                    line=f"AI 员工：正在做 #{n} · {step} · {minutes} 分钟")
        if n in by_n:
            by_n[n].update(label="AI 正在做", tone="blue", note=f"{step} · 已 {minutes} 分钟"[:28])
    elif running and ws.get("kind") in {"meeting", "chat"}:
        info.update(state="extracting", title=ws.get("title") or "", step=ws.get("stage") or "提取待办",
                    line=f"AI 员工：{ws.get('stage') or '提取待办'}（{ws.get('title') or ''}）")
    elif running:
        info.update(state="checking", line="AI 员工：在看有没有要做的")
    queue = []
    for i, q in enumerate(ws.get("queue") or [] if running else [], 1):
        n = n_of.get(q.get("record_id"))
        queue.append({"n": n, "title": title_of.get(q.get("record_id")) or q.get("title") or ""})
        x = by_n.get(n)
        if x and x.get("label") != "AI 正在做":
            x.update(label=f"排队第 {i}", tone="gray", note="做完前面的就轮到它")
    info["queue"] = queue
    last = ws.get("last") or {}
    if last:
        at = _when(last.get("at"))
        info["last"] = {"n": last.get("n"), "title": last.get("title") or "", "outcome": last.get("outcome") or "",
                        "text": last.get("text") or "", "time": f"{at:%H:%M}" if at else ""}
    order = {"AI 正在做": 0}
    moving.sort(key=lambda x: (order.get(x.get("label"), 2 if not str(x.get("label", "")).startswith("排队") else 1),
                               int(str(x.get("label", "")).replace("排队第", "").strip() or 0) if str(x.get("label", "")).startswith("排队") else 0))
    return info


def build(root, now=None):
    now = now or datetime.now(TZ)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    events, beats = activity.recent(root, since=today)
    streams, heartbeat, board, counts = _state(root)
    snap = _snapshot(root)
    try:
        need, moving, backlog, done = classify(snap["rows"], now)
    except Exception as exc:  # one odd row must never blank the whole panel
        activity.log(root, "error", "面板整理事项时出错", detail=f"{type(exc).__name__}: {exc}", status="failed")
        need, moving, backlog, done = [], [], [], []
    try:
        worker = worker_view(root, snap["rows"], moving, now)
    except Exception as exc:
        worker = {"state": "error", "line": "AI 员工：状态读不到", "queue": []}
        activity.log(root, "error", "面板读 AI 员工状态出错", detail=f"{type(exc).__name__}: {exc}", status="failed")
    # Things that look like the same item twice: suggest merging (owner decides; nothing is merged here).
    from .merge import duplicate_pairs
    for p in duplicate_pairs(snap["rows"], counts.pop("distinct", []))[:3]:
        need.append({"n": p["drop"], "title": p["drop_title"], "label": "可能重复", "tone": "purple", "who": "", "due": "",
                     "late": False, "url": None, "note": f"和 #{p['keep']} {p['keep_title'][:14]} 像是同一件事",
                     "reply": f"{p['drop']} 合并到 {p['keep']} / {p['drop']} 不重复",
                     "cmd": f"{p['drop']} 合并到 {p['keep']}", "cmd_label": "合并"})
    cindy = _cindy_runs()
    errors = [e for e in events if e["status"] == "failed"]  # 'resolved' failures are settled and not shown
    issues = problems(root, now, streams, heartbeat, beats, errors, cindy, counts.pop("links", {}))
    timeline, seen = [], {}
    for e in events[:120]:  # newest first; the same thing repeated today shows once with a count
        kind = {"send": "发给你", "detect": "检测到", "reply": "你的回复", "prework": "AI 进展", "run": "定时", "error": "出错"}.get(e["kind"], e["kind"])
        if e["kind"] == "error":
            continue
        k = (kind, e["title"])
        if k in seen:
            seen[k]["count"] += 1
            continue
        seen[k] = {"id": e["id"], "time": e["at"][11:16], "kind": kind, "title": e["title"], "ok": e["status"] != "failed", "count": 1}
        timeline.append(seen[k])
    for x in timeline:
        if x["count"] > 1:
            x["title"] += f"（今天 ×{x['count']}）"
    serious = [i for i in issues if i["level"] in {"error", "warn"}]
    level = "error" if any(i["level"] == "error" for i in issues) else "warn" if serious else "ok"
    if level == "error":
        headline = issues[0]["title"]
    elif need:
        headline = f"需要你处理 {len(need)} 件"
    elif serious:
        headline = serious[0]["title"]
    else:
        headline = "一切正常，没有要你处理的"
    stuck = sum(1 for x in moving if x["tone"] == "red")
    return {"generated_at": now.isoformat(timespec="seconds"), "level": level, "headline": headline,
            "summary": f"正在进行 {len(moving)} 件" + (f"（{stuck} 件卡住或逾期）" if stuck else "") +
                       f" · 今天发给你 {sum(1 for e in events if e['kind'] == 'send' and e['status'] == 'ok')} 条",
            "worker": worker, "unread": _unread(root),
            # 主动层: what to interrupt for (and whether to hold it), and the one-glance numbers for the menu
            **_proactive(root, need),
            "need_you": need, "in_progress": moving, "backlog_count": len(backlog), "done_recent": done[:20],
            "timeline": timeline, "issues": issues,
            "snapshot_at": snap.get("at"), "board_url": board, "counts": counts,
            "auto_restart_listener": any(i["action"] and i["action"]["kind"] == "restart_listener" for i in issues),
            "latest_event_id": max((e["id"] for e in events), default=0),
            "notify": [{"id": e["id"], "kind": e["kind"], "title": e["title"], "ok": e["status"] != "failed"}
                       for e in events[:30] if e["kind"] in {"send", "error", "prework"} or e["status"] == "failed"]}


def alert_if_cindy_stalled(root, client, state, now):
    """Called by the listener every minute: note it once (in 2 hours) when Cindy stops checking. It goes to the page's
    status dot, not to the owner's chat: the menu-bar app and the tray restart the listener by themselves, and the
    built-in GPT stands in for an AI that cannot be woken; there is nothing the owner could do with the message."""
    _, beats = activity.recent(root, since=now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(), limit=1)
    beat = _age((beats.get("会议检查") or {}).get("at"), now)
    working = now.weekday() < 5 and 8 <= now.hour < 22
    if not working or (beat is not None and beat <= 15):
        return None
    last = state.get("monitor:cindy-alert")
    if last and now - datetime.fromisoformat(last) < timedelta(hours=2):
        return "already_alerted"
    minutes = f"{int(beat)} 分钟" if beat else "很久"
    from . import inbox
    inbox.add(root, "cindy-stalled-" + now.strftime("%Y%m%d%H%M"),
              f"⚠ AI 员工已经 {minutes}没检查会议和事项了。心响正在自动重启后台，本机 GPT 会先顶上。", kind="system")
    state.set("monitor:cindy-alert", now.isoformat())
    return "alerted"


# ---- local page --------------------------------------------------------------------

PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8"><meta http-equiv="refresh" content="15">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>事项中心</title><style>
:root{{--bg:#f5f6f8;--card:#fff;--ink:#1d2129;--muted:#86909c;--line:#e8e9ec;--red:#e5484d;--orange:#e8770e;--purple:#8e4ec6;--blue:#2f6fed;--gray:#86909c;--green:#2b9a66}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151618;--card:#1f2023;--ink:#e6e7ea;--muted:#8b9099;--line:#2e3034}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 -apple-system,"PingFang SC",sans-serif}}
main{{max-width:760px;margin:0 auto;padding:24px 16px 40px}} h1{{font-size:20px;margin:0}} .sub{{color:var(--muted);font-size:13px;margin:4px 0 18px}}
section{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:6px 16px;margin-bottom:14px}}
h2{{font-size:13px;color:var(--muted);font-weight:600;margin:10px 0 4px}}
.item{{display:flex;gap:10px;align-items:flex-start;padding:10px 0;border-top:1px solid var(--line)}} h2+.item{{border-top:0}}
.tag{{flex:none;font-size:12px;padding:1px 8px;border-radius:99px;color:#fff;margin-top:2px}}
.t-green{{background:var(--green)}}.t-red{{background:var(--red)}}.t-orange{{background:var(--orange)}}.t-purple{{background:var(--purple)}}.t-blue{{background:var(--blue)}}.t-gray{{background:var(--gray)}}
.main{{flex:1;min-width:0}} .title{{font-weight:600}} .note{{color:var(--muted);font-size:13px}} .reply{{font-size:12px;color:var(--muted)}}
a.btn{{flex:none;font-size:13px;color:var(--blue);text-decoration:none;margin-top:2px}} .time{{flex:none;width:44px;color:var(--muted);font-variant-numeric:tabular-nums}}
.issue{{padding:10px 0;border-top:1px solid var(--line)}} h2+.issue{{border-top:0}} .issue b{{display:block}} .issue div{{font-size:13px}}
.empty{{color:var(--muted);padding:8px 0 12px}}</style></head><body><main>
<h1>{headline}</h1><div class="sub">{summary} · 更新于 {updated}</div>
{urgent}
<section><h2>AI 员工</h2>{worker}</section>
<section><h2>需要你（{need_count}）</h2>{need}</section>
<section><h2>正在进行（{moving_count}）</h2>{moving}{backlog}</section>
<section><h2>最近完成（7 天）</h2>{done}</section>
<section><h2>今天发生了什么</h2>{timeline}</section>
{later}
</main></body></html>"""


def render(s):
    e = html.escape
    def item(x):
        link = f'<a class="btn" href="{e(x["url"])}">打开</a>' if x.get("url") else ""
        meta = " · ".join(v for v in [x["who"] + "来做" if x["who"] in {"你", "AI"} else x["who"], x["due"]] if v)
        reply = f'<div class="reply">回复：{e(x["reply"])}</div>' if x.get("reply") else ""
        return (f'<div class="item"><span class="tag t-{x["tone"]}">{e(x["label"])}</span><div class="main">'
                f'<div class="title">#{x["n"]} {e(x["title"])}</div><div class="note">{e(" · ".join(v for v in [x["note"], meta] if v))}</div>{reply}</div>{link}</div>')
    def issue(i):
        return (f'<div class="issue"><b>{"⚠" if i["level"] in {"warn", "error"} else "⚙" if i["level"] == "setup" else "ℹ"} {e(i["title"])}</b>'
                f'<div>{e(i["what"])}</div><div class="note">系统：{e(i["done"])}</div>'
                + (f'<div>你：{e(i["you"])}</div>' if i["you"] else "") + "</div>")
    urgent = "".join(issue(i) for i in s["issues"] if i["level"] in {"error", "warn"})
    w = s.get("worker") or {}
    wlines = [e(w.get("line") or "AI 员工：状态读不到")]
    if w.get("queue"):
        wlines.append("排队：" + "、".join(e(f"#{q['n']} {q['title']}") for q in w["queue"]))
    if w.get("last"):
        l = w["last"]
        wlines.append(e(f"上一件：#{l['n']} {l['title']} · {l['outcome']} {l['time']} · {l['text']}"))
    worker = "".join(f'<div class="item"><div class="main">{x}</div></div>' for x in wlines)
    later = "".join(issue(i) for i in s["issues"] if i["level"] not in {"error", "warn"})
    return PAGE.format(
        headline=e(s["headline"]), summary=e(s["summary"]), updated=e(s["generated_at"][11:16]), worker=worker,
        urgent=f'<section><h2>要先处理</h2>{urgent}</section>' if urgent else "",
        later=f'<section><h2>系统说明（不影响使用）</h2>{later}</section>' if later else "",
        need_count=len(s["need_you"]), need="".join(item(x) for x in s["need_you"]) or '<div class="empty">没有要你处理的</div>',
        moving_count=len(s["in_progress"]), moving="".join(item(x) for x in s["in_progress"]) or '<div class="empty">暂无</div>',
        done="".join(item(x) for x in s.get("done_recent", [])) or '<div class="empty">最近 7 天还没有完成的事</div>',
        backlog=(f'<div class="empty">另有 {s["backlog_count"]} 件你的待办没定日期或还没到期，在看板里</div>' if s.get("backlog_count") else ""),
        timeline="".join(f'<div class="item"><span class="time">{x["time"]}</span><div class="main"><span class="note">{e(x["kind"])}</span> {e(x["title"])}'
                         f'{"" if x["ok"] else " ✗"}</div></div>' for x in s["timeline"][:40]) or '<div class="empty">今天还没有动静</div>')


def main(argv=None):
    p = argparse.ArgumentParser(description="事项中心本地监控")
    p.add_argument("--config", type=Path, default=Path("config.yaml"))
    p.add_argument("--json", action="store_true")
    p.add_argument("--write-html", action="store_true")
    p.add_argument("--test-send", action="store_true")
    p.add_argument("--test-card", action="store_true", help="发一张带按钮的测试卡片，点了原地变成「收到」")
    p.add_argument("--refresh", action="store_true", help="马上从飞书重读事项，刷新本地快照（心响点完按钮后用）")
    p.add_argument("--clear-resolved", action="store_true", help="清掉已经处理好的旧提示")
    args = p.parse_args(argv)
    root = args.config.resolve().parent
    if args.clear_resolved:
        clear_resolved(root)
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
    if args.refresh:
        from .config import load
        from .feishu import Feishu
        from .state import State, exclusive_lock
        from .workflow import Gateway
        client = Feishu(load(args.config.resolve()), root)
        with exclusive_lock(root / "state", wait=60):
            state = State(root / "state/state.db")
            try:
                client.verify_owner(client.auth())
                rows = Gateway(client, state).rows()
                save_snapshot(root, rows)
            finally:
                state.close()
        print(json.dumps({"ok": True, "rows": len(rows)}))
        return 0
    if args.test_card:
        from .brief_v1 import _block, _row_item, card
        from .config import load
        from .feishu import Feishu
        client = Feishu(load(args.config.resolve()), root)
        client.verify_owner(client.auth())
        now = datetime.now(TZ)
        c = card("按钮测试", "", "blue", [_block("点一下右边的按钮", "blue", [
            _row_item(("这一行点完会原地变成「✓ 收到」", "不会改任何事项", "grey", None), cmd="__ping__", cmd_label="点我")])],
                 "点了没变化，说明卡片回调还没生效。", None)
        receipt = client.send_card(c, "monitor-card-" + now.strftime("%Y%m%d%H%M%S"))
        print(json.dumps({"ok": True, "message_id": receipt.get("message_id")}, ensure_ascii=False))
        return 0
    status = build(root)
    if args.write_html:
        out = root / "state/monitor.html"
        out.write_text(render(status))
        status["page"] = str(out)
    print(json.dumps(status, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
