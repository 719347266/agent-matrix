"""心响驱动协议：任何 AI 都能接入，领活 → 干活 → 交活。没有限制，谁能运行命令谁就能驱动。

    xinxiang next    --driver "Claude Code"              领下一件活（JSON）；没有活时返回 {"idle": true}
    xinxiang step    --job ID --text "2/4 查文档"         报进度，心响里实时显示
    xinxiang say     --job ID --text "……"                 在这件事的对话里说一句（给本人看）
    xinxiang submit  --job ID                             交活：程序自检；没过返回 problem，改完再交
    xinxiang ask     --job ID --question "…" --tried "…"  卡住了：问本人一个具体问题（只用于事项）
    xinxiang release --job ID                             不做了，放回队列
    xinxiang guide                                        给 AI 看的接入说明

Four kinds of work, in this order: judging new items (when the owner turned it on), extracting todos
from a meeting, extracting todos from chats, doing one AI item. Each job has its own folder, plain
instructions and an output file; code does everything else (checks, Feishu document, table, notice).

The conversation of every item lives in 心响 (thread.py), not in the AI that did the work, so any
driver picks up where the previous one stopped: `next` hands over the history and the owner's latest
words. The built-in driver (本机 GPT, worker.py) uses exactly these functions.
"""
import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from uuid import uuid4

from . import activity
from .workenv import shim_path  # noqa: F401
from . import promptfile
from .jobstore import now, _db, _save, get, open_jobs, drivers, stop_record  # noqa: F401
from . import db as database
from .models import MatrixBusy, MatrixError
from .timeutil import TZ

ITEM_LEASE_MINUTES = 120
OTHER_LEASE_MINUTES = 30
MAX_FIX_ROUNDS = 2




# ---- the job table (state/xinxiang.db) ------------------------------------------------------------------













STOPPED = {"ok": False, "status": "cancelled", "note": "本人已经把这件事标为不做或已完成：停下，不用再做、也不用交了。"}


def _closed(job):
    if job["status"] == "cancelled":
        if str(job.get("step") or "").startswith("本人把这件事交给了"):
            return {**STOPPED, "note": f"{job['step']}：停下，不用再做、也不用交了。"}
        return dict(STOPPED)
    if job["status"] != "open":
        return {"ok": True, "status": job["status"], "note": "这件活已经结束了。"}
    return None


def _busy(root, kind):
    return any(j["kind"] == kind for j in open_jobs(root))


def _lease(minutes):
    return (now() + timedelta(minutes=minutes)).isoformat(timespec="seconds")


# ---- how an outside AI calls back --------------------------------------------------------------------



def install_shim(root):
    """A command with a plain path any AI can call (the project path may contain Chinese)."""
    path = shim_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            body = f'@echo off\r\nset PYTHONUTF8=1\r\ncd /d "{Path(root).resolve()}"\r\n"{sys.executable}" -m agent_matrix.drive %*\r\n'
        else:
            body = f'#!/bin/sh\ncd "{Path(root).resolve()}" || exit 1\nexec "{sys.executable}" -m agent_matrix.drive "$@"\n'
        if not path.is_file() or path.read_text() != body:
            path.write_text(body)
            path.chmod(0o755)
    except OSError:
        return None
    return path


def _cmd():
    return str(shim_path())


def protocol_tail(job):
    c, j = _cmd(), job["id"]
    lines = ["", "## 交活（心响驱动协议）",
             f"- 进度：`{c} step --job {j} --text \"2/4 查文档\"`（15 字内，心响里实时显示）",
             f"- 做完：`{c} submit --job {j}`。返回 `\"status\": \"fix\"` 就按 problem 改好再交；返回 done 就结束。"]
    if job["kind"] == "setup":
        lines += [f"- 要本人亲手做的事（登录、点同意、扫码、输电脑密码）：`{c} say --job {j} --text \"一句话说清要他做什么\"`，"
                  "心响里会显示给他看；在你自己的对话里也告诉他，等他做完再继续。"]
    if job["kind"] == "item":
        lines += [f"- 想对本人说的话：`{c} say --job {j} --text \"……\"`（会记进这件事的对话）",
                  f"- 卡住：`{c} ask --job {j} --question \"问本人的一个具体问题\" --tried \"已经试过什么\"`",
                  f"- 这件事更适合另一个 AI 做：`{c} handoff --job {j} --to \"它的名字\" --why \"为什么\"`。只交这一件，别的事还是你做；"
                  "返回里有 message 就把它原样发给那个 AI（你发不了就告诉本人去发），它会接着你的进度做。"]
    lines += [f"- 做不了：`{c} release --job {j}`，放回队列。",
              "- 任何调用返回 cancelled：本人已叫停，立即结束本轮和已启动的命令，不再写入或提交这件事。",
              "然后再运行一次 next 领下一件，直到返回 idle。"]
    return "\n".join(lines)


# ---- preparing each kind of job ----------------------------------------------------------------------

def _prepare_triage(root, config, driver):
    from . import thread as conversation
    from .itemwork import _pending_new
    from .workenv import project, scratch
    if not conversation.setting(root, "auto_assign", False) or _busy(root, "triage"):
        return None
    from .workflow import Gateway
    with project(root, config) as (client, state):
        rows = Gateway(client, state).rows()
    items = _pending_new(rows, conversation.judgments(root))
    if not items:
        return None
    # What happened since: the owner may already have done it in a chat, or it was called off.
    from .background import after_context
    by_id = {r["record_id"]: r for r in rows}
    for i in items:
        i["after"] = after_context(client, by_id[i["record_id"]], now())
    stamp = now().strftime("%Y%m%d%H%M%S")
    folder = scratch(root, "triage", stamp)
    (folder / "items.json").write_text(json.dumps([{k: v for k, v in i.items() if k != "record_id"} for i in items],
                                                 ensure_ascii=False, indent=1), encoding="utf-8")
    return {"id": f"triage-{stamp}", "kind": "triage", "title": f"判断新进来的 {len(items)} 件事", "folder": str(folder),
            "ref": {"items": items}, "output": ["triage.json"], "lease_until": _lease(OTHER_LEASE_MINUTES),
            "instructions": promptfile.read(root, "triage.md")}


LEARN_MIN_NEW, LEARN_EVERY_HOURS = 3, 24


