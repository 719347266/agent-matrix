"""心响 · AI 会话：哪个 AI 在用哪段对话做哪件事，能打开接着聊。

Every job a driver takes runs in one AI conversation. The built-in driver keeps one conversation per item in
the item's folder (.thread for GPT / codex, .claude-session for Claude). An outside AI (Cindy, Claude Code,
Cursor …) says which conversation it is with `xinxiang next --session <id or link>`.

    running(root)              the sessions working right now and the item each one is on
    by_item(root)              every session that ever worked on an item, newest first
    open_session(root, key)    a link opens in the browser; a GPT / Claude conversation resumes in Terminal
"""
from datetime import datetime
import json
from pathlib import Path
import re
import shlex
import subprocess

from .models import MatrixError
from .timeutil import TZ

ENGINES = {"本机 GPT": "gpt", "本机 Claude": "claude", "本机 Gemini": "gemini"}
FILES = {"gpt": ".thread", "claude": ".claude-session", "gemini": ".gemini-session"}
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{3,120}")


def _jobs(root, where="1=1", args=()):
    from .jobstore import _db
    db = _db(root)
    try:
        cols = [c[1] for c in db.execute("PRAGMA table_info(jobs)")]
        rows = db.execute(f"SELECT * FROM jobs WHERE {where} ORDER BY created DESC", args).fetchall()
    finally:
        db.close()
    return [dict(zip(cols, r)) for r in rows]


def _engine(job):
    return ENGINES.get(job.get("driver") or "", "other")


def _session_of(job):
    """(engine, conversation id, link) of one job."""
    engine = _engine(job)
    session, link = job.get("session"), job.get("link")
    if engine in FILES and not session and job.get("folder"):
        found = Path(job["folder"]) / FILES[engine]
        if not found.is_file() and engine == "gpt":
            found = Path(job["folder"]) / FILES["claude"]  # the same driver name ran on Claude
            engine = "claude" if found.is_file() else engine
        session = found.read_text().strip() if found.is_file() else None
    return engine, session, link


