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
from uuid import uuid4

from .models import MatrixBusy, MatrixError
from .timeutil import TZ

ITEM_LEASE_MINUTES = 120
OTHER_LEASE_MINUTES = 30
MAX_FIX_ROUNDS = 2


def now():
    return datetime.now(TZ)


# ---- the job table (state/xinxiang.db) ------------------------------------------------------------------

def _db(root):
    from .thread import _db as base
    db = base(root)
    db.execute("""CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT, n INTEGER, ref TEXT,
        folder TEXT, driver TEXT, status TEXT NOT NULL, step TEXT, rounds INTEGER NOT NULL DEFAULT 0, lease_until TEXT,
        created TEXT NOT NULL, updated TEXT NOT NULL, progress_at TEXT)""")
    return db


def _save(root, job):
    db = _db(root)
    try:
        with db:
            cursor = db.execute("""INSERT INTO jobs (id, kind, title, n, ref, folder, driver, status, step, rounds, lease_until,
                created, updated, progress_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET status=excluded.status, step=excluded.step, rounds=excluded.rounds,
                    lease_until=excluded.lease_until, updated=excluded.updated, progress_at=excluded.progress_at
                WHERE jobs.status='open'""",
                       (job["id"], job["kind"], job.get("title"), job.get("n"), json.dumps(job.get("ref") or {}, ensure_ascii=False),
                        job.get("folder"), job.get("driver"), job["status"], job.get("step"), job.get("rounds", 0),
                        job.get("lease_until"), job.get("created") or now().isoformat(timespec="seconds"),
                        now().isoformat(timespec="seconds"), job.get("progress_at")))
        return cursor.rowcount > 0
    finally:
        db.close()


def get(root, job_id):
    db = _db(root)
    try:
        row = db.execute("SELECT id, kind, title, n, ref, folder, driver, status, step, rounds, lease_until, created, updated, progress_at "
                         "FROM jobs WHERE id=?", (job_id,)).fetchone()
    finally:
        db.close()
    if not row:
        raise MatrixError("找不到这件活：" + str(job_id))
    keys = ("id", "kind", "title", "n", "ref", "folder", "driver", "status", "step", "rounds", "lease_until", "created", "updated", "progress_at")
    job = dict(zip(keys, row))
    job["ref"] = json.loads(job["ref"] or "{}")
    return job


def open_jobs(root):
    """Jobs someone is working on right now (lease still valid), newest first."""
    db = _db(root)
    try:
        rows = db.execute("SELECT id FROM jobs WHERE status='open' AND lease_until>? ORDER BY updated DESC",
                          (now().isoformat(timespec="seconds"),)).fetchall()
    finally:
        db.close()
    return [get(root, r[0]) for r in rows]


def drivers(root, days=7):
    """Who has been driving lately: name, last seen, jobs finished."""
    db = _db(root)
    try:
        since = (now() - timedelta(days=days)).isoformat()
        return [{"driver": d, "last": last[5:16].replace("T", " "), "done": done, "open": opened}
                for d, last, done, opened in db.execute(
                    "SELECT driver, max(updated), sum(status='done'), sum(status='open') FROM jobs WHERE updated>=? AND driver IS NOT NULL "
                    "GROUP BY driver ORDER BY max(updated) DESC", (since,))]
    finally:
        db.close()


def stop_record(root, record_id, why="本人叫停"):
    """The owner marked an item 不做 / 完成 while an AI works on it: its open jobs stop here."""
    db = _db(root)
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            stopped = [jid for jid, ref in db.execute("SELECT id, ref FROM jobs WHERE kind='item' AND status='open'")
                       if json.loads(ref or "{}").get("record_id") == record_id]
            db.executemany("UPDATE jobs SET status='cancelled',step=?,lease_until=NULL,updated=? WHERE id=?",
                           [(why, now().isoformat(timespec="seconds"), jid) for jid in stopped])
        return stopped
    finally:
        db.close()


STOPPED = {"ok": False, "status": "cancelled", "note": "本人已经把这件事标为不做或已完成：停下，不用再做、也不用交了。"}


def _closed(job):
    if job["status"] == "cancelled":
        return dict(STOPPED)
    if job["status"] != "open":
        return {"ok": True, "status": job["status"], "note": "这件活已经结束了。"}
    return None


def _busy(root, kind):
    return any(j["kind"] == kind for j in open_jobs(root))


def _lease(minutes):
    return (now() + timedelta(minutes=minutes)).isoformat(timespec="seconds")


# ---- how an outside AI calls back --------------------------------------------------------------------

