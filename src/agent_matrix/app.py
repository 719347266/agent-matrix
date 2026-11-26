"""心响 local client: one window for everything. Feishu is only the database.

    python -m agent_matrix.app            # serve on 127.0.0.1:<app_port> (the listener starts this for you)
    python -m agent_matrix.app --open     # and open it in the browser

The page (app/xinxiang.html) reads /api/state and acts through /api/cmd, /api/say and /api/read.
Every action runs exactly the same reply command as a typed Feishu reply ("44 过", "44 + 加上竞品"),
so clicking, typing in the app and replying on Feishu all behave the same.

Only this machine can use it: the server binds 127.0.0.1, checks the Host header (no DNS rebinding)
and requires a per-machine token that is only handed to the page it serves.
"""
import argparse
from datetime import datetime, timedelta
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
from urllib.parse import urlparse
import webbrowser

from contextlib import closing
from . import activity
from . import db as database
from .timeutil import TZ

POLL_CHOICES = (1, 5, 10, 15, 30, 60)  # 多久读一次聊天, minutes

PAGE = Path(__file__).with_name("app") / "xinxiang.html"


def token(root):
    path = Path(root) / "state/app-token"
    try:
        value = path.read_text().strip()
        if re.fullmatch(r"[0-9a-f]{32}", value):
            return value
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_hex(16)
    path.write_text(value)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return value


# ---- the data the page shows -------------------------------------------------------------------------

def _url(value):
    if not value:
        return None
    m = re.search(r"\((https?://[^)\s]+)\)", str(value))
    if m:
        return m.group(1)
    m = re.search(r"https?://\S+", str(value))
    return m.group(0).rstrip("]") if m else None


def _clean(text):
    text = re.sub(r"<cite[^>]*>|</cite>", "", str(text or ""))
    return re.sub(r"<[^>]+>", "", text).strip()


def _first(value):
    return (value or [None])[0] if isinstance(value, list) else value


def _day(value):
    return str(value)[:10] if value else None


def _hm(value):
    v = str(value or "")
    return f"{v[5:10]} {v[11:16]}".strip() if len(v) >= 16 else v[5:10]