def _minutes(since):
    try:
        return max(0, int((datetime.now(TZ) - datetime.fromisoformat(since)).total_seconds() // 60))
    except (TypeError, ValueError):
        return None


def _public(job):
    engine, session, link = _session_of(job)
    ref = json.loads(job.get("ref") or "{}") if isinstance(job.get("ref"), str) else (job.get("ref") or {})
    return {"job": job["id"], "kind": job["kind"], "title": job.get("title") or "", "n": job.get("n"),
            "record_id": ref.get("record_id"), "driver": job.get("driver") or "AI", "engine": engine,
            "session": session, "link": link, "status": ("expired" if job["status"] == "open" and (job.get("lease_until") or "") <= datetime.now(TZ).isoformat(timespec="seconds") else job["status"]), "step": job.get("step") or "",
            "started": str(job.get("created") or "")[:16].replace("T", " "), "minutes": _minutes(job.get("created")),
            "can_open": bool(link or (session and engine in FILES and SAFE_ID.fullmatch(session or "")))}


def running(root):
    """Sessions working right now: one per open job (lease still valid), newest first."""
    from .jobstore import now
    return [_public(j) for j in _jobs(root, "status='open' AND lease_until>?", (now().isoformat(timespec="seconds"),))]


def by_item(root):
    """{record_id: [sessions that worked on it, newest first]}; one entry per conversation."""
    out = {}
    for job in _jobs(root, "kind='item'"):
        s = _public(job)
        if not s["record_id"]:
            continue
        seen = out.setdefault(s["record_id"], [])
        key = (s["driver"], s["session"] or s["link"])
        same = next((x for x in seen if key[1] and (x["driver"], x["session"] or x["link"]) == key), None)
        if same:
            same["rounds"] = same.get("rounds", 1) + 1
            same["first"] = s["started"]
            continue
        seen.append({**s, "rounds": 1, "first": s["started"]})
    return out


def bind(root, job_id, session=None, link=None):
    """An outside AI tells which conversation it is working in (an id, or a link that opens it)."""
    from .jobstore import _db
    value = str(session or "").strip()
    if value.startswith(("http://", "https://")) and not link:
        value, link = "", value
    db = _db(root)
    try:
        with db:
            db.execute("UPDATE jobs SET session=coalesce(?, session), link=coalesce(?, link) WHERE id=?",
                       (value[:200] or None, (link or "").strip()[:500] or None, job_id))
    finally:
        db.close()


def _job(root, job_id):
    found = _jobs(root, "id=?", (job_id,))
    if not found:
        raise MatrixError("找不到这段 AI 会话。")
    return found[0]


def command(root, config, job_id):
    """The shell line that resumes this conversation in its own folder (shown to the owner, also run)."""
    job = _job(root, job_id)
    engine, session, link = _session_of(job)
    if not session or not SAFE_ID.fullmatch(session):
        raise MatrixError("这段会话没有记下对话编号，打不开。")
    folder = shlex.quote(str(job.get("folder") or Path.home()))
    if engine == "gpt":
        from .workenv import _env, codex_binary, provider_overrides
        binary = codex_binary(config)
        if not binary:
            raise MatrixError("找不到 codex 命令。")
        process_env = _env(config)
        home = process_env.get("CODEX_HOME")
        env = f"CODEX_HOME={shlex.quote(str(home))} " if home else ""
        flags = " ".join(shlex.quote(x) for x in provider_overrides())
        # GPT's tools break on non-ASCII PATH entries, so it gets a plain PATH (same as the worker)
        return f"cd {folder} && PATH={shlex.quote(process_env['PATH'])} {env}{shlex.quote(binary)} resume {flags} {shlex.quote(session)}"
    if engine == "claude":
        from .workenv import claude_binary
        binary = claude_binary(config)
        if not binary:
            raise MatrixError("找不到 claude 命令。")
        return f"cd {folder} && {shlex.quote(binary)} --resume {shlex.quote(session)}"
    raise MatrixError("这段会话不是本机的 GPT 或 Claude，用它自己的链接打开。")




def open_terminal(line):
    script = 'on run argv\n tell application "Terminal"\n do script (item 1 of argv)\n activate\n end tell\nend run'
    try:
        subprocess.run(["osascript", "-e", script, line], check=True, capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MatrixError("未能在终端中打开原对话，请检查终端是否可用以及系统的自动化权限。") from exc


# ---- reading a built-in GPT conversation inside 心响 --------------------------------------------------------

CMD = re.compile(r'cmd:\s*"((?:[^"\\]|\\.)*)"')


def _when(stamp):
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone(TZ).strftime("%m-%d %H:%M")
    except ValueError:
        return ""


def _command_of(payload):
    raw = payload.get("input") or payload.get("arguments") or ""
    found = CMD.search(raw)
    if found:
        try:
            return json.loads('"' + found.group(1) + '"')
        except ValueError:
            return found.group(1)
    try:
        args = json.loads(raw)
        cmd = args.get("cmd") or args.get("command")
        return " ".join(cmd) if isinstance(cmd, list) else str(cmd or "")
    except (ValueError, AttributeError):
        return ""


def read_rollout(path, limit=150):
    """One codex conversation as plain lines: the task it was given, what it said, what it ran, when a round ended."""
    entries = []
    with Path(path).open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            kind, p, at = o.get("type"), o.get("payload") or {}, _when(o.get("timestamp"))
            if kind == "response_item" and p.get("type") == "message":
                text = "\n".join(c.get("text", "") for c in p.get("content") or [] if isinstance(c, dict)).strip()
                if p.get("role") == "assistant" and text:
                    entries.append({"who": "ai", "text": text[:4000], "at": at})
                elif p.get("role") == "user" and text and not text.startswith("<"):
                    entries.append({"who": "task", "text": text[:6000], "at": at})
            elif kind == "response_item" and p.get("type") in ("custom_tool_call", "function_call"):
                cmd = _command_of(p)
                if cmd:
                    entries.append({"who": "run", "text": cmd[:400], "at": at})
            elif kind == "event_msg" and p.get("type") in ("task_complete", "turn_aborted"):
                entries.append({"who": "sys", "text": "这一轮做完了" if p["type"] == "task_complete" else "这一轮被停下了", "at": at})
    return entries[-limit:]


def transcript(root, config, job_id):
    """What the built-in GPT did in one conversation, for reading in 心响 (also while it is still working)."""
    job = _job(root, job_id)
    engine, session, _ = _session_of(job)
    if engine != "gpt" or not session or not SAFE_ID.fullmatch(session):
        raise MatrixError("这段对话不是本机 GPT 的，点「打开对话」去它自己的软件里看。")
    from .workenv import worker_codex_home
    home = worker_codex_home() or Path.home() / ".codex"
    files = sorted(home.glob(f"sessions/*/*/*/rollout-*{session}.jsonl"))
    if not files:
        raise MatrixError("找不到这段对话的记录。")
    entries = [e for f in files for e in read_rollout(f)]
    running = job["status"] == "open"
    return {"entries": entries[-150:], "running": running, "title": job.get("title") or "", "n": job.get("n"),
            "driver": job.get("driver") or "本机 GPT"}