def shim_path():
    base = Path.home() / ".agent-matrix" / "bin"
    return base / ("xinxiang.cmd" if os.name == "nt" else "xinxiang")


def install_shim(root):
    """A command with a plain path any AI can call (the project path may contain Chinese)."""
    path = shim_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            body = f'@echo off\r\ncd /d "{Path(root).resolve()}"\r\n"{sys.executable}" -m agent_matrix.drive %*\r\n'
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
    if job["kind"] == "item":
        lines += [f"- 想对本人说的话：`{c} say --job {j} --text \"……\"`（会记进这件事的对话）",
                  f"- 卡住：`{c} ask --job {j} --question \"问本人的一个具体问题\" --tried \"已经试过什么\"`"]
    lines += [f"- 做不了：`{c} release --job {j}`，放回队列。",
              "- 任何调用返回 cancelled：本人已叫停，立即结束本轮和已启动的命令，不再写入或提交这件事。",
              "然后再运行一次 next 领下一件，直到返回 idle。"]
    return "\n".join(lines)


# ---- preparing each kind of job ----------------------------------------------------------------------

def _prepare_triage(root, config, driver):
    from . import thread as conversation
    from .worker import _pending_new, project, scratch
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
            "instructions": (Path(root) / "prompts/triage.md").read_text(encoding="utf-8")}


LEARN_MIN_NEW, LEARN_EVERY_HOURS = 3, 24


def _prepare_learn(root, config, driver):
    """Now and then: turn the owner's recent feedback into standing rules (偏好快照)."""
    from . import memory, thread as conversation
    from .worker import scratch
    if _busy(root, "learn"):
        return None
    since = conversation.setting(root, "learned_at", "")
    hist = conversation.history(root)
    feedback = []
    snap = {}
    try:
        from .monitor import _snapshot
        snap = {r.get("record_id"): r for r in _snapshot(root).get("rows") or []}
    except Exception:
        pass
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
            "instructions": (Path(root) / "prompts/learn.md").read_text(encoding="utf-8")}


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
    from .worker import _cli
    if not (Path(source_dir) / "review.json").is_file():
        return False
    code, out = _cli(root, *validate)
    if code != 0 or not out.get("ok"):
        return False
    code, out = _cli(root, *publish)
    if not out.get("ok"):
        activity.log(root, "error", f"{label}待办入表没成功", detail=str(out.get("error"))[:200], status="failed")
    return True


def _prepare_meeting(root, config, driver):
    from .meeting_review import Review
    from .worker import _check, _cli, scratch
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
        topic = str((prep.get("metadata") or {}).get("topic") or m.get("topic") or "会议")
        header = (f"本次会议：{topic}。meeting_id = {mid}，transcript_sha256 = {prep['transcript_sha256']}。\n"
                  f"逐字稿：transcript.md；没完成的事项：{open_name}；review.json 的结构见 review.schema.json。\n"
                  "只需要在当前文件夹写好 review.json；校验和发布由程序做。")
        return {"id": f"meeting-{mid}", "kind": "meeting", "title": f"提取会议待办：{topic}"[:40], "folder": str(folder),
                "ref": {"meeting_id": mid, "source": str(source), "validate": validate, "publish": publish},
                "output": ["review.json"], "lease_until": _lease(OTHER_LEASE_MINUTES),
                "instructions": header + "\n\n" + (Path(root) / "prompts/meeting_extract.md").read_text(encoding="utf-8")}
    return None


def _prepare_chat(root, config, driver):
    from .chat_review import ChatReview
    from .worker import _check, _cli, scratch
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
    try:  # what was said after each candidate, so things already settled are not turned into todos
        from .background import add_followups
        from .worker import project
        path = folder / Path(prep["candidates_path"]).name
        with project(root, config) as (client, state):
            pass
        data = add_followups(client, json.loads(path.read_text(encoding="utf-8")), now())
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass
    header = (f"本批聊天：batch_id = {batch}。候选消息：{Path(prep['candidates_path']).name}；结构见 review.schema.json。\n"
              "只需要在当前文件夹写好 review.json；校验和发布由程序做。")
    return {"id": f"chat-{re.sub(r'[^A-Za-z0-9_-]', '', batch)}", "kind": "chat", "title": "提取聊天待办", "folder": str(folder),
            "ref": {"source": str(source), "validate": validate, "publish": publish}, "output": ["review.json"],
            "lease_until": _lease(OTHER_LEASE_MINUTES),
            "instructions": header + "\n\n" + (Path(root) / "prompts/chat_extract.md").read_text(encoding="utf-8")}