def _num(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def build_state(root):
    from . import inbox, thread
    from .snapshot import _snapshot
    from .workenv import is_running, read_status
    root = Path(root)
    broken = {}

    def part(name, fn, default):
        """One part of the page. If it fails the page still opens: that part is left empty and says why."""
        try:
            return fn()
        except Exception as exc:
            import traceback
            broken[name] = f"{type(exc).__name__}: {exc}"[:240]
            activity.swallowed(root, f"app.build_state.{name}", exc)
            print(f"心响页面「{name}」出错：", traceback.format_exc(), file=sys.stderr, flush=True)
            return default
    snap = part("事项", lambda: _snapshot(root), {})
    rows = snap.get("rows") or []
    n_of = {r.get("record_id"): _num(r.get("编号")) for r in rows}
    history = part("对话记录", lambda: thread.history(root), {})
    from .reply_timing import measure
    timings, timing_summary = part("回复速度", lambda: measure(history, datetime.now(TZ)), ({}, {}))
    closures = part("完成记录", lambda: thread.closures(root), {})
    try:
        from . import sessions as ai_sessions
        sessions_of, running_sessions = ai_sessions.by_item(root), ai_sessions.running(root)
    except Exception:
        sessions_of, running_sessions = {}, []
    items, threads, deliverables = [], {}, {}
    try:  # which items the owner strung together, and which look related but are not linked yet
        from . import itemlinks
        from .textmatch import similarity
        linked = itemlinks.pairs(root)
        by_ai = {pair for pair, who in itemlinks.made_by(root).items() if who == "AI"}
    except Exception:
        linked, by_ai = [], set()
    before_of, after_of, ai_of = {}, {}, {}
    for a, b in linked:
        if n_of.get(a) is not None and n_of.get(b) is not None:
            if (a, b) in by_ai:  # the page marks these, so a wrong guess is easy to spot and undo
                ai_of.setdefault(a, []).append(n_of[b])
                ai_of.setdefault(b, []).append(n_of[a])
            after_of.setdefault(a, []).append(n_of[b])
            before_of.setdefault(b, []).append(n_of[a])
    titled = [(r["record_id"], r.get("标题") or "") for r in rows if n_of.get(r.get("record_id")) is not None and not r.get("排除本人提醒")]

    parent_of = {r.get("record_id"): _first(r.get("父事项")) for r in rows}

    def suggest(rid, title):
        joined = {x for pair in linked if rid in pair for x in pair}
        joined |= {parent_of.get(rid)} | {c for c, p in parent_of.items() if p == rid}  # its own 备材料 / parent
        scored = sorted(((similarity(title, t), other) for other, t in titled if other != rid and other not in joined), reverse=True)
        return [n_of[other] for score, other in scored[:3] if score >= 0.45]
    def add_item(r):   # one item at a time: a broken record is left out and named, the rest still shows
        n = n_of.get(r.get("record_id"))
        if n is None:
            return
        parent = _first(r.get("父事项"))
        url = _url(r.get("交付物链接"))
        item = {"n": n, "rid": r["record_id"], "title": r.get("标题") or "", "who": r.get("执行方") or "", "status": r.get("状态") or "",
                "due": _day(r.get("截止日")), "progress": r.get("进度") or "", "blocker": r.get("卡点"), "url": url,
                "summary": r.get("预做结果摘要"), "quote": _clean(r.get("原话引用")), "source": r.get("来源"),
                "source_url": _url(r.get("来源链接")), "criterion": r.get("完成标准"), "extra": r.get("补充材料"),
                "rejected": r.get("退回意见"), "parent": n_of.get(parent), "next": r.get("下一步动作"),
                "finished": _day(r.get("完成时间")), "hidden": bool(r.get("排除本人提醒")),
                "updated": str(r.get("更新时间") or "")[:19], "created": str(r.get("创建时间") or "")[:19],
                "finished_at": str(r.get("完成时间") or "")[:16].replace("T", " "),
                "reply_timing": timings.get(r["record_id"]), "closed": closures.get(r["record_id"]), "sessions": sessions_of.get(r["record_id"], []),
                "before": before_of.get(r["record_id"], []), "after": after_of.get(r["record_id"], []),
                "ai_linked": ai_of.get(r["record_id"], []),
                "related": suggest(r["record_id"], r.get("标题") or "") if linked is not None else [],
                "draft_reply": part("回复草稿", lambda: _draft_reply(root, r), None),
                "draft_notice": part("群通知草稿", lambda: _draft_notice(root, r), None)}
        items.append(item)
        h = history.get(r["record_id"], {})
        msgs = [{"at": _hm(r.get("创建时间")), "from": "system", "kind": "say", "text": f"从{item['source'] or '手动'}进表"}] \
            if r.get("创建时间") else []
        msgs += h.get("messages", [])
        dv = h.get("deliverables") or []
        if not dv and url:  # delivered before 心响 kept versions
            dv = [{"v": 1, "at": _hm(r.get("完成时间")), "summary": item["summary"] or "", "url": url, "confirm": "", "assumptions": [], "sources": []}]
        if dv and not any(m["kind"] == "deliver" for m in msgs):
            msgs += [{"at": v["at"], "from": "ai", "kind": "deliver", "text": v["summary"] or "交付", "url": v["url"], "driver": "AI"} for v in dv]
        threads[n], deliverables[n] = msgs, dv

    for r in rows:
        part(f"事项 #{_num(r.get('编号')) or r.get('record_id')}", lambda r=r: add_item(r), None)
    try:  # which project each item is in: where the owner put it, or a guess shown as a guess
        from . import projects
        project_list, placed = projects.all(root), projects.place(root, rows)
    except Exception:
        project_list, placed = [], {}
    for item in items:
        item["project"], item["project_by"] = placed.get(item["rid"], (None, None))
    from . import local_sessions
    external = part("本机 AI 会话", lambda: local_sessions.view(root, sessions_of, running_sessions, items), [])
    from . import cindy_pickup
    pickups = part("Cindy 接手", lambda: cindy_pickup.latest(root), {})
    from . import aidrivers
    handed = part("单件交给的 AI", lambda: aidrivers.assignments(root), {})
    from . import versions as version_store
    in_version = {}
    for v in part("项目和版本", lambda: version_store.all(root), []):
        for x in v["items"]:
            in_version[x["rid"]] = {"id": v["id"], "name": f"{v['project']} · {v['name']}", "status": v["status_label"]}
    for item in items:
        item["version"] = in_version.get(item["rid"])
        item["cindy_pickup"] = pickups.get(item["rid"])
        item["ai_for"] = handed.get(item["rid"])   # handed to one AI on its own; None = the AI chosen in settings
        item["sessions"] = sessions_of.get(item["rid"], [])
    part("来源和人", lambda: _origins(root, rows, items), None)
    part("看起来办完了", lambda: _done_hints(root, items), None)
    from . import drive
    ws, running = part("AI 员工状态", lambda: (read_status(root), is_running(root)), ({}, False))
    jobs = part("正在做的活", lambda: drive.open_jobs(root), [])

    def minutes(at):
        try:
            return max(0, int((datetime.now(TZ) - datetime.fromisoformat(at)).total_seconds() // 60))
        except (TypeError, ValueError):
            return 0
    doing = [{"n": j.get("n"), "kind": j["kind"], "title": j.get("title") or "", "driver": j.get("driver") or "AI",
              "step": j.get("step") or "", "minutes": minutes(j.get("created"))} for j in jobs]
    current = next((d for d in doing if d["kind"] == "item"), doing[0] if doing else None)
    last = ws.get("last") or None
    worker = {"running": running or bool(jobs), "current": current, "doing": doing, "drivers": part("AI 列表", lambda: drive.drivers(root), []),
              "queue": [n_of.get(q.get("record_id")) for q in (ws.get("queue") or []) if n_of.get(q.get("record_id"))] if running else [],
              "last": {"n": _num(last.get("n")), "title": last.get("title"), "outcome": last.get("outcome"), "text": last.get("text"),
                       "time": str(last.get("at", ""))[11:16]} if last else None}
    box = [{"id": m["id"], "time": m["time"] if m["day"] == datetime.now(TZ).date().isoformat() else m["day"][5:] + " " + m["time"],
            "title": m["title"], "text": m["summary"], "kind": m["kind"], "n": (m["refs"] or [None])[0], "refs": m["refs"] or [],
            "blocks": m["blocks"], "at": str(m.get("at") or "")[:19], "read": m["read"]}  # blocks: the whole message, opened in place
           for m in part("动态", lambda: inbox.messages(root, 150), [])]
    judged = {n_of[rid]: j for rid, j in part("判断记录", lambda: thread.judgments(root), {}).items() if n_of.get(rid)}
    settings = {"auto_assign": bool(thread.setting(root, "auto_assign", False)),
                "builtin_driver": bool(thread.setting(root, "builtin_driver", True)),
                "engine": thread.setting(root, "engine", "codex"),
                "onboarded": bool(thread.setting(root, "onboarded", False)),
                "guide_version": int(thread.setting(root, "guide_version", 0) or 0),   # which first-run guide the owner went through
                "prework_enabled": _config_flag(root, "prework_enabled"),
                "worker_mcp": _config_flag(root, "worker_mcp_tools")}
    settings["engine_sort"] = thread.setting(root, "engine_sort", settings["engine"] if settings["engine"] != "cindy" else "codex")
    settings["pickup_buddy"] = (root / "state/cindy-pickup.json").is_file()
    settings["cindy_project"] = thread.setting(root, "cindy_project", "")
    from . import approvals
    settings["dev_mode"] = approvals.mode(root)
    settings["people"] = part("人的分组", lambda: _people_tags(root), {})   # who the owner marked as 客户 / 同事
    waiting = [{**a, "n": n_of.get(a.get("record_id"))} for a in approvals.pending(root)]
    try:
        from . import local_sessions
        settings["cindy"] = local_sessions.cindy_status(root)
        if settings["cindy"].get("installed"):
            settings["cindy"]["setup_text"] = local_sessions.cindy_setup_text(root)
    except Exception:
        settings["cindy"] = {}
    try:
        from . import aidrivers
        settings["drivers"] = aidrivers.known(root)   # every AI that came and said who it is
    except Exception as exc:
        activity.swallowed(root, "app.drivers", exc)
        settings["drivers"] = []
    settings["engine_tests"] = {k: thread.setting(root, f"engine_test:{k}", None) for k in ("codex", "claude", "gemini", "api")}
    from .engines import fallback_engine
    settings["engine_fallback"] = fallback_engine(root) or ""   # 叫不醒时谁来顶上; "" = nobody
    try:  # waking the AIs in other apps, and whether the built-in driver stands in for one right now
        from . import wakeup
        settings["wake"] = wakeup.status(root)
    except Exception as exc:
        activity.swallowed(root, "app.wake", exc)
        settings["wake"] = {}
    try:  # where conversations open and items are picked up (设置 → AI 对话)
        from . import local_sessions
        from .config import load as load_config
        found = local_sessions.apps(load_config(root / "config.yaml"))
        cfg = load_config(root / "config.yaml")
        settings["models"] = {"codex": cfg.worker_model, "claude": cfg.worker_claude_model, "gemini": cfg.worker_gemini_model}
        settings["want_feishu"] = bool(thread.setting(root, "want_feishu", False))
        settings["want_slack"] = bool(thread.setting(root, "want_slack", False))
        settings["want_wechat"] = bool(thread.setting(root, "want_wechat", False))
        from . import setupshim
        settings["setup_prompt"] = setupshim.prompt(root)   # the one message the owner pastes to their own AI
        settings["part_prompts"] = {part: setupshim.part_prompt(part) for part in setupshim.PARTS}   # one part to a second AI
        settings["setup"] = setup_view(root)   # what the owner asked the driving AI to set up, and how far it got
        settings["gemini_key"] = bool(cfg.worker_gemini_api_key)   # only whether one is set; the key itself never leaves the config
        from .engines import API_PROVIDERS, api_target
        settings["api"] = {"provider": cfg.worker_api_provider, "base": cfg.worker_api_base_url, "model": cfg.worker_api_model,
                           "key": bool(cfg.worker_api_key), "target": api_target(cfg),
                           "providers": [{"id": k, **v} for k, v in API_PROVIDERS.items()]}
        from . import hymem   # 记忆增强: the settings without the keys, and how far writing has got
        settings["memory_boost"] = {"enabled": cfg.memory_boost_enabled, "base_url": cfg.memory_base_url, "has_key": bool(cfg.memory_api_key),
                                    "llm": cfg.memory_llm_model, "embed_base_url": cfg.memory_embed_base_url, "has_embed_key": bool(cfg.memory_embed_api_key),
                                    "embed": cfg.memory_embed_model, "dims": cfg.memory_embed_dims, "installed": hymem.installed(),
                                    "ready": hymem.ready(cfg), "presets": hymem.PRESETS, "status": hymem.status(root)}
        settings["me"] = {"name": cfg.owner_name, "aliases": cfg.owner_aliases}
        settings.update(intake_mode=cfg.intake_mode, brief_channel=cfg.brief_channel,
                        sources={"meetings": cfg.meeting_watch_enabled, "chats": cfg.chat_watch_enabled, "tasks": cfg.feishu_tasks_enabled})
        settings.update(apps=found, pickup_app=local_sessions.pickup_choice(root, found),
                        gpt_open=local_sessions.gpt_open_choice(root, found))
    except Exception as exc:
        activity.swallowed(root, "app.build_state", exc)
    return {"demo": False, "page": page_version(), "today": datetime.now(TZ).date().isoformat(), "generated_at": datetime.now(TZ).strftime("%H:%M:%S"),
            "settings": settings, "judgments": judged, "reply_timing_summary": timing_summary,
            "guide": part("接入说明", lambda: drive.guide_text(str(drive.shim_path())), ""),
            **part("记忆", lambda: memory_view(root, items), {}),
            "snapshot_at": str(snap.get("at") or "")[11:16], "items": items, "projects": project_list, "threads": threads, "deliverables": deliverables,
            "worker": worker, "approvals": waiting, "inbox": box, "running": running_sessions, "local_sessions": external,
            "system": {**part("系统状态", lambda: system_info(root), {}), "store": part("存储", lambda: store_info(root), {}),
                       "update": part("版本更新", lambda: _update_status(root), {})}, "bugs": _bugs(root), "broken": broken}


def _people_tags(root):
    from . import people
    return people.tags(root)


def _origins(root, rows, items):
    """item["origin"]: the meeting, chat or task list it came from and who it is about (people.origin). A 备材料 made
    from another item has no source of its own: it takes its parent's."""
    from . import people
    from .config import load
    cfg = load(Path(root) / "config.yaml")
    self_names = {"我", "本人", cfg.owner_name, *cfg.owner_aliases} - {"", None}
    meetings, chats = people.known(root)
    tags = people.tags(root)
    by_rid, by_n = {r.get("record_id"): r for r in rows}, {i["n"]: i for i in items}
    for item in items:
        item["origin"] = people.origin(by_rid.get(item["rid"]) or {}, item.get("source_url"), meetings, chats, self_names, tags)
    for item in items:
        parent = by_n.get(item.get("parent"))
        if parent and item["origin"]["kind"] == "手动":
            item["origin"] = parent["origin"]


def _done_hints(root, items):
    """item["done_hint"]: the AI found the owner's open item already done somewhere (donecheck), with the words."""
    from . import donecheck
    hints = donecheck.hints(root)
    for item in items:
        h = hints.get(item["rid"])
        if h and not h.get("dismissed") and item.get("status") not in ("完成", "取消"):
            item["done_hint"] = {k: h.get(k) for k in ("evidence", "where", "url", "at")}


def _draft_notice(root, row):
    """The group notice the AI wrote (e.g. a PR is up), waiting for the owner's 发送 (or already sent)."""
    from . import replies
    entry = replies.get(root, row["record_id"], replies.NOTICE)
    if not entry or entry.get("status") == "dropped":
        return None
    return entry


def _draft_reply(root, row):
    """The reply the AI wrote for the person who asked, waiting for the owner's 发送 (or already sent / dropped)."""
    from . import replies
    entry = replies.get(root, row["record_id"])
    if not entry or entry.get("status") == "dropped":
        return None
    return {**entry, "where": replies.where(replies.target(row))}


def _bugs(root):
    try:
        from . import bugs
        return bugs.all(root)
    except Exception as exc:
        activity.swallowed(root, "app.bugs", exc)
        return []


def _update_status(root):
    try:
        from . import update
        update.check_soon(root)   # looks at the releases page in the background, a few times a day at most
        return update.status(root)
    except Exception as exc:
        activity.swallowed(root, "app.update_status", exc)
        return {}


def page_version():
    """Changes whenever the page file changes, so an open window can notice and reload itself."""
    from hashlib import sha256
    try:
        return sha256(PAGE.read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


def store_info(root):
    """Where the items live, and how far the Feishu backup is behind."""
    from .monitor import _local_store
    if not _local_store(root):
        return {"local": False}
    from . import itemstore
    st = itemstore.status(root)
    return {"local": True, "pending": st["pending"], "failed": st["failed"], "ok_at": str(st.get("ok_at") or "")[5:16].replace("T", " ")}


def memory_view(root, items):
    """记忆层 for the page: the owner's rules, what waits on a condition, recent decisions, and per item what
    was said about its topic before."""
    from . import memory
    try:
        def moment(m):
            return {"id": m["id"], "kind": m["kind"], "label": memory.KIND_LABEL.get(m["kind"], ""), "text": m["text"],
                    "quote": (m.get("quote") or "")[:200], "source": m.get("source_title") or "", "at": str(m["at"])[5:16],
                    "wait_for": m.get("wait_for"), "due": (m.get("due") or "")[:10] or None, "status": m.get("status")}
        waiting = [moment(m) for m in memory.moments(root, kind="waiting", limit=40) if m["status"] in {"open", "alerted"}]
        recent = [moment(m) for m in memory.moments(root, limit=60) if m["kind"] in {"decision", "open_question"} and m["status"] != "stale"][:20]
        history = {}
        for i in items:
            if i["hidden"] or i["status"] in {"完成", "取消"}:
                continue
            lines = [moment(m) for m in memory.recall(root, f"{i['title']} {i.get('next') or ''}", limit=4)]
            if lines:
                history[i["n"]] = lines
        from .jobstore import open_jobs
        jobs = open_jobs(root)
        scanning = next(({"by": j.get("driver"), "step": j.get("step") or ""} for j in jobs if j["kind"] == "profile"), None)
        from . import tidy, versions
        n_of = {i["rid"]: i for i in items}
        archive = [{**v, "items": [{"n": n_of[x["rid"]]["n"], "title": n_of[x["rid"]]["title"], "status": n_of[x["rid"]]["status"], "by": x["by"]}
                                   for x in v["items"] if x["rid"] in n_of]} for v in versions.all(root)]
        organizing = next(({"by": j.get("driver"), "step": j.get("step") or ""} for j in jobs if j["kind"] == "organize"), None)
        since = (datetime.now(TZ) - timedelta(days=30)).isoformat(timespec="seconds")
        verdicts = memory.outcomes(root, since=since)
        from . import library
        lib = [{"name": e["name"], "description": e["description"], "sections": [t for t, _ in e["sections"]], "raw": e["raw"]}
               for e in library.entries(root)]
        return {"memory": {"library": lib, "library_problems": library.problems(root), "waiting": waiting, "recent": recent,
                           "profile": {**memory.profile(root), "draft": memory.draft(root), "asked": memory.scan_asked(root), "scanning": scanning},
                           "versions": archive, "organize": {"asked": versions.organize_asked(root), "doing": organizing},
                           "verdicts": {k: sum(1 for o in verdicts if o["verdict"] == k) for k in ("通过", "退回", "要改")},
                           "effect": _effect(root),
                           "updates": [{k: u[k] for k in ("id", "type", "label", "content", "why", "created")} | {"old": u["data"].get("old") or ""}
                                       | {"detail": _draft_detail(u["data"]) if u["type"] == "memory" else ""}
                                       for u in tidy.updates(root)],
                           "tidying": next(({"by": j.get("driver"), "step": j.get("step") or ""} for j in jobs if j["kind"] == "tidy"), None)},
                "history": history}
    except Exception as exc:
        return {"memory": {"library": [], "waiting": [], "recent": [], "error": str(exc)[:120]}, "history": {}}


def _draft_detail(d):
    """What a 经验库 draft would actually change, for the owner to read before 用."""
    parts = []
    if d.get("old_text"):
        parts.append(("挪走这一节：" if d.get("mode") == "move" else "原文：") + "\n" + d["old_text"].strip())
    if d.get("description"):
        parts.append("目录描述：" + d["description"])
    if d.get("body"):
        parts.append(("改成：" if d.get("mode") == "revise" else "加上：") + "\n" + d["body"].strip())
    return "\n\n".join(parts)


def _effect(root):
    """效果: how the AI's deliverables fared, week by week (stats.py, cached ~10 minutes)."""
    try:
        from . import stats
        return stats.cached(root)
    except Exception as exc:
        activity.swallowed(root, "app.effect", exc)
        return {"has": False}


def system_info(root):
    import sqlite3
    names = {"im.message.receive_v1": "飞书回复", "card.action.trigger": "卡片按钮", "task.task.update_user_access_v2": "飞书任务",
             "vc.meeting.participant_meeting_ended_v1": "会议结束", "vc.note.generated_v1": "纪要生成"}
    try:
        db = database.read(Path(root) / "state/state.db")
        status = json.loads((db.execute("SELECT value FROM kv WHERE key='listen:status'").fetchone() or ["{}"])[0])
        beat = json.loads((db.execute("SELECT value FROM kv WHERE key='listen:heartbeat'").fetchone() or ["null"])[0])
        slack = json.loads((db.execute("SELECT value FROM kv WHERE key='slack:identity'").fetchone() or ["null"])[0])
        jira = json.loads((db.execute("SELECT value FROM kv WHERE key='jira:identity'").fetchone() or ["null"])[0])
        wechat_status = json.loads((db.execute("SELECT value FROM kv WHERE key='wechat:status'").fetchone() or ["null"])[0])
        feishu_health = json.loads((db.execute("SELECT value FROM kv WHERE key='feishu:health'").fetchone() or ["null"])[0])
        notion_url = json.loads((db.execute("SELECT value FROM kv WHERE key='notion:url'").fetchone() or ["null"])[0])
        db.close()
    except Exception:
        status, beat, slack, jira, notion_url, wechat_status, feishu_health = {}, None, None, None, None, None, None
    alive = False
    try:
        alive = datetime.now(TZ) - datetime.fromisoformat(beat) < timedelta(minutes=3)
    except (TypeError, ValueError):
        pass
    from .config import load
    from .workenv import claude_binary, codex_binary, gemini_binary
    try:
        config = load(Path(root) / "config.yaml")
        found = {"codex": bool(codex_binary(config)), "claude": bool(claude_binary(config)), "gemini": bool(gemini_binary(config))}
        mode = {"local": "feishu", "feishu": "feishu"}.get(config.item_store, config.item_store)
        record = {"review_every": config.chat_review_every_minutes, "poll_every": config.chat_poll_minutes, "review_times": config.chat_review_times, "mode": mode, "feishu_ready": bool(config.base_token and config.tables.get("事项表") not in (None, "local")),
                  "notion": bool(config.notion_database_id), "notion_url": notion_url,
                  "skip_chats": config.chat_skip_chats, "skip_words": config.chat_skip_words, "recent_chats": _recent_chats(root)}
    except Exception:
        found, record = {}, {}
    try:  # what the setup check needs to say "do this next" in the owner's own words and paths
        import shutil
        lark, npm = bool(shutil.which(getattr(config, "lark_binary", "") or "lark-cli")), bool(shutil.which("npm"))
    except Exception:
        lark, npm = False, False
    here = {"platform": "windows" if sys.platform.startswith("win") else "mac" if sys.platform == "darwin" else "other",
            "root": str(Path(root).resolve()), "python": re.sub(r"(?i)pythonw(\.exe)$", r"python\1", sys.executable), "lark_cli": lark, "npm": npm,
            "client": bool(os.environ.get("XINXIANG_CLIENT"))}   # started by the desktop client, which keeps the listener alive itself
    try:
        from .sources import wechat as wechat_source
        wechat = {"installed": bool(wechat_source.binary(config)), "enabled": bool(config.wechat_enabled), "status": wechat_status}
    except Exception:
        wechat = {"installed": False, "enabled": False, "status": None}
    # connected on paper is not enough: a few failed sign-in checks in a row mean nothing is being read
    broken = bool(feishu_health and feishu_health.get("ok") is False and feishu_health.get("count", 0) >= 3)
    record["feishu_broken"] = {"why": feishu_health.get("why") or "", "since": feishu_health.get("since") or ""} if broken and record.get("feishu_ready") else None
    try:   # what keeps failing right now (a proxy that is off, permissions Feishu refuses, columns missing)
        problems = activity.problems(root)
    except Exception:
        problems = {}
    if not problems.get("proxy") and (down := local_proxy_down()):
        problems["proxy"] = down
    return {"engines": found, "slack": slack, "jira": jira, "wechat": wechat, "here": here, "record": record, "problems": problems, "push": status, "listener": "运行中" if alive else "没在运行", "subs": "、".join(f"{names.get(k, k)} {'✓' if v == 'live' else '未订阅'}"
                                                                    for k, v in status.items())}


# ---- actions -----------------------------------------------------------------------------------------

def _python(root, *args, timeout=150):
    run = subprocess.run([sys.executable, "-m", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    return (run.stdout or "").strip() or (run.stderr or "").strip()[-300:]


def run_command(root, text):
    """Exactly what a typed Feishu reply does; then re-read the table so the page shows the change."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()[:500]
    if not text:
        return {"ok": False, "reply": "没有内容。"}
    reply = _python(root, "agent_matrix.reply", "--text", text)
    if reply == "NOT_A_COMMAND":
        return {"ok": False, "reply": "没看懂。格式是「编号 + 动作」，比如：44 过 / 44 + 加上竞品 / 44 先做"}
    # Re-read the table in the background; the page reloads a few seconds later.
    threading.Thread(target=_python, args=(root, "agent_matrix.monitor", "--refresh"), kwargs={"timeout": 120}, daemon=True).start()
    return {"ok": "✓" in reply, "reply": reply}


def chatlog(root, limit=500):
    """Every chat line read lately and what became of it, newest first: went to an item, folded into one, skipped by
    the AI (with its reason), dropped in code before the AI saw it (with the reason), or still waiting."""
    import sqlite3
    from .snapshot import _snapshot
    n_of = {r.get("record_id"): _num(r.get("编号")) for r in _snapshot(Path(root)).get("rows") or []}
    try:
        with closing(database.read(Path(root) / "state/state.db")) as db:
            got = db.execute("SELECT message_id, chat_name, chat_mode, sender_name, sent_at, text, kind, status "
                             "FROM chat_candidates ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for mid, chat, mode, sender, at, text, kind, status in got:
        at = str(at or "")
        if at.isdigit():  # Lark: milliseconds
            at = datetime.fromtimestamp(int(at) / (1000 if len(at) > 10 else 1), TZ).strftime("%Y-%m-%d %H:%M")
        status = str(status or "")
        verdict, _, why = status.partition(":")
        if verdict in ("item", "merged"):
            why = n_of.get(why)
        elif verdict == "done":  # before verdicts were kept: only known that the AI looked at it
            verdict, why = "seen", ""
        out.append({"id": mid, "chat": chat, "p2p": mode == "p2p", "who": sender, "at": at[:16], "text": text,
                    "kind": kind, "verdict": verdict, "why": why,
                    "url": mid if str(mid).startswith("https://") else None})
    return out


def _recent_chats(root, limit=30):
    """Chat names seen lately, so the filter can be picked instead of typed."""
    import sqlite3
    try:
        with closing(database.read(Path(root) / "state/state.db")) as db:
            return [r[0] for r in db.execute("SELECT chat_name, MAX(sent_at) m FROM chat_candidates GROUP BY chat_name ORDER BY m DESC LIMIT ?", (limit,))]
    except sqlite3.Error:
        return []


def _config_flag(root, name):
    from .config import load
    try:
        return bool(getattr(load(root / "config.yaml"), name))
    except Exception:
        return False


def say(root, n, text):
    """The owner talks to an item's AI conversation: recorded as an addition, and the AI picks it up."""
    from . import thread
    from .snapshot import _snapshot
    row = next((r for r in _snapshot(root).get("rows") or [] if _num(r.get("编号")) == _num(n)), None)
    text = re.sub(r"\s+", " ", str(text or "")).strip()[:1000]
    if not row or not text:
        return {"ok": False, "reply": "找不到这件事，或者没有内容。"}
    if row.get("状态") in {"完成", "取消"}:
        thread.add(root, row["record_id"], "me", "feedback", text)
        return {"ok": True, "reply": f"#{n} 已记进对话（这件事已经{row.get('状态')}，没有重新打开）"}
    return run_command(root, f"{_num(n)} + {text}")


# ---- HTTP --------------------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "xinxiang"

    def log_message(self, *args):
        pass

    def _ok_host(self):
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _send(self, code, body, kind="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        return self._ok_host() and secrets.compare_digest(self.headers.get("X-Xinxiang-Token", ""), self.server.token)

    def do_GET(self):
        if not self._ok_host():
            return self._send(403, {"error": "forbidden"})
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            page = PAGE.read_text(encoding="utf-8")
            page = page.replace("/*__PAGE__*/\"\"", json.dumps(page_version())).replace("/*__TOKEN__*/\"\"", json.dumps(self.server.token))
            return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        if path.startswith("/doc/"):  # a deliverable kept on this computer (no Feishu): readable like any document link
            from . import localdoc
            parts = path.split("/")
            text = localdoc.read(self.server.root, *parts[2:4]) if len(parts) == 4 else None
            if text is None:
                return self._send(404, "这份交付已经不在这台电脑上了。".encode("utf-8"), "text/plain; charset=utf-8")
            return self._send(200, localdoc.page(text).encode("utf-8"), "text/html; charset=utf-8")
        if path == "/icon.png":  # the app icon doubles as the page logo
            icon = Path(__file__).resolve().parent / "app/icon.png"   # travels with the program, on every system
            if icon.is_file():
                return self._send(200, icon.read_bytes(), "image/png")
            return self._send(404, {"error": "no icon"})
        if path == "/api/state":
            if not self._authorized():
                return self._send(403, {"error": "forbidden"})
            try:
                commands = self.server.commands.recent()
                return self._send(200, {**build_state(self.server.root), "commands": commands})
            except Exception as exc:
                import traceback
                print("心响页面数据出错：", traceback.format_exc(), file=sys.stderr, flush=True)   # into logs/listen.log
                return self._send(500, {"error": f"{type(exc).__name__}: {exc}"[:300]})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            return self._send(403, {"error": "forbidden"})
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 100_000)
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        root, path = self.server.root, urlparse(self.path).path
        try:
            if path in {"/api/commands", "/api/cmd", "/api/say"}:
                if path == "/api/say":
                    body = {**body, "kind": "say"}
                command = self.server.commands.submit(body)
                return self._send(202, {"ok": True, "command": command, "reply": command["message"]})
            if path == "/api/commands/retry":
                command = self.server.commands.retry(str(body.get("id") or ""))
                return self._send(202, {"ok": True, "command": command})
            if path == "/api/commands/dismiss":
                self.server.commands.dismiss(str(body.get("id") or ""))
                return self._send(200, {"ok": True})
            if path == "/api/hymem":  # 记忆增强: save the owner's endpoint and key, test them, or search (for AI jobs)
                from . import hymem
                from .config import load as load_config, save as save_config
                config = load_config(root / "config.yaml")
                if body.get("search") is not None:
                    try:
                        return self._send(200, {"ok": True, "lines": hymem.search(root, config, body["search"], int(body.get("limit") or 8))})
                    except Exception as exc:
                        return self._send(200, {"ok": False, "lines": [], "error": f"{type(exc).__name__}: {exc}"[:200]})
                if body.get("test"):
                    return self._send(200, hymem.test(root, config))
                save = body.get("save") or {}
                fields = {"enabled": "memory_boost_enabled", "base_url": "memory_base_url", "llm": "memory_llm_model",
                          "embed_base_url": "memory_embed_base_url", "embed": "memory_embed_model", "dims": "memory_embed_dims",
                          "key": "memory_api_key", "embed_key": "memory_embed_api_key"}
                was_on = hymem.ready(config)
                for k, attr in fields.items():
                    if k not in save or (k in ("key", "embed_key") and not str(save[k] or "").strip()):
                        continue   # an empty key box keeps the key already saved
                    v = save[k]
                    setattr(config, attr, bool(v) if k == "enabled" else int(v or 1024) if k == "dims" else str(v or "").strip()[:300])
                save_config(root / "config.yaml", config)
                hymem.mark(root, hymem.ready(config))
                queued = hymem.backfill(root) if hymem.ready(config) and not was_on else 0   # just switched on: start from what 心响 knows
                return self._send(200, {"ok": True, "ready": hymem.ready(config), "queued": queued})
            if path == "/api/profile":  # 关于你: the owner writes or corrects it; every AI reads it before work
                from . import memory
                if body.get("scan") is not None:   # 让 AI 认识我: the places the owner ticked
                    if not body["scan"]:
                        memory.cancel_scan(root)
                        return self._send(200, {"ok": True})
                    try:
                        memory.ask_scan(root, list(body["scan"]))
                    except ValueError as exc:
                        return self._send(400, {"ok": False, "reply": str(exc)})
                    return self._send(200, {"ok": True, **setup_view(root)["driver"]})
                before = memory.profile(root)["text"].strip()
                if "draft" in body:                # the owner keeps the AI's draft, or throws it away
                    try:
                        got = memory.drop_draft(root, use=bool(body["draft"]))
                    except ValueError as exc:
                        return self._send(409, {"ok": False, "reply": str(exc)})
                else:
                    got = memory.set_profile(root, body.get("text"))
                if got["text"].strip() != before:
                    from . import hymem
                    hymem.remember_profile(root, got["text"], before)
                return self._send(200, {"ok": True, "profile": got})
            if path == "/api/organize":  # 整理项目和版本: the AI sorts the work; the owner fixes what it got wrong
                from . import versions
                from .snapshot import _snapshot
                if body.get("ask"):
                    versions.ask_organize(root)
                    return self._send(200, {"ok": True, **setup_view(root)["driver"]})
                if body.get("cancel"):
                    versions.organize_done(root)
                if body.get("expected") and body.get("status"):
                    versions.set_expected(root, body["expected"], body["status"])
                if body.get("n") is not None and "version" in body:   # the owner moves one item (0 = out of any version)
                    rid = next((r["record_id"] for r in _snapshot(root).get("rows") or [] if str(r.get("编号")) == str(body["n"])), None)
                    if not rid:
                        return self._send(400, {"ok": False, "reply": "找不到这件事。"})
                    versions.place(root, rid, int(body["version"] or 0) or None, by=versions.OWNER)
                return self._send(200, {"ok": True})
            if path == "/api/rules":   # what was left waiting on a condition is settled
                from . import memory
                if body.get("waiting_done"):
                    memory.set_status(root, body["waiting_done"], "done")
                return self._send(200, {"ok": True})
            if path == "/api/memory/library":   # the owner edits the 经验库 directly: no review for their own hand
                from . import library
                try:
                    if body.get("delete"):
                        library.delete(root, str(body["delete"]))
                    else:
                        text = str(body.get("text") or "")
                        library.save(root, library.parse(text)["name"], text, body.get("old_name"))
                except ValueError as exc:
                    return self._send(400, {"ok": False, "reply": str(exc)})
                return self._send(200, {"ok": True})
            if path == "/api/memory/updates":  # 记忆更新: 用 applies what the daily tidy proposed, 不要 leaves the memory alone
                from . import tidy
                pick = lambda v: [u["id"] for u in tidy.updates(root)] if v == "all" else [int(x) for x in (v if isinstance(v, list) else [v])]
                from . import memory
                before = memory.profile(root)["text"].strip() if body.get("use") else ""
                used, failed = tidy.use(root, pick(body["use"])) if body.get("use") else ([], {})
                if used:
                    after = memory.profile(root)["text"].strip()
                    if after != before:
                        from . import hymem
                        hymem.remember_profile(root, after, before)
                if body.get("drop"):
                    tidy.drop(root, pick(body["drop"]))
                return self._send(200, {"ok": not failed, "used": len(used), "failed": {str(k): v for k, v in failed.items()},
                                        "reply": "；".join(failed.values())[:200], "left": len(tidy.updates(root))})
            if path == "/api/chatlog":  # what became of each chat line read (设置 → 聊天判断)
                return self._send(200, {"ok": True, "lines": chatlog(root)})
            if path == "/api/item/new":  # the owner adds an item by hand
                from . import manual
                from .models import MatrixBusy, MatrixError
                try:
                    return self._send(200, {"ok": True, **manual.add(root, body.get("title"), body.get("who") or "我", body.get("due"), body.get("note"))})
                except MatrixBusy:
                    return self._send(400, {"ok": False, "reply": "后台正忙，过几秒再点一次。"})
                except MatrixError as exc:
                    return self._send(400, {"ok": False, "reply": str(exc)})
            if path == "/api/wechat/check":  # can WeChat be read right now (takes about 20 seconds)
                from .config import load
                from .sources import wechat
                return self._send(200, wechat.check(load(root / "config.yaml")))
            if path == "/api/bugs":  # write a problem down, take one away, or get them all as one text to hand in
                from . import bugs
                if body.get("add"):
                    bugs.add(root, body["add"])
                if body.get("remove"):
                    bugs.remove(root, body["remove"])
                if body.get("sent"):
                    bugs.mark_sent(root)
                out = {"ok": True, "bugs": bugs.all(root)}
                if body.get("report"):
                    out.update(report=bugs.report(root), issue_url=bugs.issue_url(root))
                return self._send(200, out)
            if path == "/api/update":  # look for a newer version, or get it
                from . import update
                if body.get("install"):
                    return self._send(200, update.start(root))
                update.check(root, force=True)
                return self._send(200, {"ok": True, **update.status(root)})
            if path == "/api/onboard/start":  # the guide's last step: what the owner chose goes to the AI, in order
                from . import memory, setupasks, thread
                s = system_info(root)
                have = {"feishu": bool((s.get("record") or {}).get("feishu_ready")), "slack": bool(s.get("slack")),
                        "wechat": bool((s.get("wechat") or {}).get("enabled") and (s.get("wechat") or {}).get("installed"))}
                for tool in [t for t in body.get("tools") or [] if t in have]:
                    thread.set_setting(root, "want_" + tool, True)
                    if not have[tool]:
                        setupasks.ask(root, tool)
                scan = [x for x in body.get("scan") or [] if x in memory.SCAN_SOURCES]
                if scan:
                    memory.ask_scan(root, scan)
                if body.get("organize"):   # after 认识你: sort the work into project → version
                    from . import versions
                    versions.ask_organize(root)
                return self._send(200, {"ok": True, **setup_view(root)["driver"]})
            if path == "/api/setup":  # 接入飞书 / Slack / 微信, or 剩下没配好的: the AI that drives 心响 does it
                from . import setupasks, thread
                what = str(body.get("what") or "")
                if what not in setupasks.WHAT:
                    return self._send(400, {"ok": False, "reply": "不知道要接什么。"})
                if body.get("cancel"):
                    setupasks.done(root, what)
                    return self._send(200, {"ok": True})
                if what != "fix":
                    thread.set_setting(root, "want_" + what, True)
                thread.set_setting(root, "setup:gave_up:" + what, None)
                setupasks.ask(root, what)
                return self._send(200, {"ok": True, **setup_view(root)["driver"]})
            if path == "/api/item/ai":  # hand this one item to another AI (or back to the one chosen in settings)
                from . import aidrivers, drive
                from .config import load
                from .models import MatrixBusy, MatrixError
                from .snapshot import _snapshot
                row = next((r for r in _snapshot(root).get("rows") or [] if str(r.get("编号")) == str(body.get("n"))), None)
                to = str(body.get("to") or "")
                names = {"driver:" + d["name"] for d in aidrivers.known(root)}
                if not row:
                    return self._send(400, {"ok": False, "reply": "找不到这件事。"})
                if to not in {"", aidrivers.HANDOFF, "cindy", *aidrivers.LOCAL} | names:
                    return self._send(400, {"ok": False, "reply": "没有这个 AI。"})
                try:
                    return self._send(200, drive.hand_over(root, load(root / "config.yaml"), row["record_id"], to, by="本人",
                                                           n=row.get("编号"), title=str(row.get("标题") or "")))
                except MatrixBusy:
                    return self._send(400, {"ok": False, "reply": "后台正忙，过几秒再点一次。"})
                except MatrixError as exc:
                    return self._send(400, {"ok": False, "reply": str(exc)})
            if path == "/api/item/deliverable":  # the owner adds, corrects or removes a deliverable version
                from . import deliverables
                from .models import MatrixBusy, MatrixError
                try:
                    return self._send(200, {"ok": True, **deliverables.change(
                        root, int(body.get("n") or 0), str(body.get("op") or ""), version=body.get("v"),
                        url=body.get("url"), summary=body.get("summary"))})
                except MatrixBusy:
                    return self._send(400, {"ok": False, "reply": "后台正忙，过几秒再点一次。"})
                except MatrixError as exc:
                    return self._send(400, {"ok": False, "reply": str(exc)})
            if path == "/api/person":  # the owner says who this is: 客户 or 同事 ("" = back to the guess)
                from . import people
                try:
                    tags = people.set_kind(root, body.get("name"), body.get("kind") or "")
                except ValueError as exc:
                    return self._send(400, {"ok": False, "reply": str(exc)})
                return self._send(200, {"ok": True, "people": tags})
            if path == "/api/donehint":  # 「看起来办完了」 was wrong: not suggested again for the same words
                from . import donecheck
                if not donecheck.dismiss(root, str(body.get("rid") or "")):
                    return self._send(400, {"ok": False, "reply": "这件事没有待确认的「看起来办完了」。"})
                return self._send(200, {"ok": True})
            if path == "/api/projects":  # the owner's projects: add one, rename one, take one away
                from . import projects
                from .models import MatrixError
                try:
                    made = projects.create(root, body["add"]) if body.get("add") else None
                    if body.get("rename"):
                        projects.rename(root, body["rename"], body.get("name"))
                    if body.get("remove"):
                        projects.remove(root, body["remove"])
                except MatrixError as exc:
                    return self._send(400, {"ok": False, "reply": str(exc)})
                return self._send(200, {"ok": True, "id": made, "projects": projects.all(root)})
            if path == "/api/item/prompt":  # the words the AI got for this item, as they were sent
                from .drive import prompt_path
                rid = str(body.get("rid") or "")
                target = prompt_path(root, rid) if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", rid) else None
                if not target or not target.is_file():
                    return self._send(200, {"ok": True, "text": "", "at": ""})
                at = datetime.fromtimestamp(target.stat().st_mtime, TZ).strftime("%m-%d %H:%M")
                return self._send(200, {"ok": True, "text": target.read_text(encoding="utf-8"), "at": at})
            if path == "/api/item/project":  # put items in a project (0 = none)
                from . import projects
                from .models import MatrixError
                from .snapshot import _snapshot
                rid = {int(r.get("编号") or 0): r.get("record_id") for r in _snapshot(root).get("rows") or []}
                ids = [rid[int(n)] for n in body.get("numbers") or [] if rid.get(int(n))]
                if not ids:
                    return self._send(400, {"ok": False, "reply": "找不到这个编号的事项。"})
                try:
                    projects.assign(root, ids, body.get("project"))
                except MatrixError as exc:
                    return self._send(400, {"ok": False, "reply": str(exc)})
                return self._send(200, {"ok": True})
            if path == "/api/item/link":  # string two items together (or take the link away): earlier → later
                from . import itemlinks
                from .models import MatrixError
                from .snapshot import _snapshot
                rid = {int(r.get("编号") or 0): r.get("record_id") for r in _snapshot(root).get("rows") or []}
                earlier, later = rid.get(int(body.get("earlier") or 0)), rid.get(int(body.get("later") or 0))
                if not earlier or not later:
                    return self._send(400, {"ok": False, "reply": "找不到这个编号的事项。"})
                try:
                    itemlinks.link(root, earlier, later) if body.get("on", True) else itemlinks.unlink(root, earlier, later)
                except MatrixError as exc:
                    return self._send(400, {"ok": False, "reply": str(exc)})
                return self._send(200, {"ok": True})
            if path in ("/api/reply/send", "/api/reply/drop"):   # only ever from the owner's own click
                from . import replies
                from .models import MatrixError
                from .snapshot import _snapshot
                rid = str(body.get("rid") or "")
                if path.endswith("drop"):
                    ok = replies.discard(root, rid)
                    return self._send(200 if ok else 400, {"ok": ok, "reply": "" if ok else "这条回复已经发过或不发了。"})
                row = next((r for r in _snapshot(root).get("rows") or [] if r.get("record_id") == rid), None)
                if not row:
                    return self._send(400, {"ok": False, "reply": "找不到这件事。"})
                from .config import load as load_config
                try:
                    return self._send(200, replies.send(root, load_config(root / "config.yaml"), row, body.get("text")))
                except MatrixError as exc:
                    return self._send(400, {"ok": False, "reply": str(exc)})
                except Exception as exc:   # Feishu / Slack refused: say so, the draft stays for another try
                    activity.swallowed(root, "app.reply_send", exc)
                    return self._send(400, {"ok": False, "reply": f"没发出去（{type(exc).__name__}: {str(exc)[:120]}），草稿还在，可以再点一次。"})
            if path in ("/api/notice/send", "/api/notice/drop"):   # only ever from the owner's own click
                from . import replies
                from .models import MatrixError
                rid = str(body.get("rid") or "")
                if path.endswith("drop"):
                    ok = replies.discard(root, rid, replies.NOTICE)
                    return self._send(200 if ok else 400, {"ok": ok, "reply": "" if ok else "这条群通知已经发过或不发了。"})
                from .config import load as load_config
                try:
                    return self._send(200, replies.send_notice(root, load_config(root / "config.yaml"), rid, body.get("text")))
                except MatrixError as exc:
                    return self._send(400, {"ok": False, "reply": str(exc)})
                except Exception as exc:   # Slack refused: say so, the draft stays for another try
                    activity.swallowed(root, "app.notice_send", exc)
                    return self._send(400, {"ok": False, "reply": f"没发出去（{type(exc).__name__}: {str(exc)[:120]}），草稿还在，可以再点一次。"})
            if path == "/api/approval":
                from . import approvals
                ok = approvals.decide(root, str(body.get("id") or ""), bool(body.get("allow")), body.get("note"))
                return self._send(200 if ok else 400, {"ok": ok, "reply": "" if ok else "这一步已经不在等了（AI 那一轮可能已结束）"})
            if path == "/api/backup/retry":
                from . import itemstore
                return self._send(200, {"ok": True, "retried": itemstore.retry_failed(root)})
            if path == "/api/settings":
                from . import thread
                from . import aidrivers
                ai_names = {aidrivers.setting_of(d["name"]) for d in aidrivers.known(root)}
                if body.get("engine") in {"codex", "claude", "gemini", "api", "cindy"} | ai_names:
                    thread.set_setting(root, "engine", body["engine"])
                from . import approvals
                if body.get("dev_mode") in approvals.MODES:
                    thread.set_setting(root, "dev_mode", body["dev_mode"])
                for want in ("want_feishu", "want_slack", "want_wechat"):   # the owner means to connect it: the setup check then says how
                    if want in body:
                        thread.set_setting(root, want, bool(body[want]))
                if "cindy_project" in body:
                    thread.set_setting(root, "cindy_project", str(body["cindy_project"] or "")[:500])
                if body.get("engine_sort") in {"codex", "claude", "gemini", "api", "cindy"} | ai_names:
                    thread.set_setting(root, "engine_sort", body["engine_sort"])
                if body.get("engine_fallback") in {"codex", "claude", "gemini", "api", ""}:
                    thread.set_setting(root, "engine_fallback", body["engine_fallback"])
                if "cindy_wake_url" in body:   # the address of Cindy's webhook trigger, pasted by the owner: tried at once
                    try:
                        hook = aidrivers.set_hook(root, "cindy", str(body["cindy_wake_url"] or ""))
                    except ValueError as exc:
                        return self._send(400, {"ok": False, "error": str(exc)})
                    if hook:
                        tried = aidrivers.send_wake(hook, "test")
                        return self._send(200, {"ok": True, "wake": tried})
                if any(k in body for k in ("worker_api_provider", "worker_api_base_url", "worker_api_key", "worker_api_model")):
                    from .config import load as load_config, save as save_config   # 模型接口: kept in config.yaml on this computer
                    from .engines import API_PROVIDERS
                    config = load_config(root / "config.yaml")
                    if body.get("worker_api_provider") in API_PROVIDERS:
                        config.worker_api_provider = body["worker_api_provider"]
                    for key in ("worker_api_base_url", "worker_api_key", "worker_api_model"):
                        if key in body:
                            setattr(config, key, str(body[key] or "").strip()[:300])
                    save_config(root / "config.yaml", config)
                if "worker_gemini_api_key" in body:  # kept in config.yaml on this computer, handed to the gemini command only
                    from .config import load as load_config, save as save_config
                    config = load_config(root / "config.yaml")
                    config.worker_gemini_api_key = str(body["worker_gemini_api_key"] or "").strip()[:200]
                    save_config(root / "config.yaml", config)
                if any(k in body for k in ("worker_model", "worker_claude_model", "worker_gemini_model")):  # model names live in config.yaml
                    from .config import load as load_config, save as save_config
                    config = load_config(root / "config.yaml")
                    for key in ("worker_model", "worker_claude_model", "worker_gemini_model"):
                        if key in body:
                            value = " ".join(str(body[key] or "").split())[:80]
                            if key == "worker_model" and not value:
                                continue  # GPT needs a model name; Claude may use the account default
                            setattr(config, key, value)
                    save_config(root / "config.yaml", config)
                from .local_sessions import GPT_OPEN, PICKUP
                if body.get("pickup_app") in PICKUP:
                    thread.set_setting(root, "pickup_app", body["pickup_app"])
                if body.get("gpt_open") in GPT_OPEN:
                    thread.set_setting(root, "gpt_open", body["gpt_open"])
                if "builtin_driver" in body:
                    thread.set_setting(root, "builtin_driver", bool(body["builtin_driver"]))
                if "onboarded" in body:
                    thread.set_setting(root, "onboarded", bool(body["onboarded"]))
                if isinstance(body.get("guide_version"), int):
                    thread.set_setting(root, "guide_version", body["guide_version"])
                switches = ("meeting_watch_enabled", "chat_watch_enabled", "feishu_tasks_enabled", "wechat_enabled")
                if (body.get("intake_mode") in ("all", "accepted", "manual") or body.get("brief_channel") in ("feishu", "local")
                        or any(k in body for k in switches)):
                    from .config import load as load_config, save as save_config
                    config = load_config(root / "config.yaml")
                    if body.get("brief_channel") in ("feishu", "local"):
                        config.brief_channel = body["brief_channel"]
                    for key in switches:
                        if key in body:
                            setattr(config, key, bool(body[key]))
                    if config.meeting_watch_enabled and not config.meeting_watch_started_at:  # only meetings from today on
                        config.meeting_watch_started_at = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
                    if body.get("intake_mode") in ("all", "accepted", "manual"):
                        config.intake_mode = body["intake_mode"]
                        if config.intake_mode != "manual":  # what is taken in gets worked on
                            config.prework_enabled = True
                        if config.intake_mode == "all":      # 全能: the AI also decides on its own what it can do
                            thread.set_setting(root, "auto_assign", True)
                    save_config(root / "config.yaml", config)
                if "owner_name" in body or "owner_aliases" in body:  # how the owner is addressed in meetings and chats
                    from .config import load as load_config, save as save_config
                    config = load_config(root / "config.yaml")
                    name = " ".join(str(body.get("owner_name") or "").split())[:40]
                    if name:  # an empty name would make every meeting action "not the owner's"
                        config.owner_name = name
                    if isinstance(body.get("owner_aliases"), list):
                        config.owner_aliases = list(dict.fromkeys(str(a).strip()[:20] for a in body["owner_aliases"] if str(a).strip()))[:10]
                    save_config(root / "config.yaml", config)
                if (body.get("chat_review_every_minutes") in (0, 5, 15, 30, 60) or body.get("chat_poll_minutes") in POLL_CHOICES
                        or "prework_enabled" in body or "worker_mcp" in body
                        or "chat_skip_chats" in body or "chat_skip_words" in body):
                    from .config import load, save  # config.yaml: the listener and worker read these there
                    config = load(root / "config.yaml")
                    if body.get("chat_poll_minutes") in POLL_CHOICES:
                        config.chat_poll_minutes = body["chat_poll_minutes"]
                    if body.get("chat_review_every_minutes") in (0, 5, 15, 30, 60):
                        config.chat_review_every_minutes = body["chat_review_every_minutes"]
                    if "prework_enabled" in body:
                        config.prework_enabled = bool(body["prework_enabled"])
                    if "worker_mcp" in body:
                        config.worker_mcp_tools = ["*"] if body["worker_mcp"] else []
                    for key in ("chat_skip_chats", "chat_skip_words"):
                        if isinstance(body.get(key), list):
                            setattr(config, key, list(dict.fromkeys(str(w).strip()[:40] for w in body[key] if str(w).strip()))[:100])
                    save(root / "config.yaml", config)
                    if config.prework_enabled and body.get("prework_enabled"):
                        from .workenv import kick  # start on the waiting AI items now, not at the next 2-minute tick
                        threading.Thread(target=kick, args=(root, config), kwargs={"force": True}, daemon=True).start()
                if "auto_assign" in body:
                    thread.set_setting(root, "auto_assign", bool(body["auto_assign"]))
                    if body["auto_assign"]:  # judge the waiting items right away
                        from .config import load
                        from .workenv import kick
                        kick(root, load(root / "config.yaml"), force=True)
                return self._send(200, {"ok": True})
            if path in ("/api/store", "/api/notion/connect"):
                from .models import MatrixError
                from . import notion, storemode
                try:
                    if path == "/api/notion/connect":
                        return self._send(200, {"ok": True, **notion.connect(root, body.get("token"), body.get("page"))})
                    return self._send(200, {"ok": True, "mode": storemode.choose(root, body.get("mode"))})
                except MatrixError as exc:
                    return self._send(400, {"ok": False, "reply": str(exc)})
                except OSError:
                    return self._send(400, {"ok": False, "reply": "连不上 Notion，检查网络后再试。"})
            if path.startswith("/api/jira/"):
                from .models import MatrixError
                from .sources import jira
                try:
                    if path == "/api/jira/connect":
                        return self._send(200, {"ok": True, **jira.connect(root, body.get("site"), body.get("email"), body.get("token"))})
                    if path == "/api/jira/disconnect":
                        jira.disconnect(root)
                        return self._send(200, {"ok": True})
                except MatrixError as exc:
                    return self._send(400, {"ok": False, "reply": str(exc)})
                except OSError:
                    return self._send(400, {"ok": False, "reply": "连不上 Jira，检查地址和网络后再试。"})
            if path.startswith("/api/slack/"):
                from .config import load
                from .models import MatrixError
                from .sources import slack
                try:
                    if path == "/api/slack/connect":
                        return self._send(200, {"ok": True, **slack.connect(root, body.get("token"))})
                    if path == "/api/slack/disconnect":
                        slack.disconnect(root)
                        return self._send(200, {"ok": True})
                    if path == "/api/slack/approvals/connect":
                        return self._send(200, {"ok": True, **slack.connect_approvals(root, body.get("bot"), body.get("app"))})
                    if path == "/api/slack/approvals/disconnect":
                        slack.disconnect_approvals(root)
                        return self._send(200, {"ok": True})
                    if path == "/api/slack/try":
                        hours = min(max(float(body.get("hours") or 24), 1), 168)
                        return self._send(200, {"ok": True, **slack.dry_run(load(root / "config.yaml"), hours=hours)})
                except MatrixError as exc:
                    return self._send(400, {"ok": False, "reply": str(exc)})
                except OSError:
                    return self._send(400, {"ok": False, "reply": "连不上 Slack，检查网络后再试。"})
            if path == "/api/session/associate":
                from . import local_sessions
                from .models import MatrixError
                try:
                    result = local_sessions.associate(root, str(body.get("key") or ""), int(body.get("n") or 0))
                    return self._send(200, {"ok": True, **result})
                except MatrixError as exc:
                    return self._send(400, {"ok": False, "reply": str(exc)})
            if path == "/api/session/transcript":
                from .config import load
                from .models import MatrixError
                from . import sessions as ai_sessions
                try:
                    return self._send(200, {"ok": True, **ai_sessions.transcript(root, load(root / "config.yaml"), str(body.get("job") or ""))})
                except MatrixError as exc:
                    return self._send(400, {"ok": False, "reply": str(exc)})
            if path == "/api/cindy/setup":
                from . import local_sessions
                return self._send(200, {"ok": True, "text": local_sessions.cindy_setup_text(root)})
            if path == "/api/engine/test":
                from .config import load
                from .engines import test_engine
                name = body.get("engine")
                if name not in {"codex", "claude", "gemini", "api"}:
                    return self._send(400, {"ok": False, "reply": "不认识这个 AI。"})
                return self._send(200, {"ok": True, **test_engine(root, load(root / "config.yaml"), name)})
            if path == "/api/session/unbind":
                from . import local_sessions
                return self._send(200, local_sessions.unbind(root, str(body.get("key") or "")))
            if path in ("/api/session/start", "/api/session/cindy", "/api/session/pickup"):
                from .config import load
                from .models import MatrixError
                from .snapshot import _snapshot
                from .drive import _history_text
                from . import local_sessions
                n = int(body.get("n") or 0)
                row = next((r for r in _snapshot(root).get("rows", []) if str(r.get("编号")) == str(n)), None)
                if not row:
                    return self._send(400, {"ok": False, "reply": "找不到这件事。"})
                item = {"record_id": row["record_id"], "n": n, "title": row.get("标题"), "status": row.get("状态"), "who": row.get("执行方"),
                        "due": str(row.get("截止日") or "")[:10], "source": row.get("来源"), "source_url": _url(row.get("来源链接")),
                        "next": row.get("下一步动作"), "criterion": row.get("完成标准"), "quote": _clean(row.get("原话引用")),
                        "url": _url(row.get("交付物链接")), "extra": _clean(row.get("补充材料")),
                        "rejected": _clean(row.get("退回意见"))}
                try:
                    start = {"/api/session/cindy": local_sessions.start_in_cindy, "/api/session/start": local_sessions.start_for_item}.get(path, local_sessions.pick_up)
                    extra = {"where": str(body["app"])} if path == "/api/session/pickup" and body.get("app") else {}   # this item, this once
                    return self._send(200, {"ok": True, **start(root, load(root / "config.yaml"), item,
                                                               history=_history_text(root, row["record_id"], 40), **extra)})
                except MatrixError as exc:
                    return self._send(400, {"ok": False, "reply": str(exc)})
            if path == "/api/session/open":
                from .config import load
                from .models import MatrixError
                from . import local_sessions
                try:
                    return self._send(200, {"ok": True, **local_sessions.open_session(root, load(root / "config.yaml"), str(body.get("job") or ""))})
                except MatrixError as exc:
                    return self._send(400, {"ok": False, "reply": str(exc)})
            if path == "/api/read":
                from . import inbox
                inbox.mark_read(root, None if body.get("all") else [int(x) for x in body.get("ids") or []])
                return self._send(200, {"ok": True})
        except ValueError as exc:
            return self._send(400, {"ok": False, "reply": str(exc)})
        except Exception as exc:
            return self._send(500, {"ok": False, "reply": f"出错了：{type(exc).__name__}"})
        return self._send(404, {"error": "not found"})


def make_server(root, port):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.root, server.token = Path(root), token(root)
    server.daemon_threads = True
    from .commands import Commands
    server.commands = Commands(root)
    info = Path(root) / "state/app.json"
    info.write_text(json.dumps({"port": port, "pid": os.getpid(), "url": f"http://127.0.0.1:{port}/"}))
    return server


def local_proxy_down():
    """The computer is told to send web traffic through a proxy on this machine (Clash and the like) that is not
    running: lark-cli, npm and the rest then cannot reach the internet. Returns that address, or None."""
    import socket
    from urllib.parse import urlparse
    for key in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy"):
        value = os.environ.get(key)
        if not value:
            continue
        url = urlparse(value if "://" in value else "http://" + value)
        if url.hostname in ("127.0.0.1", "localhost") and url.port:
            try:
                socket.create_connection((url.hostname, url.port), timeout=0.5).close()
                return None
            except OSError:
                return f"{url.hostname}:{url.port}"
        return None
    return None


def setup_view(root):
    """For the settings page: who will do the setting up, what waits for it, what it is doing and what it asked for."""
    from . import aidrivers, drive, setupasks, thread
    engine = thread.setting(root, "engine", "codex")
    name = aidrivers.name_in(engine)
    known = {d["name"]: d for d in aidrivers.known(root)}
    driver = {"name": name or ("Cindy" if engine == "cindy" else ""),
              "online": bool(known.get(name, {}).get("online")) if name else engine == "cindy"}
    asks = setupasks.pending(root)
    jobs = {j["ref"].get("what"): {"by": j.get("driver"), "step": j.get("step") or "", "title": j.get("title"),
                                   "said": (asks.get(j["ref"].get("what")) or {}).get("said") or ""}
            for j in drive.open_jobs(root) if j["kind"] == "setup"}
    gave_up = {w: thread.setting(root, "setup:gave_up:" + w, None) for w in setupasks.WHAT}
    return {"driver": driver, "asks": asks, "doing": jobs, "gave_up": {w: g for w, g in gave_up.items() if g}}


def start_background(root, config):
    """Called by the listener: serve the client from the always-on process. A second copy just skips."""
    try:
        from . import drive, inbox, thread
        thread.backfill(root)
        inbox.import_history(root)
        drive.install_shim(root)  # `~/.agent-matrix/bin/xinxiang`: the command any AI calls
        try:
            from . import update
            update.settle(root)   # an update that was under way has landed
        except Exception as exc:
            activity.swallowed(root, "app.update_settle", exc)
        from . import meeting_review
        meeting_review.backfill_meetings(root)  # earlier meetings become history (quietly, once)
    except Exception as exc:
        activity.swallowed(root, "app.start_background", exc)
    try:
        server = make_server(root, config.app_port)
    except OSError:
        # Busy: fine if it is another 心响; if some other program holds the port, say so loudly.
        if not is_xinxiang(config.app_port):
            from . import activity
            activity.log(root, "error", f"心响打不开：端口 {config.app_port} 被别的程序占用",
                         detail="在 config.yaml 里把 app_port 改成别的数字，再重启监听和心响.app（macapp/build.sh）。", status="failed")
        return None
    threading.Thread(target=server.serve_forever, daemon=True, name="xinxiang").start()
    return server


def is_xinxiang(port):
    """Is the program answering on this port 心响 (and not some other local server)?"""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as r:
            return "<title>心响</title>" in r.read(4000).decode("utf-8", "ignore")
    except Exception:
        return False


def main(argv=None):
    from .config import load
    p = argparse.ArgumentParser(description="心响本机客户端")
    p.add_argument("--config", type=Path, default=Path("config.yaml"))
    p.add_argument("--open", action="store_true")
    args = p.parse_args(argv)
    path = args.config.resolve()
    config = load(path)
    from . import inbox, thread
    thread.backfill(path.parent)
    inbox.import_history(path.parent)
    server = make_server(path.parent, config.app_port)
    url = f"http://127.0.0.1:{config.app_port}/"
    print(json.dumps({"ok": True, "url": url}))
    if args.open:
        webbrowser.open(url)
    server.serve_forever()


if __name__ == "__main__":
    raise SystemExit(main())