def _prepare_setup(root, config, driver):
    """The owner clicked 接入飞书 / Slack / 微信 (or asked for the rest to be set up): the AI that drives 心响 does it."""
    from . import setupasks
    from .setupshim import _cmd as setup_cmd
    from .workenv import scratch
    if _busy(root, "setup"):
        return None
    asks = setupasks.pending(root)
    if not asks:
        return None
    what = next(iter(asks))
    name = setupasks.WHAT[what]
    only = "" if what == "fix" else f" --only {what}"
    stamp = now().strftime("%Y%m%d%H%M%S")
    title = "把心响剩下的配好" if what == "fix" else f"接入 {name}" if name.isascii() else f"接入{name}"
    doctor = f"{setup_cmd(root)} doctor{only}"
    return {"id": f"setup-{what}-{stamp}", "kind": "setup", "title": title, "folder": str(scratch(root, "setup", stamp)),
            "ref": {"what": what}, "output": [], "lease_until": _lease(OTHER_LEASE_MINUTES),
            "instructions": f"""# {title}

本人在心响里点了「{title}」，要你来把它配好。你就是驱动心响的 AI，命令都由你来运行。

1. 运行 `{doctor}`。它会列出还缺什么、每一步怎么做；照着逐项做，每做完一步再运行一次，直到这一项就绪。
2. 只有人能做的事（在浏览器里登录、点同意、扫码、输电脑密码）才找本人：用下面的 say 命令说清要他做什么，然后等他做完再继续。
3. 每到一个阶段报一下进度（step，15 字内），本人在心响设置里能看到。
4. 做完运行 submit。心响会自己再查一遍：真接好了才算完成；没好会告诉你还差什么，照着继续。

请遵守：
- 不要在对话里问本人要密码、验证码、Token 或密钥。要填 Token 的（比如 Slack），请本人自己粘贴到心响的设置里。
- 改系统设置、重新签名应用这类会影响别的软件的事，先说清楚影响，本人同意了再做。
- 只做配置这一件事，不要改别的东西。"""}


def _prepare_learn(root, config, driver):
    """Now and then: turn the owner's recent feedback into standing rules (偏好快照)."""
    from . import memory, thread as conversation
    from .workenv import scratch
    if _busy(root, "learn"):
        return None
    since = conversation.setting(root, "learned_at", "")
    hist = conversation.history(root)
    feedback = []
    snap = {}
    try:
        from .snapshot import _snapshot
        snap = {r.get("record_id"): r for r in _snapshot(root).get("rows") or []}
    except Exception as exc:
        activity.swallowed(root, "drive._prepare_learn", exc)
    for rid, h in hist.items():
        for m in h.get("messages", []):
            if m["from"] == "me" and m["kind"] == "feedback" and len(m["text"]) >= 6 and m["at"] > (since[5:16].replace("T", " ") if since else ""):
                feedback.append({"item": str((snap.get(rid) or {}).get("标题") or ""), "at": m["at"], "said": m["text"][:400]})
    if not feedback:
        return None
    fresh_enough = len(feedback) >= LEARN_MIN_NEW
    stale = not since or now() - datetime.fromisoformat(since) > timedelta(hours=LEARN_EVERY_HOURS)
    if not (fresh_enough or stale):
        return None
    stamp = now().strftime("%Y%m%d%H%M%S")
    folder = scratch(root, "learn", stamp)
    (folder / "feedback.json").write_text(json.dumps(feedback[-60:], ensure_ascii=False, indent=1), encoding="utf-8")
    (folder / "rules.json").write_text(json.dumps(memory.rules(root), ensure_ascii=False, indent=1), encoding="utf-8")
    return {"id": f"learn-{stamp}", "kind": "learn", "title": f"从你最近 {len(feedback)} 条反馈里学规矩", "folder": str(folder),
            "ref": {"count": len(feedback)}, "output": ["learned.json"], "lease_until": _lease(OTHER_LEASE_MINUTES),
            "instructions": promptfile.read(root, "learn.md")}