def _history_text(root, record_id, limit=30):
    from . import thread as conversation
    msgs = conversation.history(root, [record_id]).get(record_id, {}).get("messages", [])[-limit:]
    who = {"me": "本人", "ai": "AI", "system": "系统"}
    return "\n".join(f"- {m['at']} {m.get('driver') if m['from'] == 'ai' and m.get('driver') else who.get(m['from'], '')}：{m['text'][:400]}"
                     for m in msgs)


def _prepare_item(root, config, driver, skip=()):
    from .worker import Item, _changes, first_prompt, pick_next, project, resume_prompt
    pick = pick_next(root, config, skip=skip)
    if not pick:
        return None
    item = Item(root, config, pick)
    got = item.claim(lease_minutes=ITEM_LEASE_MINUTES)
    if not got:
        return {"skip": pick["record_id"]}
    client, rows, row, notes = got
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


def next_job(root, config, driver, *, skip=()):
    """The next piece of work for this driver, already prepared; None when there is nothing to do."""
    root = Path(root)
    for prepare in (_prepare_triage, _prepare_meeting, _prepare_chat, _prepare_learn):
        job = prepare(root, config, driver)
        if job:
            return _open(root, job, driver)
    tried = set(skip)
    for _ in range(5):
        job = _prepare_item(root, config, driver, skip=tried)
        if not job:
            return None
        if "skip" in job:
            tried.add(job["skip"])
            continue
        # Preparing background may take time; the owner may have cancelled meanwhile.
        # Publish the lease under the same lock used by owner commands.
        from .prework import current
        from .worker import project
        with project(root, config) as (client, state):
            current(config, client, state, job["ref"]["record_id"], job["ref"]["version"])
            return _open(root, job, driver)
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
    except Exception:
        pass
    job.update(driver=driver, status="open", rounds=0, step="领到了")
    _save(root, job)
    return job


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
            from .worker import project
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
    except Exception:
        pass
    return {"ok": False, "status": "failed", "problem": str(problem)[:300]}


def _close(root, job, status):
    job.update(status=status, lease_until=None)
    _save(root, job)


def _item(root, config, job):
    from .worker import Item
    item = Item(root, config, {"record_id": job["ref"]["record_id"], "input_version": job["ref"]["version"], "title": job.get("title")})
    item.n, item.finished, item.job_id = job.get("n"), False, job["id"]
    return item


def submit(root, config, job_id, driver=None):
    """Check the output; publish it, or say exactly what to fix (the job stays open for the fix)."""
    job = get(root, job_id)
    if job["status"] == "cancelled":
        return STOPPED
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
    from . import activity
    from .worker import _cli
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
    from .worker import _remember, check_result
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


def _row_of(root, record_id):
    from .monitor import _snapshot
    return next((r for r in _snapshot(root).get("rows") or [] if r.get("record_id") == record_id), {})


def _submit_triage(root, job):
    from . import activity, thread as conversation
    from .worker import TRIAGE_LABEL
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
            run = subprocess.run([sys.executable, "-m", "agent_matrix.reply", "--text", command], cwd=root, capture_output=True,
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
    _close(root, job, "released")
    return {"ok": True, "status": "released"}


# ---- command line ----------------------------------------------------------------------------------------

GUIDE = """# 接入心响：任何 AI 都可以驱动

你是本人的 AI 员工。循环做下面几步，直到没有活：

1. 运行 `{cmd} next --driver "<你的名字>"`。返回 `{{"idle": true}}` 就结束。
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
    p.add_argument("command", choices=["next", "step", "say", "submit", "ask", "release", "guide", "status"])
    p.add_argument("--driver", default=os.environ.get("XINXIANG_DRIVER") or "外部 AI")
    p.add_argument("--job"); p.add_argument("--text"); p.add_argument("--question"); p.add_argument("--tried")
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
        config = load(path)
        if args.command == "next":
            job = next_job(root, config, args.driver)
            out = public(job) if job else {"idle": True}
        elif not args.job:
            raise MatrixError("需要 --job。")
        elif args.command == "step":
            out = step(root, config, args.job, args.text)
        elif args.command == "say":
            out = say(root, args.job, args.text, args.driver)
        elif args.command == "submit":
            out = submit(root, config, args.job, args.driver)
        elif args.command == "ask":
            out = ask(root, config, args.job, args.question, args.tried)
        else:
            out = release(root, config, args.job)
        print(json.dumps(out, ensure_ascii=False))
        return 0
    except (MatrixError, MatrixBusy) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
