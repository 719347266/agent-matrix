"""The AI employee: up to ten concurrent jobs, one conversation per item, results come back by themselves.

    python -m agent_matrix.worker run        # work until nothing is left (started by the listener)
    python -m agent_matrix.worker kick       # start `run` in the background unless it is already running
    python -m agent_matrix.worker status     # what it is doing right now (JSON, for the panel)

Each available lane picks new items, meetings, chat batches and AI work through the shared driver protocol.
Each AI item gets its own folder under state/work/<record_id>/ and its own GPT conversation (codex
exec); later feedback from the owner resumes the same conversation instead of starting over.

The model only reads (Feishu, read-only) and writes files in its folder: 交付.md and 结果.json.
Code does everything else: claim, background pack, checks, creating the Feishu document, writing the
table, and sending the card. Nothing here holds the project lock while the model is thinking.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta
from glob import glob
from hashlib import sha256
import json
import os
from pathlib import Path
import queue as queue_mod
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

from . import activity
from .models import MatrixBusy, MatrixError
from .workenv import (now, status_path, read_status, _STATUS_LOCK, write_status, _alive, is_running, _version_key, codex_binary, owner_codex_home, worker_codex_home, _env, work_base, scratch, provider_overrides, project, _cli, _check, claude_binary, kick)  # noqa: F401
from .engines import (CodexRun, MCP_CACHE_SECONDS, MCP_DENY, mcp_servers, ClaudeRun, ENGINES, engine, test_engine)  # noqa: F401
from .itemwork import (SECTIONS_ALWAYS, TEMPLATE_HINT, _plain, _section_names, check_result, workdir_for, record_copy, _changes, _remember, first_prompt, resume_prompt, TRIAGE_LABEL, _pending_new, pick_next, Item)  # noqa: F401
from .state import State, exclusive_lock
from .timeutil import TZ

MAX_FIX_ROUNDS = 2
IDLE_POLL_SECONDS = 15  # a free lane looks for new work this often while other lanes are busy


# ---- small helpers ------------------------------------------------------------------------------------





































# ---- running the model ----------------------------------------------------------------------------------





# Whatever MCP is loaded, the AI only reads: it never sends, publishes, merges, deletes or edits shared workspaces
# (Notion pages, comments, calendars…) on its own. Deliverables go in its own folder, as before.












# ---- checking what came back ----------------------------------------------------------------------------







# ---- one AI item ---------------------------------------------------------------------------------------















# ---- new items: the AI decides whether it can do them (owner's switch in 心响) -----------------------------





# ---- the queue ------------------------------------------------------------------------------------------



def _tick(root, config, job, item, seen):
    """Mirror step.txt to the panel and to the job (and, once a minute, to the table's 进度)."""
    try:
        text = (item.workdir / "step.txt").read_text(encoding="utf-8").strip().splitlines()[0][:15]
    except (OSError, IndexError):
        return
    if text != seen.get("step"):
        seen["step"] = text
        from . import drive
        drive.step(root, config, job["id"], text)
        item.show("在做", text)


def run_item_job(root, config, job):
    """The built-in driver on one item: resume its own GPT conversation when it has one."""
    from . import drive
    item = drive._item(root, config, job)
    write_status(root, kind="item", record_id=item.rid, n=job.get("n"), title=(job.get("title") or "")[:26],
                 stage="找背景", step="读任务单和背景", item_started=now().isoformat(timespec="seconds"),
                 stage_at=now().isoformat(timespec="seconds"))
    Run = engine(root)
    thread_file, timed = item.workdir / Run.THREAD_FILE, item.workdir / ".timed_out"
    thread = thread_file.read_text().strip() if thread_file.is_file() else None
    prompt, seen, result = (job["builtin_prompt"] if thread else job["instructions"]), {}, {}
    stop = lambda: drive.get(root, job["id"])["status"] != "open"
    for _ in range(MAX_FIX_ROUNDS + 1):
        run = Run(config, item.workdir, thread=thread).run(prompt, on_tick=lambda: _tick(root, config, job, item, seen), should_stop=stop)
        if not run.stopped and not stop() and thread and not run.messages and not run.timed_out and run.returncode:
            # the old conversation cannot be resumed: start a new one, the history comes with the instructions
            thread_file.unlink(missing_ok=True)
            thread = None
            run = Run(config, item.workdir).run(job["instructions"], on_tick=lambda: _tick(root, config, job, item, seen), should_stop=stop)
        if run.stopped or stop():
            item.release()
            from .prework import add_note
            add_note(root, item.rid, f"本轮已停止，{Run.NAME}停下了")
            return {"status": "cancelled"}
        thread = run.thread or thread
        if thread:
            from .sessions import bind
            bind(root, job["id"], thread)
        item.mirror_reply()
        if run.timed_out or not thread:
            if run.timed_out:
                timed.write_text(now().isoformat())
            item.defer(f"这一轮超时（{config.worker_timeout_minutes} 分钟），下一轮接着做" if run.timed_out else f"{Run.NAME}没有启动成功，稍后重试", 0)
            drive._close(root, drive.get(root, job["id"]), "released")
            return {"status": "deferred"}
        timed.unlink(missing_ok=True)
        item.show("自检", "程序检查交付")
        result = drive.submit(root, config, job["id"])
        if result.get("status") != "fix":
            return result
        prompt = f"程序检查没通过：{result['problem']}\n改好 交付.md / 结果.json 再结束。"
    return result


def run_job(root, config, job):
    """The built-in driver on any job; extraction and triage run once, plus one round of fixes."""
    from . import drive
    if job["kind"] == "item":
        return run_item_job(root, config, job)
    write_status(root, kind=job["kind"], stage=job.get("title") or job["kind"], step="", n=None, title=(job.get("title") or "")[:26],
                 stage_at=now().isoformat(timespec="seconds"))
    folder = Path(job["folder"])
    Run = engine(root, "sort")
    run = Run(config, folder, effort=config.extract_effort, timeout_minutes=15).run(job["instructions"])
    if run.thread:
        from .sessions import bind
        bind(root, job["id"], run.thread)
    result = drive.submit(root, config, job["id"])
    if result.get("status") == "fix" and run.thread:
        Run(config, folder, thread=run.thread, effort=config.extract_effort, timeout_minutes=10).run(
            f"程序检查没通过：{result['problem']}\n改好 {'、'.join(job.get('output') or [])} 再结束。")
        result = drive.submit(root, config, job["id"])
    if result.get("status") == "fix":
        return drive.fail(root, drive.get(root, job["id"]), result.get("problem") or "改了一轮还是没通过检查")
    return result


def run_loop(root, config, *, max_jobs=200):
    """The built-in driver (本机 GPT): up to `worker_concurrency` jobs at the same time (default 10).

    Taking a job is serialised (so no job is taken twice); the work itself runs in parallel. A lane that
    finds nothing keeps looking while other lanes are busy, so new work never waits for the whole batch."""
    from . import activity, drive, thread as conversation
    root = Path(root)
    if not conversation.setting(root, "builtin_driver", True):
        return {"ok": True, "jobs": 0, "builtin": "off"}
    if os.name != "nt":
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit(143)))
    name = engine(root).NAME
    os.environ["XINXIANG_DRIVER"] = name
    width = max(1, min(20, int(getattr(config, "worker_concurrency", 10) or 1)))
    take, active, failed, counter = threading.Lock(), {}, set(), {"n": 0}

    def lane():
        while True:
            with take:
                if counter["n"] >= max_jobs:
                    return
                try:
                    # items go to Cindy when the owner chose it (设置 → AI 执行); this driver then only sorts
                    kinds = drive.local_kinds(root)   # what was handed to Cindy is Cindy's
                    job = drive.next_job(root, config, name, skip=set(failed), kinds=kinds) if kinds else None
                except (MatrixError, MatrixBusy, OSError) as exc:
                    activity.log(root, "error", f"{name}领活出错", detail=str(exc)[:300], status="failed")
                    job = None
                if job:
                    active[job["id"]], counter["n"] = job, counter["n"] + 1
                elif not active:
                    return  # nothing to do and nobody busy: done
            if not job:
                time.sleep(IDLE_POLL_SECONDS)  # others are still working; new work may arrive meanwhile
                continue
            try:
                activity.beat(root, "AI 员工", f"在做 {len(active)} 件")
                run_job(root, config, job)
            except (MatrixError, MatrixBusy, OSError) as exc:
                if job.get("ref", {}).get("record_id"):
                    with take:
                        failed.add(job["ref"]["record_id"])
                activity.log(root, "error", f"{name}处理「{str(job.get('title'))[:16]}」出错", detail=str(exc)[:300], status="failed")
                try:
                    drive.release(root, config, job["id"])
                except Exception as exc:
                    activity.swallowed(root, "worker.run_loop", exc)
            finally:
                with take:
                    active.pop(job["id"], None)

    with exclusive_lock(root / "state/worker", wait=0):
        write_status(root, running=True, pid=os.getpid(), started_at=now().isoformat(timespec="seconds"), stage="开始",
                     step="", record_id=None, n=None, title="", lanes=width)
        lanes = [threading.Thread(target=lane, daemon=True, name=f"lane-{k}") for k in range(width)]
        try:
            for t in lanes:
                t.start()
            for t in lanes:
                while t.is_alive():
                    t.join(timeout=1)
        finally:
            for job in list(active.values()):  # interrupted: give unfinished work back at once
                try:
                    drive.release(root, config, job["id"])
                except Exception as exc:
                    activity.swallowed(root, "worker.run_loop", exc)
            write_status(root, running=False, stage="空闲", step="", record_id=None, n=None, title="", queue=[])
            activity.beat(root, "AI 员工", "空闲")
    return {"ok": True, "jobs": counter["n"]}




def main(argv=None):
    from .config import load
    parser = argparse.ArgumentParser(description="AI 员工：最多并发十件，每件一个对话")
    parser.add_argument("command", choices=["run", "kick", "status"])
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    args = parser.parse_args(argv)
    path = args.config.resolve()
    config = load(path)
    if args.command == "status":
        print(json.dumps({**read_status(path.parent), "alive": is_running(path.parent)}, ensure_ascii=False))
        return 0
    if args.command == "kick":
        print(json.dumps({"ok": True, "worker": kick(path.parent, config)}))
        return 0
    if not config.worker_enabled:
        print(json.dumps({"ok": True, "worker": "off"}))
        return 0
    try:
        print(json.dumps(run_loop(path.parent, config), ensure_ascii=False))
        return 0
    except MatrixBusy:
        print(json.dumps({"ok": True, "worker": "already_running"}))
        return 0
    except Exception as exc:  # the panel must see why the employee stopped
        from . import activity
        activity.log(path.parent, "error", "AI 员工停下了", detail=f"{type(exc).__name__}: {exc}"[:300], status="failed")
        write_status(path.parent, running=False, stage="出错", step=str(exc)[:40])
        raise


if __name__ == "__main__":
    raise SystemExit(main())