def _submit_learn(root, job):
    from . import activity, memory, thread as conversation
    try:
        learned = json.loads((Path(job["folder"]) / "learned.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        job["rounds"] += 1
        _save(root, job)
        return {"ok": False, "status": "fix", "problem": "没有找到有效的 learned.json（格式见说明里的例子）。"}
    added = []
    for r in (learned.get("add") or [])[:3]:
        rule = memory.add_rule(root, r.get("rule"), r.get("why", ""), source="AI 学的")
        if rule:
            added.append(rule)
    ai_rules = {x["id"] for x in memory.rules(root) if x["source"] == "AI 学的"}
    for rid in learned.get("retire") or []:
        if rid in ai_rules:
            memory.remove_rule(root, rid)
    conversation.set_setting(root, "learned_at", now().isoformat(timespec="seconds"))
    if added:
        activity.log(root, "detect", "从你的反馈里学到新规矩：" + "；".join(added)[:200])
    _close(root, job, "done")
    return {"ok": True, "status": "done", "added": added}


def _try_existing_review(root, source_dir, validate, publish, label):
    """Extracted earlier but the publish failed: publish without a new model run."""
    from . import activity
    from .workenv import _cli
    if not (Path(source_dir) / "review.json").is_file():
        return False
    code, out = _cli(root, *validate)
    if code != 0 or not out.get("ok"):
        return False
    code, out = _cli(root, *publish)
    if not out.get("ok"):
        activity.log(root, "error", f"{label}待办入表没成功", detail=str(out.get("error"))[:200], status="failed")
    return True


def _write_related(root, folder):
    """related_items.json: what a new item may be the next step of: everything still open, plus what was
    finished in the last two weeks."""
    from .snapshot import _snapshot
    since = (now() - timedelta(days=14)).isoformat()[:10]
    out = []
    for r in _snapshot(root).get("rows") or []:
        status = r.get("状态") or ""
        if r.get("排除本人提醒") or status == "取消" or not r.get("编号"):
            continue
        if status == "完成" and str(r.get("完成时间") or r.get("更新时间") or "")[:10] < since:
            continue
        out.append({"编号": int(str(r["编号"])), "标题": r.get("标题"), "状态": status})
    (Path(folder) / "related_items.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


def _prepare_meeting(root, config, driver):
    from .meeting_review import Review
    from .workenv import _check, _cli, scratch
    code, checked = _check(root, "agent_matrix.meeting_watch")
    taken = {j["ref"].get("meeting_id") for j in open_jobs(root) if j["kind"] == "meeting"}
    for m in checked.get("ready") or []:
        mid = m["meeting_id"]
        if mid in taken:
            continue
        if m.get("status") == "needs_attention":
            _cli(root, "agent_matrix.meeting_watch", "notify-missing", "--meeting-id", mid)
            continue
        code, prep = _cli(root, "agent_matrix.meeting_watch", "prepare", "--meeting-id", mid)
        if code != 0 or not prep.get("transcript_path"):
            _cli(root, "agent_matrix.meeting_watch", "defer", "--meeting-id", mid)
            continue
        source = Path(prep["transcript_path"]).parent
        review = str(source / "review.json")
        validate = ["agent_matrix.meeting_review", "validate", "--review", review]
        publish = ["agent_matrix.meeting_review", "publish", "--review", review]
        if _try_existing_review(root, source, validate, publish, "会议"):
            continue
        (source / "review.schema.json").write_text(json.dumps(Review.model_json_schema(), ensure_ascii=False), encoding="utf-8")
        open_name = Path(prep.get("open_items_path") or "open_items.json").name
        folder = scratch(root, "extract", mid)
        for name in ("transcript.md", open_name, "review.schema.json"):
            if (source / name).is_file():
                shutil.copyfile(source / name, folder / name)
        (folder / "review.json").unlink(missing_ok=True)
        _write_related(root, folder)
        topic = str((prep.get("metadata") or {}).get("topic") or m.get("topic") or "会议")
        header = (f"本次会议：{topic}。meeting_id = {mid}，transcript_sha256 = {prep['transcript_sha256']}。\n"
                  f"逐字稿：transcript.md；没完成的事项：{open_name}；review.json 的结构见 review.schema.json。\n"
                  "只需要在当前文件夹写好 review.json；校验和发布由程序做。")
        return {"id": f"meeting-{mid}", "kind": "meeting", "title": f"提取会议待办：{topic}"[:40], "folder": str(folder),
                "ref": {"meeting_id": mid, "source": str(source), "validate": validate, "publish": publish},
                "output": ["review.json"], "lease_until": _lease(OTHER_LEASE_MINUTES),
                "instructions": header + "\n\n" + promptfile.read(root, "meeting_extract.md")}
    return None


def _prepare_chat(root, config, driver):
    from .chat_review import ChatReview
    from .workenv import _check, _cli, scratch
    if _busy(root, "chat"):
        return None
    code, checked = _check(root, "agent_matrix.chat_review")
    if code != 0 or not checked.get("run_model"):
        return None
    code, prep = _cli(root, "agent_matrix.chat_review", "prepare")
    if code != 0 or not prep.get("candidates_path"):
        return None
    source = Path(prep["candidates_path"]).parent
    review = prep.get("review_path") or str(source / "review.json")
    validate = ["agent_matrix.chat_review", "validate", "--review", review]
    publish = ["agent_matrix.chat_review", "publish", "--review", review]
    (source / "review.schema.json").write_text(json.dumps(ChatReview.model_json_schema(), ensure_ascii=False), encoding="utf-8")
    batch = str(prep.get("batch_id") or source.name)
    folder = scratch(root, "extract", source.name)
    for name in (Path(prep["candidates_path"]).name, "review.schema.json"):
        if (source / name).is_file():
            shutil.copyfile(source / name, folder / name)
    (folder / "review.json").unlink(missing_ok=True)
    _write_related(root, folder)
    try:  # what was said after each candidate, so things already settled are not turned into todos
        from .background import add_followups
        from .workenv import project
        path = folder / Path(prep["candidates_path"]).name
        with project(root, config) as (client, state):
            pass
        data = add_followups(client, json.loads(path.read_text(encoding="utf-8")), now())
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        # the batch keeps them too: the program checks the owner's "好的" against these lines, not the model's word
        (source / path.name).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as exc:
        activity.swallowed(root, "drive._prepare_chat", exc)
    header = (f"本批聊天：batch_id = {batch}。候选消息：{Path(prep['candidates_path']).name}；结构见 review.schema.json。\n"
              "只需要在当前文件夹写好 review.json；校验和发布由程序做。")
    return {"id": f"chat-{re.sub(r'[^A-Za-z0-9_-]', '', batch)}", "kind": "chat", "title": "提取聊天待办", "folder": str(folder),
            "ref": {"source": str(source), "validate": validate, "publish": publish}, "output": ["review.json"],
            "lease_until": _lease(OTHER_LEASE_MINUTES),
            "instructions": header + "\n\n" + promptfile.read(root, "chat_extract.md")}


def _history_text(root, record_id, limit=30):
    from . import thread as conversation
    all_messages = conversation.history(root, [record_id]).get(record_id, {}).get("messages", [])
    # Both personal notes and historical closure events may contain the owner's private text.
    msgs = [m for m in all_messages if m["kind"] not in {"note", "closed"}][-limit:]
    who = {"me": "本人", "ai": "AI", "system": "系统"}
    return "\n".join(f"- {m['at']} {m.get('driver') if m['from'] == 'ai' and m.get('driver') else who.get(m['from'], '')}：{m['text'][:400]}"
                     for m in msgs)


def _prepare_item(root, config, driver, skip=(), allow=None, name_for=None):
    from .itemwork import Item
    from .itemwork import pick_next
    pick = pick_next(root, config, skip=skip, allow=allow)
    if not pick:
        return None
    if name_for:
        driver = name_for(pick["record_id"])
    item = Item(root, config, pick)
    got = item.claim(lease_minutes=ITEM_LEASE_MINUTES)
    if not got:
        return {"skip": pick["record_id"]}
    client, rows, row, notes = got
    try:
        return {**_item_job(root, config, driver, item, client, rows, row, notes), "driver_name": driver}
    except BaseException:
        item.release()   # claimed but not handed out: back to the queue now, not when the lease runs out
        raise


def _item_job(root, config, driver, item, client, rows, row, notes):
    from .itemwork import _changes, first_prompt, resume_prompt
    from .workenv import project
    from .prework import add_note
    add_note(root, item.rid, f"{driver} 开始处理")
    item.prepare_folder(client, rows, row, notes)
    with project(root, config, client=False, wait=60) as (_, state):
        first = state.get("work:first", [])
        if item.rid in first:
            state.set("work:first", [x for x in first if x != item.rid])
    changes = _changes(item.workdir, row)
    history = _history_text(root, item.rid)
    base = first_prompt(root, row)
    extra = []
    if history:
        extra += ["", "## 这件事之前的对话（心响保存；可能是别的 AI 做的，接着往下，不要从头来）", history]
    if changes:
        extra += ["", "## 本人新补充的要求（这一轮必须照做）", *[f"- {c}" for c in changes]]
    return {"id": f"item-{item.rid}-{item.version[:8]}", "kind": "item", "title": item.title, "n": int(str(item.n)) if item.n else None,
            "folder": str(item.workdir), "ref": {"record_id": item.rid, "version": item.version},
            "output": ["交付.md", "结果.json"], "lease_until": _lease(ITEM_LEASE_MINUTES),
            "instructions": base + "\n".join(extra),
            # the built-in GPT resumes its own conversation instead of re-reading everything
            "builtin_prompt": resume_prompt(changes, (item.workdir / ".timed_out").is_file()) if any((item.workdir / f).is_file() for f in (".thread", ".claude-session")) else base}


SORTING = ("triage", "meeting", "chat", "learn")   # 整理: judging new items, meetings, chats, learning rules


def item_runner(root):
    """Who does the items (设置 → AI 执行 → 做事用哪个): "cindy" (Cindy's own schedule takes them) or "local"."""
    from .thread import setting
    chosen = setting(Path(root), "engine", "codex")
    return "cindy" if chosen == "cindy" else "driver" if str(chosen).startswith("driver:") else "local"


def sort_runner(root):
    """Who sorts (judging new items, meetings, chats, learning rules): "cindy" or "local"."""
    from .thread import setting
    chosen = setting(Path(root), "engine_sort", "")
    return "cindy" if chosen == "cindy" else "driver" if str(chosen).startswith("driver:") else "local"


def cindy_kinds(root):
    """What Cindy's schedule takes: the items and/or the sorting, as the owner chose (设置 → 谁来做事)."""
    return [*(SORTING if sort_runner(root) == "cindy" else ()), *(("item",) if item_runner(root) == "cindy" else ())]


def local_kinds(root):
    """What the built-in driver takes: the sorting unless it was handed to someone else, and the items that are its own
    (all of them when a local AI is chosen in settings, else only those handed to a local AI one by one)."""
    return [*(SORTING if sort_runner(root) == "local" else ()), "item"]


def kinds_for(root, driver):
    """What this AI takes: the sorting if it was handed to it, and items (only its own: see item_filter)."""
    from .aidrivers import is_mine
    from .thread import setting
    work, sort = setting(Path(root), "engine", "codex"), setting(Path(root), "engine_sort", "")
    return [*(("setup",) if is_mine(work, driver) else ()), *(SORTING if is_mine(sort, driver) else ()), "item"]


def item_filter(root, driver, *, builtin=False):
    """Which items this AI may take: those handed to it, and, unless handed to someone else, those of the AI chosen
    in settings when that is this one."""
    from . import aidrivers
    from .thread import setting
    table, usual = aidrivers.assignments(root), setting(Path(root), "engine", "codex")
    return lambda rid: aidrivers.is_mine(table.get(rid) or usual, driver, builtin=builtin)


def has_sorting_for(root, config):
    """Is there a meeting, a round of chats or new items to judge waiting? Cheap: a schedule's pre-run check asks."""
    from . import thread as conversation
    from .workenv import _check
    try:
        code, checked = _check(root, "agent_matrix.meeting_watch")
        taken = {j["ref"].get("meeting_id") for j in open_jobs(root) if j["kind"] == "meeting"}
        if any(m.get("status") == "ready" and m["meeting_id"] not in taken for m in checked.get("ready") or []):
            return "meeting"
        if not _busy(root, "chat"):
            code, checked = _check(root, "agent_matrix.chat_review")
            if code == 0 and checked.get("run_model"):
                return "chat"
        if conversation.setting(Path(root), "auto_assign", False) and not _busy(root, "triage"):
            from .itemwork import _pending_new
            from .snapshot import _snapshot
            if _pending_new(_snapshot(root).get("rows") or [], conversation.judgments(root)):
                return "triage"
    except Exception as exc:
        activity.swallowed(root, "drive.has_sorting_for", exc)
    return None


def next_job(root, config, driver, *, skip=(), kinds=None, builtin=False, only=None):
    """The next piece of work for this driver, already prepared; None when there is nothing to do.
    kinds limits what it takes: e.g. Cindy takes only items, the built-in driver then only sorting work.
    builtin: the built-in driver asks (it takes the items of the local AIs, each run with its own AI).
    only: one item by its record id, that the owner or another AI handed to whoever got the message."""
    root = Path(root)
    from . import aidrivers
    allow, name_for = None, None
    if only:
        owner = aidrivers.assigned(root, only)
        if owner and owner != aidrivers.HANDOFF and not aidrivers.is_mine(owner, driver):
            raise MatrixError(f"这件事交给了 {aidrivers.label(owner)}，不是你。")
        if any(j["kind"] == "item" and j["ref"].get("record_id") == only for j in open_jobs(root)):
            raise MatrixError("这件事正在被别的 AI 做。")
        kinds, allow = ["item"], (lambda rid: rid == only)
    elif builtin:
        allow = item_filter(root, driver, builtin=True)
        name_for = lambda rid: aidrivers.LOCAL.get(aidrivers.owner_of(root, rid), driver)
    elif "cindy" in str(driver).lower() or aidrivers.is_known(root, driver):
        # Cindy's schedule and every AI that said hello take what was handed to them, whatever their prompt asks for
        kinds, allow = kinds_for(root, driver), item_filter(root, driver)
    wanted = set(kinds or (*SORTING, "item"))
    if "setup" in wanted:   # the owner is waiting on this one: it goes before anything else
        job = _prepare_setup(root, config, driver)
        if job:
            return _open(root, job, driver)
    for kind, prepare in zip(SORTING, (_prepare_triage, _prepare_meeting, _prepare_chat, _prepare_learn)):
        if kind not in wanted:
            continue
        job = prepare(root, config, driver)
        if job:
            return _open(root, job, driver)
    if "item" not in wanted:
        return None
    tried = set(skip)
    for _ in range(5):
        job = _prepare_item(root, config, driver, skip=tried, allow=allow, name_for=name_for)
        if not job:
            return None
        if "skip" in job:
            tried.add(job["skip"])
            continue
        if only and aidrivers.assigned(root, only) == aidrivers.HANDOFF:
            aidrivers.assign(root, only, aidrivers.setting_of(aidrivers.clean(driver)))   # whoever got the message, has it now
        # Preparing background may take time; the owner may have cancelled meanwhile.
        # Publish the lease under the same lock used by owner commands.
        from .prework import current
        from .workenv import project
        with project(root, config) as (client, state):
            current(config, client, state, job["ref"]["record_id"], job["ref"]["version"])
            return _open(root, job, job.pop("driver_name", None) or driver)
    return None


def _open(root, job, driver):
    # A restored/retried item gets a fresh lease identity. Old callbacks cannot target the new run.
    db = _db(root)
    try:
        if db.execute("SELECT 1 FROM jobs WHERE id=?", (job["id"],)).fetchone():
            job["id"] += "-" + uuid4().hex[:12]
    finally:
        db.close()
    try:  # 记忆层: every AI reads the owner's standing rules before any work
        from .memory import rules_text
        head = rules_text(root)
        job["instructions"] = head + job["instructions"]
        if job.get("builtin_prompt"):
            job["builtin_prompt"] = head + job["builtin_prompt"]
    except Exception as exc:
        activity.swallowed(root, "drive._open", exc)
    if job["kind"] == "item" and "cindy" in str(driver).lower():
        job["instructions"] = cindy_header(root, job) + job["instructions"]
    job.update(driver=driver, status="open", rounds=0, step="领到了")
    _save(root, job)
    return job


def cindy_header(root, job):
    """Cindy works inside the owner's own project: say so, so it follows that project's rules and memory and does not
    mistake 心响 (which only hands out work and takes deliverables) for the product it is working on."""
    from .thread import setting
    project = setting(Path(root), "cindy_project", "") or ""
    name = Path(project).name if project else "当前"
    return (f"## 先说清楚：你在哪、在做什么\n"
            f"- 你在本人的「{name}」项目里（Cindy 的当前工作目录）。这件事是这个项目的工作：按这个项目自己的规范、模板和记忆来做。\n"
            f"- 心响只是给你派活、收交付的系统，不是你要做需求的产品；不要把心响的东西写进交付。\n"
            f"- 回复第一行写「心响 #{job.get('n') or ''} {job.get('title') or ''}」，方便本人在心响里找到这段对话。\n"
            f"- 交付文件写到下面说的工作文件夹（folder）里，做完按末尾的命令交给心响。\n\n")


def has_items_for(root, config, driver="Cindy"):
    """Is there an item for this AI waiting that nobody is working on? Cheap enough for a schedule's pre-run check."""
    from .workenv import _check
    code, checked = _check(root, "agent_matrix.prework")
    if code != 0:
        return False
    taken = {j["ref"].get("record_id") for j in open_jobs(root) if j["kind"] == "item"}
    mine = item_filter(root, driver)
    return any(r.get("status") in {"queued", "running", "prepared"} and r["record_id"] not in taken and mine(r["record_id"])
               for r in checked.get("ready") or [])


def public(job):
    """What an outside AI gets back from `next`."""
    out = {k: job.get(k) for k in ("id", "kind", "title", "n", "folder", "output")}
    out["job"] = out.pop("id")
    out["instructions"] = job["instructions"] + "\n" + protocol_tail(job)
    return out


# ---- progress, words, finishing ------------------------------------------------------------------------

def step(root, config, job_id, text, driver=None):
    job = get(root, job_id)
    if closed := _closed(job):
        return closed
    text = re.sub(r"\s+", " ", str(text or "")).strip()[:15]
    job.update(step=text, lease_until=_lease(ITEM_LEASE_MINUTES if job["kind"] == "item" else OTHER_LEASE_MINUTES))
    progress_at = job.get("progress_at")
    write = job["kind"] == "item" and (not progress_at or now() - datetime.fromisoformat(progress_at) > timedelta(seconds=60))
    if write:
        job["progress_at"] = now().isoformat(timespec="seconds")
    if not _save(root, job):
        return _closed(get(root, job_id))
    if write:
        try:
            from .prework import report_progress
            from .workenv import project
            with project(root, config, wait=20) as (client, state):
                if closed := _closed(get(root, job_id)):
                    return closed
                report_progress(config, client, state, job["ref"]["record_id"], text)
        except (MatrixError, MatrixBusy, OSError):
            pass
    return {"ok": True, "step": text}


def say(root, job_id, text, driver=None):
    job = get(root, job_id)
    if closed := _closed(job):
        return closed
    if job["kind"] == "setup":   # shown where the owner clicked 接入, until the next thing is said
        from . import setupasks
        setupasks.note(root, job["ref"]["what"], said=str(text or "").strip()[:300], said_at=now().isoformat(timespec="seconds"))
        return {"ok": True}
    if job["kind"] != "item":
        return {"ok": False, "error": "只有事项有对话。"}
    from . import thread as conversation
    conversation.add(root, job["ref"]["record_id"], "ai", "say", str(text)[:3000], driver=driver or job.get("driver"))
    return {"ok": True}


def fail(root, job, problem):
    """A job the AI could not finish is said out loud: the panel shows it and 心响 gets a message."""
    from . import activity, inbox
    job = get(root, job["id"]) if isinstance(job, dict) and "id" in job else job
    job.update(status="failed", lease_until=None)
    _save(root, job)
    activity.log(root, "error", f"{job.get('title') or job['kind']}：没做成", detail=str(problem)[:300], status="failed")
    try:
        inbox.add(root, f"job-failed:{job['id']}:{job.get('rounds', 0)}",
                  f"⚠ {job.get('title') or 'AI 的活'}没做成\n原因：{str(problem)[:200]}\n系统会在 30 分钟后自动再试一次；"
                  f"急的话在「系统」页点「立即检查」。", kind="system")
    except Exception as exc:
        activity.swallowed(root, "drive.fail", exc)
    return {"ok": False, "status": "failed", "problem": str(problem)[:300]}


def _close(root, job, status):
    job.update(status=status, lease_until=None)
    _save(root, job)


def _item(root, config, job):
    from .itemwork import Item
    item = Item(root, config, {"record_id": job["ref"]["record_id"], "input_version": job["ref"]["version"], "title": job.get("title")})
    item.n, item.finished, item.job_id = job.get("n"), False, job["id"]
    return item


def submit(root, config, job_id, driver=None):
    """Check the output; publish it, or say exactly what to fix (the job stays open for the fix)."""
    job = get(root, job_id)
    if job["status"] == "cancelled":
        return _closed(job)
    if job["status"] != "open":
        return {"ok": True, "status": job["status"], "note": "这件活已经结束了。"}
    os.environ.setdefault("XINXIANG_DRIVER", job.get("driver") or driver or "AI")
    kind = job["kind"]
    if kind == "item":
        return _submit_item(root, config, job)
    if kind == "triage":
        return _submit_triage(root, job)
    if kind == "learn":
        return _submit_learn(root, job)
    if kind == "setup":
        return _submit_setup(root, job)
    from . import activity
    from .workenv import _cli
    folder, source = Path(job["folder"]), Path(job["ref"]["source"])
    if (folder / "review.json").is_file():
        shutil.copyfile(folder / "review.json", source / "review.json")
    code, out = _cli(root, *job["ref"]["validate"])
    if code != 0 or not out.get("ok"):
        job["rounds"] += 1
        problem = str(out.get("error") or "review.json 不符合格式")[:300]
        if job["rounds"] > MAX_FIX_ROUNDS:
            return fail(root, job, problem)
        _save(root, job)
        return {"ok": False, "status": "fix", "problem": problem + "\n只修正有依据的字段；没有承接字眼的条目直接删掉。"}
    code, out = _cli(root, *job["ref"]["publish"])
    if not out.get("ok"):
        return fail(root, job, "入表没成功：" + str(out.get("error"))[:200])
    _close(root, job, "done")
    if out.get("repaired"):
        activity.log(root, "detect", f"{job['title']}：自动修正 {len(out['repaired'])} 处", detail="；".join(out["repaired"])[:300])
    return {"ok": True, "status": "done", "created": out.get("created") or len(out.get("task_ids") or []),
            "repaired": out.get("repaired") or []}


def _submit_item(root, config, job):
    from .itemwork import _remember, check_result
    item = _item(root, config, job)
    result, problem = check_result(item.workdir, None)
    if closed := _closed(get(root, job["id"])):
        return closed
    if problem:
        job["rounds"] += 1
        if job["rounds"] > MAX_FIX_ROUNDS:
            item.block(f"自检没通过：{problem}"[:28], {"note": problem}, 0)
            _close(root, job, "blocked")
            return {"ok": False, "status": "blocked", "problem": problem}
        _save(root, job)
        return {"ok": False, "status": "fix", "problem": problem}
    _remember(item.workdir, _row_of(root, item.rid))
    if result["outcome"] == "blocked":
        item.block(str(result["question"]), result, 0)
        _close(root, job, "blocked")
        return {"ok": True, "status": "blocked"}
    published = item.publish(result, 0)
    _close(root, job, "done")
    return {"ok": True, "status": "done", "url": published.get("url")}


def _record_of(root, n):
    from .snapshot import _snapshot
    return next((r["record_id"] for r in _snapshot(root).get("rows") or [] if str(r.get("编号")) == str(n)), None)


def _row_of(root, record_id):
    from .snapshot import _snapshot
    return next((r for r in _snapshot(root).get("rows") or [] if r.get("record_id") == record_id), {})


def _submit_setup(root, job):
    """心响 checks for itself whether it is really set up; the ask goes away only then."""
    from . import inbox, setupasks
    from .workenv import _cli
    what = job["ref"]["what"]
    code, out = _cli(root, "agent_matrix.setupcheck", "status", *([] if what == "fix" else ["--only", what]), timeout=120)
    if not out:
        return {"ok": False, "status": "fix", "problem": "心响没查到配置情况，过一会儿再交一次。"}
    if not out.get("ready"):
        job["rounds"] += 1
        if job["rounds"] > 40:
            return fail(root, job, "改了很多轮还是没配好：" + "、".join(out.get("left") or []))
        _save(root, job)
        return {"ok": False, "status": "fix", "problem": "还没好：" + "、".join(out.get("left") or []) + "\n" + str(out.get("text") or "")[-1500:]}
    setupasks.done(root, what)
    _close(root, job, "done")
    activity.log(root, "setup", f"{job['title']}：好了", detail=job.get("driver") or "")
    try:
        inbox.add(root, f"setup-done:{job['id']}", f"✓ {job['title']}，{job.get('driver') or 'AI'} 配好了。", kind="system")
    except Exception as exc:
        activity.swallowed(root, "drive._submit_setup", exc)
    return {"ok": True, "status": "done"}


def _submit_triage(root, job):
    from . import activity, thread as conversation
    from .itemwork import TRIAGE_LABEL
    try:
        verdicts = json.loads((Path(job["folder"]) / "triage.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        job["rounds"] += 1
        _save(root, job)
        return {"ok": False, "status": "fix", "problem": "没有找到有效的 triage.json（格式见说明里的例子）。"}
    by_n = {i["n"]: i for i in job["ref"]["items"]}
    applied = []
    for v in verdicts if isinstance(verdicts, list) else []:
        item = by_n.get(v.get("n") if isinstance(v, dict) else None)
        decision = (v or {}).get("decision")
        if not item or decision not in TRIAGE_LABEL:
            continue
        if decision == "prep" and item["who"] == "AI":
            decision = "ai"
        reason = str(v.get("reason") or "")[:60]
        evidence = str(v.get("evidence") or "")[:300]
        if decision in {"done", "skip"} and not (evidence if decision == "done" else reason):
            continue  # closing something needs a reason (and, for done, the words that prove it)
        url = str(v.get("url") or "") if str(v.get("url") or "").startswith("http") else ""
        n = item["n"]
        commands = {"ai": [f"{n} AI做"], "prep": [f"{n} AI备"], "skip": [f"{n} 不做"],
                    "done": [f"{n} 完成", f"{n} + {url}"] if url else [f"{n} 无交付"]}.get(decision, [])
        ok = True
        for command in commands:
            run = subprocess.run([sys.executable, "-m", "agent_matrix.reply", "--text", command, "--source", "ai"], cwd=root, capture_output=True,
                                 text=True, encoding="utf-8", timeout=180)
            ok = ok and "✓" in (run.stdout or "")
        if not ok:
            continue  # not applied: judged again next time
        conversation.judge(root, item["record_id"], decision, reason)
        conversation.add(root, item["record_id"], "ai", "say", f"判断：{TRIAGE_LABEL[decision]}。{reason}" +
                         (f"\n依据：{evidence}" if evidence else ""), driver=job.get("driver"))
        applied.append({"n": item["n"], "decision": decision})
    if applied:
        activity.log(root, "detect", "AI 判断新进来的事：" + "、".join(f"#{a['n']} {TRIAGE_LABEL[a['decision']][:6]}" for a in applied))
    _close(root, job, "done")
    return {"ok": True, "status": "done", "applied": applied}


def ask(root, config, job_id, question, tried):
    job = get(root, job_id)
    if closed := _closed(job):
        return closed
    if job["kind"] != "item":
        return {"ok": False, "error": "只有事项可以问本人；提取类的活做不了就 release。"}
    if not str(question or "").strip() or not str(tried or "").strip():
        return {"ok": False, "error": "要写清楚问题（--question）和已经试过什么（--tried）。"}
    _item(root, config, job).block(str(question).strip(), {"tried": str(tried).strip()}, 0)
    _close(root, job, "blocked")
    return {"ok": True, "status": "blocked"}


def release(root, config, job_id, reason="驱动方放回"):
    job = get(root, job_id)
    if job["status"] != "open":
        return {"ok": True, "status": job["status"]}
    if job["kind"] == "item":
        _item(root, config, job).release()
    if job["kind"] == "setup":   # the AI could not do it: the owner sees why, and can ask again
        from . import setupasks, thread as conversation
        what = job["ref"]["what"]
        said = (setupasks.pending(root).get(what) or {}).get("said") or ""
        setupasks.done(root, what)
        conversation.set_setting(Path(root), "setup:gave_up:" + what,
                                 {"by": job.get("driver"), "why": said, "at": now().isoformat(timespec="seconds")})
    _close(root, job, "released")
    return {"ok": True, "status": "released"}


# ---- one item to another AI -------------------------------------------------------------------------------

def handoff_message(n, title, value):
    """What is sent to the AI that takes over one item: it says who it is (without taking everything else over),
    takes that one item, and carries on from where the last one stopped."""
    from .aidrivers import name_in
    from .setupshim import work_cmd
    xin, name = work_cmd(), name_in(value) or "<你的名字>"
    return f"""请你来做我电脑上「心响」里的一件事：#{n} {title}。心响是本机的待办工具；这件事之前可能有别的 AI 做过，任务单、背景、之前的对话和上一版都在它给你的工作台里，接着做，不要从头来。

1. 告诉心响你是谁：
{xin} hello --name "{name}" --app "<你所在的客户端>" --model "<你现在用的模型>" --helper
2. 领这件事：
{xin} next --driver "{name}" --n {n}
3. 进入返回的 folder，按 instructions 做，用里面写的命令报进度、交活。
4. 交完再运行一次 {xin} next --driver "{name}"：还有交给你的事就接着做，返回 idle 就结束。

请遵守：命令都由你来运行，不要让我自己去敲；不要在对话里问我要密码、验证码或密钥；对外发送、审批、付款、删除只准备材料，不要执行。"""


def hand_over(root, config, record_id, value, *, by, why="", n=None, title="", job=None):
    """Give one item to one AI (value as in aidrivers; empty = back to the AI chosen in settings).

    A run in progress stops: the AI that handed it over closes its own job; when the owner does it, whoever is
    working on it is told to stop. The item goes straight back to the queue for the new AI."""
    from . import aidrivers
    from .prework import add_note
    from .thread import setting as conversation_setting
    root = Path(root)
    usual = conversation_setting(root, "engine", "codex")
    to = aidrivers.label(value or usual)
    if value == usual:
        value = ""   # handed to the one chosen in settings anyway: it follows the settings again
    if job is not None:
        _close(root, job, "handed")
        _item(root, config, job).release()
    else:
        for jid in stop_record(root, record_id, why=f"本人把这件事交给了 {to}"):
            _item(root, config, get(root, jid)).release()
    aidrivers.assign(root, record_id, value)
    add_note(root, record_id, f"{by} 把这件事交给了 {to}" + (f"：{why}" if why else ""))
    out, target = {"ok": True, "status": "handed", "to": to}, value or usual
    if target in aidrivers.LOCAL:
        from .workenv import kick
        kick(root, config, force=True)
        out["note"] = f"{to} 会自己接手，不用发消息。"
    elif target == "cindy":
        out["note"] = "Cindy 下次来领活时会接手。"
    else:
        out["message"] = handoff_message(n, title, target)
        known = {d["name"]: d for d in aidrivers.known(root)}
        if (known.get(aidrivers.name_in(target) or "") or {}).get("online"):
            out["note"] = f"{to} 正在守着领活，会自己接手；它不在时再把 message 发给它。"
    return out


def handoff(root, config, job_id, to, why, driver):
    """The AI working on an item gives that one item to another AI."""
    from . import aidrivers
    job = get(root, job_id)
    if closed := _closed(job):
        return closed
    if job["kind"] != "item":
        return {"ok": False, "error": "只有事项能交给别的 AI；整理类的活做不了就 release。"}
    if not str(to or "").strip():
        return {"ok": False, "error": "要写交给谁：--to \"它的名字\"（比如 Claude Code、本机 Gemini、Cindy）。"}
    value = aidrivers.resolve(root, to)
    me = job.get("driver") or driver
    os.environ["XINXIANG_DRIVER"] = me   # the note says who handed it on
    if aidrivers.is_mine(value, me) or aidrivers.label(value) == me:
        return {"ok": False, "error": "这就是你自己。"}
    return hand_over(root, config, job["ref"]["record_id"], value, by=me, why=str(why or "").strip()[:200],
                     n=job.get("n"), title=job.get("title") or "", job=job)


# ---- command line ----------------------------------------------------------------------------------------

GUIDE = """# 接入心响：任何 AI 都可以驱动

你是本人的 AI 员工。循环做下面几步，直到没有活：

0. 先说你是谁（只用一次）：`{cmd} hello --name "<你的名字>" --app "<你所在的客户端>" --model "<你用的模型>"`。
   从这一刻起，心响的事和整理都交给你；本人把这段话发给另一个 AI，就换成那个 AI。
1. 运行 `{cmd} next --driver "<你的名字>" --wait 600 --session "<你这段对话的编号或链接>"`。没活时它会等着，有活立刻返回；
   返回 `{{"idle": true}}` 就再运行一次，一直守着，直到本人说停。
   `--session` 让本人在心响里看到你在做哪件事，并能直接打开你这段对话；没有就省略。
2. 返回的 JSON 里有 `folder`（工作文件夹）、`instructions`（这件活的完整说明）、`output`（要写出的文件）。
   进入 folder，按 instructions 做；需要飞书资料时用 `lark-cli`（只读）。
3. 按 instructions 末尾的命令报进度、交活：`{cmd} submit --job <job>`。返回 fix 就按 problem 改好再交。
4. 回到第 1 步。

规则：飞书里的东西只读；建文档、改表格、通知本人都由心响做。对外发送、审批、付款、删除只准备材料，不执行。
每件事的对话历史由心响保存，next 会把它交给你：接着上一个 AI 往下做，不要从头来。
"""


def main(argv=None):
    from .config import load
    p = argparse.ArgumentParser(prog="xinxiang", description="心响驱动协议：任何 AI 都能领活、交活")
    p.add_argument("command", choices=["hello", "next", "step", "say", "submit", "ask", "release", "handoff", "guide", "status", "gate"])
    p.add_argument("--name", help="hello：你是谁（Antigravity、Gemini、Claude Code、Cursor …）")
    p.add_argument("--app", default="", help="hello：你所在的客户端")
    p.add_argument("--model", default="", help="hello：你现在用的模型")
    p.add_argument("--wait", type=int, default=0, help="next：没活时最多等这么多秒再回来（期间有活立刻给你）")
    p.add_argument("--helper", action="store_true", help="hello：只来做交给你的事，不接手其余的")
    p.add_argument("--part", choices=["all", "work", "sort"], default="all", help="hello：接手全部，或只接「做事」/「整理」")
    p.add_argument("--n", type=int, help="next：领交给你的这一件（编号）")
    p.add_argument("--to", help="handoff：交给哪个 AI"); p.add_argument("--why", default="", help="handoff：为什么")
    p.add_argument("--kinds", help="只领这几类活，逗号分隔：item,triage,meeting,chat,learn")
    p.add_argument("--driver", default=os.environ.get("XINXIANG_DRIVER") or "外部 AI")
    p.add_argument("--job"); p.add_argument("--text"); p.add_argument("--question"); p.add_argument("--tried")
    p.add_argument("--session", default=os.environ.get("XINXIANG_SESSION"), help="你这段对话的编号或能打开它的链接")
    p.add_argument("--config", type=Path, default=Path("config.yaml"))
    args = p.parse_args(argv)
    path = args.config.resolve()
    root = path.parent
    os.environ["XINXIANG_DRIVER"] = args.driver
    install_shim(root)
    try:
        if args.command == "guide":
            print(GUIDE.format(cmd=_cmd()))
            return 0
        if args.command == "status":
            print(json.dumps({"open": [{k: j[k] for k in ("id", "kind", "title", "driver", "step")} for j in open_jobs(root)],
                              "drivers": drivers(root)}, ensure_ascii=False))
            return 0
        from . import aidrivers
        if args.command == "hello":
            # an AI says who it is: from now on it does the items and the sorting, until another one says hello
            try:
                take = False if args.helper else True if args.part == "all" else args.part
                me = aidrivers.hello(root, args.name or args.driver, app=args.app, model=args.model, take=take)
            except ValueError as exc:
                raise MatrixError(str(exc)) from None
            print(json.dumps({"ok": True, "you": me["name"],
                              "now": "记下了，交给你的事你来做" if args.helper else {"all": "心响的事和整理都交给你了", "work": "心响的「做事」交给你了",
                                                                                   "sort": "心响的「整理」交给你了"}[args.part],
                              "next": f'{_cmd()} next --driver "{me["name"]}"' + ("" if args.helper else " --wait 600")}, ensure_ascii=False))
            return 0
        aidrivers.seen(root, args.driver)
        config = load(path)
        if args.command == "gate":
            # a schedule's pre-run check: exit 0 = there is an item to do, 2 = nothing (skip this round, no tokens)
            from . import setupasks
            from .aidrivers import is_mine
            from .thread import setting
            if setupasks.pending(root) and is_mine(setting(root, "engine", "codex"), "Cindy") and not _busy(root, "setup"):
                print(json.dumps({"ok": True, "work": "setup"}, ensure_ascii=False))
                return 0
            if has_items_for(root, config, "Cindy"):
                print(json.dumps({"ok": True, "work": "item"}, ensure_ascii=False))
                return 0
            sorting = sort_runner(root) == "cindy" and has_sorting_for(root, config)
            if sorting:
                print(json.dumps({"ok": True, "work": sorting}, ensure_ascii=False))
                return 0
            print(json.dumps({"ok": True, "work": None}, ensure_ascii=False))
            return 2
        if args.command == "next":
            kinds = [k.strip() for k in (args.kinds or "").split(",") if k.strip()] or None
            only = _record_of(root, args.n) if args.n else None
            if args.n and not only:
                raise MatrixError(f"找不到 #{args.n}。")
            job = next_job(root, config, args.driver, kinds=kinds, only=only)
            if args.n and not job:
                raise MatrixError(f"#{args.n} 现在不在 AI 的待做里：可能已经交付、在等本人，或刚被叫停。")
            until = time.monotonic() + max(0, min(args.wait, 3600))
            while not job and time.monotonic() < until:   # nothing yet: stay on the line, so the AI need not poll
                time.sleep(5)
                aidrivers.seen(root, args.driver)
                job = next_job(root, config, args.driver, kinds=kinds)
            if job and args.session:
                from .sessions import bind
                bind(root, job["id"], args.session)
            out = public(job) if job else {"idle": True}
        elif not args.job:
            raise MatrixError("需要 --job。")
        elif args.command == "step":
            if args.session:
                from .sessions import bind
                bind(root, args.job, args.session)
            out = step(root, config, args.job, args.text)
        elif args.command == "say":
            out = say(root, args.job, args.text, args.driver)
        elif args.command == "submit":
            out = submit(root, config, args.job, args.driver)
        elif args.command == "ask":
            out = ask(root, config, args.job, args.question, args.tried)
        elif args.command == "handoff":
            out = handoff(root, config, args.job, args.to, args.why, args.driver)
        else:
            out = release(root, config, args.job)
        print(json.dumps(out, ensure_ascii=False))
        return 0
    except (MatrixError, MatrixBusy) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
