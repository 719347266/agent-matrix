"""Discover this user's ongoing AI conversations from local runtime records (Cindy, Claude, Codex). No network calls.

A conversation counts as ongoing while it is open: a live Claude process, a Cindy session or a Codex thread used in
the last few hours. Each one says which app it is in, what it is about (its title and the latest request), whether it
is replying right now, and which 心响 item it belongs to: a conversation clearly about one item (opened from that
item, or naming it) binds itself; the owner binds the rest by hand, or undoes a binding on the item."""
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time

from . import db as database
from .models import MatrixError
from .timeutil import TZ

ID = re.compile(r"[a-zA-Z0-9_-]{1,128}\Z")
HOST = re.compile(r"local_[A-Za-z0-9-]{1,64}")   # a Claude desktop conversation (claude://code/continue?session=…)
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")  # a Claude CLI conversation
LIVE_SECONDS = 3 * 3600  # Cindy / Codex: used within this window = still an open conversation
SHOWN = 10
NAMES = {"cindy": "Cindy", "codex": "Codex", "claude": "Claude"}
ISSUES = (OSError, sqlite3.Error, ValueError, TypeError, KeyError, OverflowError)


def _ro(p):
    return database.read(p, timeout=.2)


def _record(source, sid, title, stamp, *, sdk=None, cwd=None, working=True, doing="", app=None, live=False, host=None):
    if not isinstance(sid, str) or not ID.fullmatch(sid):
        return None
    return dict(key=f"{source}:{sid}", source=source, sid=sid, title=str(title or NAMES[source])[:100],
                stamp=float(stamp), sdk=sdk, cwd=str(cwd or ""), working=bool(working),
                doing=_short(doing), app=app or NAMES[source], live=bool(live),
                host=host if isinstance(host, str) and HOST.fullmatch(host) else None)


def _short(text, limit=140):
    text = " ".join(str(text or "").split())
    return text[:limit] + ("…" if len(text) > limit else "")


CINDY_SOURCES = {"scheduler": "Cindy 定时任务", "feishu": "Cindy 飞书"}


def _cindy(home, now):
    base = home / "Library/Application Support/Cindy"
    for p in base.glob("cindy-*.db"):
        with closing(_ro(p)) as db:
            cols = {c[1] for c in db.execute("PRAGMA table_info(sessions)")}
            pick = lambda name: name if name in cols else "NULL"
            for sid,title,start,ended,seen,engine_sid,cwd,preview,origin in db.execute(f"""
                SELECT id,title,active_turn_started_at,last_turn_ended_at,coalesce(last_activity_at,active_turn_started_at),
                       sdk_session_id,{pick("working_dir")},{pick("list_preview")},{pick("source")} FROM sessions
                WHERE status='active' AND hidden_at IS NULL
                  AND coalesce(last_activity_at,active_turn_started_at,0) BETWEEN ? AND ?
                ORDER BY coalesce(last_activity_at,active_turn_started_at) DESC LIMIT 64""",
                                    (int((now-LIVE_SECONDS)*1000),int((now+30)*1000))):
                working = bool(start) and (start or 0) > (ended or 0)
                yield _record("cindy",sid,title,(seen or start or 0)/1000,sdk=engine_sid,cwd=cwd,working=working,
                              doing=preview,app=CINDY_SOURCES.get(origin,"Cindy"),live=True)


def _alive(pid):
    from .procs import alive
    return type(pid) is int and alive(pid)   # not a signal-0 probe: on Windows that ends the process


def _plain_request(text):
    """What the owner typed: system notes dropped, paste wrappers removed, the words kept."""
    text = re.sub(r"<system-reminder>.*?</system-reminder>", " ", str(text or ""), flags=re.S)
    text = re.sub(r"</?[a-zA-Z][\w-]*(?:\s[^<>]*)?>", " ", text)
    return " ".join(text.split())


def _tail_lines(path, limit=16 * 1024 * 1024, block=512 * 1024):
    """Lines of a file from the end backwards, reading at most `limit` bytes."""
    with path.open("rb") as f:
        end = f.seek(0, 2)
        pos, rest = end, b""
        while pos > 0 and end - pos < limit:
            step = min(block, pos)
            pos -= step
            f.seek(pos)
            chunk = f.read(step) + rest
            lines = chunk.split(b"\n")
            rest = lines.pop(0) if pos > 0 else b""
            for line in reversed(lines):
                yield line
            if pos == 0 and rest:
                yield rest


def _last_request(home, sid):
    """The owner's latest request in a Claude conversation (its transcript), skipping tool results and system notes."""
    for path in (home/".claude/projects").glob(f"*/{sid}.jsonl"):
        for line in _tail_lines(path):
            if b'"type":"user"' not in line[:400] and b'"type": "user"' not in line[:400]:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("type") != "user" or obj.get("isMeta"):
                continue
            content = (obj.get("message") or {}).get("content")
            parts = [content] if isinstance(content, str) else [c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("type") == "text"]
            text = _plain_request(" ".join(x for x in parts if x))
            if text and not text.startswith(("Caveat:", "[Request interrupted")):
                return text
    return ""


def _claude(home, now):
    for p in (home/".claude/sessions").glob("[0-9]*.json"):
        if not p.stem.isdigit() or p.stat().st_size>32768:
            continue
        try:
            obj=json.loads(p.read_bytes())
            if not _alive(obj.get("pid")):
                continue  # a live Claude process is an open conversation, replying or waiting for the owner
            stamp=float(obj.get("updatedAt") or obj.get("statusUpdatedAt") or obj.get("startedAt") or now*1000)/1000
            sid=obj.get("sessionId")
            desktop=obj.get("entrypoint")=="claude-desktop"
            yield _record("claude",sid,obj.get("name"),min(stamp,now),cwd=obj.get("cwd"),
                          working=obj.get("status") in {"busy","running","working","processing"},
                          doing=_last_request(home,sid) if isinstance(sid,str) and ID.fullmatch(sid) else "",
                          app="Claude 桌面" if desktop else "Claude 命令行",live=True,host=obj.get("hostSessionId"))
        except ISSUES:
            continue


def _turn_signal(path,now):
    """Use typed turn events, never a recently opened thread or quoted text in a message."""
    if not path.is_file() or not -30 <= now-path.stat().st_mtime <= 300:
        return None
    with path.open("rb") as f:
        f.seek(max(0,f.seek(0,2)-131072))
        lines=f.read().splitlines()
    for line in reversed(lines):
        if b'"event_msg"' not in line[:160] or not any(t in line for t in (b'"task_started"',b'"task_complete"',b'"turn_aborted"')):
            continue
        try:
            ev=json.loads(line)
            if ev.get("type")!="event_msg": continue
            typ=ev.get("payload",{}).get("type")
            if typ in ("task_complete","turn_aborted"): return False
            if typ=="task_started":
                stamp=datetime.fromisoformat(ev["timestamp"].replace("Z","+00:00")).timestamp()
                return True if -30 <= now-stamp <= 300 else None
        except ISSUES:
            continue
    return None


def _codex(home,now):
    base=home/".codex"
    alive={}
    registry=base/"process_manager/chat_processes.json"
    if registry.is_file() and registry.stat().st_size<4000000:
        for proc in json.loads(registry.read_bytes()):
            if isinstance(proc,dict) and _alive(proc.get("osPid")) and now-300 <= float(proc.get("updatedAtMs") or 0)/1000 <= now+30:
                alive[proc.get("conversationId")]=proc.get("turnId")
    for p in sorted(base.glob("state_[0-9]*.sqlite"),reverse=True)[:1]:
        with closing(_ro(p)) as db:
            rows=db.execute("""SELECT id,title,cwd,rollout_path,updated_at FROM threads WHERE archived=0
                ORDER BY updated_at DESC LIMIT 48""").fetchall()
        for sid,title,cwd,rollout,updated in rows:
            if not rollout: continue
            try:
                path=Path(rollout).resolve()
                if not path.is_relative_to((base/"sessions").resolve()): continue
                signal = _turn_signal(path,now)
                stamp = min(now,max(float(updated or 0),path.stat().st_mtime))
                if signal is True or sid in alive or now-stamp <= LIVE_SECONDS:
                    yield _record("codex",sid,title,stamp,cwd=cwd,working=signal is True,doing=title,live=sid in alive)
            except ISSUES:
                continue


def discover(home=None):
    home=Path(home or Path.home())
    now=time.time()
    out={}
    for reader in (_cindy,_claude,_codex):
        try:
            for r in reader(home,now):
                if r: out[r["key"]]=r
        except ISSUES:
            continue  # unavailable app or older metadata schema must not interrupt the other sources
    owned={r["sdk"] for r in out.values() if r["source"]=="cindy" and r.get("sdk")}
    kept=[v for v in out.values() if v["source"]=="cindy" or v["sid"] not in owned]
    kept.sort(key=lambda r:(not r["working"],-r["stamp"]))  # replying first, then most recent
    return {r["key"]:r for r in kept[:SHOWN]}


def _db(root):
    return database.open(root, "xinxiang")


def _public(rec, present):
    """present: the conversation is open now (replying or waiting); otherwise only remembered on an item."""
    working = present and rec.get("working", True)
    return {"job":"local:"+rec["key"],"engine":{"codex":"gpt"}.get(rec["source"],rec["source"]),
            "driver":rec.get("app") or NAMES[rec["source"]],"session":rec["sid"],"title":rec["title"],
            "doing":rec.get("doing",""),"working":working,
            "status":"open" if working else ("idle" if present else "recorded"),"can_open":True,"local":True,
            "step":"正在回复" if working else ("等你" if present else ""),
            "started":datetime.fromtimestamp(rec["stamp"],TZ).strftime("%m-%d %H:%M")}


def _bind_of(root, rec, items):
    """The item a conversation is clearly about, or None: it runs in the item's own folder (opened from 心响),
    or it names one item ("#62", or the item's full title)."""
    from .workenv import work_base
    cwd = str(rec.get("cwd") or "")
    if cwd:
        base = str(work_base(root))
        for i in items:
            if cwd == f"{base}/{i['rid']}":
                return i["rid"]
    text = f"{rec.get('title','')} {rec.get('doing','')}"
    shown = [i for i in items if not i.get("hidden")]
    numbers = {int(x) for x in re.findall(r"#(\d{1,5})(?!\d)", text)}
    hit = [i for i in shown if i["n"] in numbers]
    if not hit:
        hit = [i for i in shown if len(i.get("title") or "") >= 6 and i["title"] in text]
    return hit[0]["rid"] if len(hit) == 1 else None


def view(root,by_item,running,items):
    """Ongoing conversations for the sidebar, each with its item. A conversation clearly about one item is bound to it
    by itself (and stays bound); the owner can undo that on the item. Every bound conversation is listed on its item."""
    obs=discover()
    with closing(_db(root)) as db, db:
        records={key:(rid,json.loads(data)) for key,rid,data in db.execute("SELECT key,rid,data FROM local_session_items")}
        skipped={key for (key,) in db.execute("SELECT key FROM local_session_skips")}
        for key,r in obs.items():
            if key in records or key in skipped:
                continue
            rid=_bind_of(root,r,items)
            if rid:
                db.execute("INSERT OR REPLACE INTO local_session_items VALUES(?,?,?)",(key,rid,json.dumps(r,ensure_ascii=False)))
                records[key]=(rid,r)
    assigned={i["rid"]:i["n"] for i in items}
    known={}
    for rid,sessions in by_item.items():
        for s in sessions:
            if s.get("session"):
                known.setdefault(s["session"],set()).add(rid)
    active={s.get("session") for s in running if s.get("session")}
    result=[]
    for key,(rid,r) in records.items():
        if rid not in assigned: continue
        if not any(s.get("session")==r["sid"] for s in by_item.get(rid,[])):
            now=obs.get(key)
            extra={**_public(now or r,key in obs),"key":key}
            by_item.setdefault(rid,[]).append({**extra,"first":extra["started"],"rounds":1})
    for key,r in obs.items():
        if r["sid"] in active or (r.get("sdk") and r["sdk"] in active): continue
        candidates=known.get(r["sid"],set()) | known.get(r.get("sdk"),set())
        rid=records[key][0] if key in records else next(iter(candidates)) if len(candidates)==1 else None
        result.append({**_public(r,True),"n":assigned.get(rid),"key":key})
    return result


def unbind(root, key):
    """The owner says a conversation is not about that item: unbind it and never bind it by itself again."""
    with closing(_db(root)) as db, db:
        db.execute("DELETE FROM local_session_items WHERE key=?", (key,))
        db.execute("INSERT OR IGNORE INTO local_session_skips VALUES(?)", (key,))
    return {"ok": True}


def associate(root,key,n):
    from .snapshot import _snapshot
    rec=discover().get(key)
    if not rec: raise MatrixError("这段会话已经不在运行，刷新后再试。")
    rid=next((r.get("record_id") for r in _snapshot(root).get("rows",[]) if str(r.get("编号"))==str(n)),None)
    if not rid: raise MatrixError("找不到这个事项编号。")
    with closing(_db(root)) as db, db:
        db.execute("INSERT OR REPLACE INTO local_session_items VALUES(?,?,?)",(key,rid,json.dumps(rec,ensure_ascii=False)))
    return {"n":n}


def open_claude(sid, host=None):
    """Into the Claude app, right at that conversation: a desktop one by its id, a command-line one imported by its id."""
    if host and HOST.fullmatch(host):
        url = "claude://code/continue?session=" + host
    elif sid and UUID.fullmatch(sid):
        url = "claude://resume?session=" + sid
    else:
        raise MatrixError("这段 Claude 对话没有可以打开的编号。")
    try:
        subprocess.run(["open", url], capture_output=True, check=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MatrixError("无法打开 Claude。") from exc
    return {"opened": "claude"}


def open_local(root,config,key):
    from . import sessions, workenv
    import shlex
    rec=discover().get(key)
    if not rec:
        with closing(_db(root)) as db:
            row=db.execute("SELECT data FROM local_session_items WHERE key=?",(key,)).fetchone()
            rec=json.loads(row[0]) if row else None
    if not rec or not ID.fullmatch(rec["sid"]): raise MatrixError("没有可打开的原会话。")
    src,sid=rec["source"],rec["sid"]
    if src=="cindy":
        try: subprocess.run(["open","cindy://session/"+sid],capture_output=True,check=True,timeout=10)
        except (OSError,subprocess.SubprocessError) as exc: raise MatrixError("无法打开本机 Cindy。") from exc
        return {"opened":"cindy"}
    if src=="claude":
        return open_claude(sid, rec.get("host"))
    try: subprocess.run(["open","codex://threads/"+sid],capture_output=True,check=True,timeout=10)
    except (OSError,subprocess.SubprocessError) as exc: raise MatrixError("无法打开这段 Codex 对话。") from exc
    return {"opened":"codex"}


CINDY_APP = Path("/Applications/Cindy.app")
CLAUDE_APP = Path("/Applications/Claude.app")
CHATGPT_APP = Path("/Applications/ChatGPT.app")
PICKUP = ("cindy", "claude", "terminal-claude", "terminal-codex", "terminal-gemini")   # where the owner picks an item up
GPT_OPEN = ("here", "chatgpt", "terminal")                         # where a built-in GPT conversation opens


def apps(config=None):
    """What this computer can use: the apps that are installed and the command-line tools on PATH."""
    from . import workenv
    return {"cindy": CINDY_APP.is_dir(), "claude": CLAUDE_APP.is_dir(), "chatgpt": CHATGPT_APP.is_dir(),
            "terminal-claude": bool(workenv.claude_binary(config)) if config else bool(__import__("shutil").which("claude")),
            "terminal-codex": bool(workenv.codex_binary(config)) if config else bool(__import__("shutil").which("codex")),
            "terminal-gemini": bool(workenv.gemini_binary(config)) if config else bool(__import__("shutil").which("gemini"))}


def pickup_choice(root, found=None):
    """The owner's choice for picking items up, or the first installed (Cindy, Claude, then the command line)."""
    from .thread import setting
    found = found or apps()
    chosen = setting(Path(root), "pickup_app", None)
    if chosen in PICKUP and found.get(chosen):
        return chosen
    return next((k for k in PICKUP if found.get(k)), "terminal-claude")


def gpt_open_choice(root, found=None):
    return "here"   # the built-in GPT's conversation is always read in 心响 (the choice was taken away)


def _gpt_said(root, record_id, limit=12):
    """What the built-in GPT said and handed in on this item (its own conversation files), newest last."""
    from . import sessions
    from .workenv import worker_codex_home
    home = worker_codex_home() or Path.home() / ".codex"
    said = []
    for s in reversed(sessions.by_item(root).get(record_id, [])):
        if s.get("engine") != "gpt" or not s.get("session") or not ID.fullmatch(str(s["session"])):
            continue
        for path in sorted(home.glob(f"sessions/*/*/*/rollout-*{s['session']}.jsonl")):
            said += [f"[{e['at']}] {e['text']}" for e in sessions.read_rollout(path) if e["who"] == "ai"]
    return said[-limit:]


def write_context(root, item, history="", *, buddy=False):
    """The item's folder becomes the place to pick it up: 心响事项.md with the facts, the conversation so far and what
    the built-in GPT already did, plus AGENTS.md / CLAUDE.md so whichever AI opens the folder reads it first."""
    from . import itemwork
    folder = itemwork.workdir_for(root, item["record_id"])
    lines = [f"# 心响事项 #{item['n']}：{item.get('title') or ''}", "",
             f"- 状态：{item.get('status') or ''}；谁来做：{item.get('who') or ''}；截止：{item.get('due') or '无'}",
             f"- 来源：{item.get('source') or '手动'} {item.get('source_url') or ''}".rstrip(),
             f"- 要做：{item.get('next') or item.get('title') or ''}"]
    if item.get("criterion"):
        lines.append(f"- 做到算完：{item['criterion']}")
    if item.get("quote"):
        lines += ["", "## 原话", str(item["quote"])[:1500]]
    if buddy:
        for label, key in (("补充材料", "extra"), ("退回意见", "rejected")):
            if item.get(key):
                lines += ["", "## " + label, str(item[key])]
    if item.get("url"):
        lines += ["", "## 已有交付", item["url"]]
    files = [name for name in ("交付.md", "任务单.md", "背景.md", "结果.json") if (folder / name).is_file()]
    if files:
        lines += ["", "## 这个文件夹里已有的材料", *[f"- {name}" for name in files]]
    if history:
        lines += ["", "## 这件事之前的对话（心响记录）", history]
    said = _gpt_said(root, item["record_id"])
    if said:
        lines += ["", "## 本机 GPT 之前做这件事时说的话", *said]
    (folder / "心响事项.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    guide = (f"这个文件夹是心响事项 #{item['n']}「{item.get('title') or ''}」的工作文件夹。\n"
             "接手时先读 心响事项.md：里面有来龙去脉、之前的对话和已经做出的东西。接着往下做，不要从头来；"
             "先用两三句话说清现在到哪一步、还差什么，再问本人接下来要做什么。\n")
    if buddy:
        guide = "豚豚接手：先读 心响事项.md、已有交付和最新反馈，再按所属项目规范直接续做；没有新反馈不重做、不代验收。\n"
    for name in ("AGENTS.md", "CLAUDE.md"):
        (folder / name).write_text(guide, encoding="utf-8")
    return folder


def start_in_cindy(root, config, item, history=""):
    """Pick an item up in Cindy (the owner's GPT subscription lives there): a new Cindy task in the item's own folder,
    which holds the whole context. The task binds itself to the item by that folder (see view), so later
    「打开对话」 goes straight back to it."""
    if not CINDY_APP.is_dir():
        raise MatrixError("这台电脑上没有找到 Cindy。")
    from . import cindy_pickup
    route = cindy_pickup.binding(root)
    folder = write_context(root, item, history, buddy=bool(route))
    if route:
        result = cindy_pickup.enqueue(root, route, item, folder, history)
        try:
            subprocess.run(["open", "-a", str(CINDY_APP), "cindy://session/" + route["session"]],
                           capture_output=True, check=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            result["navigation_failed"] = True  # independent of queue/receipt; never resend here
        return result
    try:
        subprocess.run(["open", "-a", str(CINDY_APP), "--", str(folder)], capture_output=True, check=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MatrixError("无法打开 Cindy。") from exc
    return {"opened": "cindy", "folder": str(folder)}


def pick_up(root, config, item, history="", where=None):
    """Pick an item up with the whole context in the item's folder: in the app chosen for this one item (`where`),
    else in the owner's usual one (设置 → 打开对话). Choosing for one item changes nothing else."""
    found = apps(config)
    if where is not None and (where not in PICKUP or not found.get(where)):
        raise MatrixError("这台电脑上没有这个 App。")
    where = where or pickup_choice(root, found)
    if where == "cindy":
        return start_in_cindy(root, config, item, history)
    if where == "claude":
        return start_for_item(root, config, item, history)
    return start_in_terminal(root, config, item, history, cli=where.split("-", 1)[1])


def start_in_terminal(root, config, item, history="", cli="claude"):
    """For people without a desktop app: the command-line AI in Terminal, in the item's folder."""
    from . import sessions, workenv
    import shlex
    folder = write_context(root, item, history)
    binary = {"claude": workenv.claude_binary, "gemini": workenv.gemini_binary}.get(cli, workenv.codex_binary)(config)
    if not binary:
        raise MatrixError(f"没有找到 {cli} 命令行。")
    prompt = "读当前文件夹的 心响事项.md，接着这件事往下做。先说清现在到哪一步、还差什么，再问我接下来要做什么。"
    sessions.open_terminal(f"cd {shlex.quote(str(folder))} && {shlex.join([binary, prompt])}")
    return {"opened": "terminal", "folder": str(folder)}


def open_gpt_in_chatgpt(thread_id):
    """A built-in GPT conversation in the ChatGPT app: only this one is linked into the owner's Codex folder
    (a hard link, so both sides share one file and the AI's next round sees what the owner said there)."""
    from .workenv import owner_codex_home, worker_codex_home
    home = worker_codex_home()
    if not home or not ID.fullmatch(thread_id):
        raise MatrixError("这段对话打不开。")
    found = sorted(home.glob(f"sessions/*/*/*/rollout-*{thread_id}.jsonl"))
    if not found:
        raise MatrixError("找不到这段对话的记录。")
    for path in found:
        target = owner_codex_home() / path.relative_to(home)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            try:
                os.link(path, target)
            except OSError as exc:
                raise MatrixError("没能把这段对话放进 ChatGPT 的 Codex。") from exc
    try:
        subprocess.run(["open", "codex://threads/" + thread_id], capture_output=True, check=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MatrixError("无法打开 ChatGPT。") from exc
    return {"opened": "chatgpt"}


def start_for_item(root, config, item, history=""):
    """The same, in the Claude app."""
    folder = write_context(root, item, history)
    prompt = "读当前文件夹的 心响事项.md，接着这件事往下做。先说清现在到哪一步、还差什么，再问我接下来要做什么。"
    from urllib.parse import urlencode
    url = "claude://code/new?" + urlencode({"folder": str(folder), "q": prompt})
    try:
        subprocess.run(["open", url], capture_output=True, check=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MatrixError("无法打开 Claude。") from exc
    return {"opened": "claude", "folder": str(folder)}


# ---- Cindy as the one doing items: its projects, its pickup schedule, the message that sets it up -------------

SCHEDULE_NAME = "心响｜领活来做"


def _cindy_db(home=None):
    base = Path(home or Path.home()) / "Library/Application Support/Cindy"
    found = sorted(base.glob("cindy-*.db"))
    return found[0] if found else None


def cindy_projects(home=None, limit=8):
    """The owner's Cindy projects (folders), most used first; bots, temporary and download folders left out."""
    db_path = _cindy_db(home)
    if not db_path:
        return []
    with closing(_ro(db_path)) as db:
        rows = db.execute("""SELECT coalesce(project_dir, working_dir) AS d, count(*) FROM sessions
            WHERE hidden_at IS NULL AND coalesce(project_dir, working_dir) IS NOT NULL GROUP BY d ORDER BY 2 DESC LIMIT 40""").fetchall()
    out = []
    for folder, count in rows:
        if not folder or any(x in folder for x in ("/bots/", "/tmp", "/Codex/", "/var/folders/", "/.agent-matrix/")):
            continue
        out.append({"folder": folder, "name": Path(folder).name, "tasks": count})
        if len(out) >= limit:
            break
    return out


def cindy_schedule(home=None):
    """Cindy's pickup schedule for 心响, read from Cindy's own records: set up or not, active, last run."""
    db_path = _cindy_db(home)
    if not db_path:
        return None
    with closing(_ro(db_path)) as db:
        cols = {c[1] for c in db.execute("PRAGMA table_info(schedules)")}
        pick = lambda c: c if c in cols else "NULL"
        row = db.execute(f"""SELECT id, status, cron_expr, working_dir, {pick("pre_run_hook_command")}, last_fired_at,
            {pick("last_finished_at")}, agent_kind FROM schedules WHERE name=? ORDER BY updated_at DESC LIMIT 1""", (SCHEDULE_NAME,)).fetchone()
        if not row:
            return None
        last = _cindy_last_run(db, row[0])
    sid, status, cron, folder, hook, fired, finished, kind = row
    when = lambda ms: datetime.fromtimestamp(int(ms) / 1000, TZ).strftime("%m-%d %H:%M") if ms else ""
    # the hook may run `xinxiang gate` itself or through the project's own check script: either way the gate's
    # answer ({"ok": .., "work": ..}) shows up in the hook output Cindy keeps for each run
    # (looked for in the last few rounds: the round going on right now has no answer written down yet)
    gate_seen = bool(last and last.get("answered"))
    return {"id": sid, "status": status, "cron": cron, "folder": folder or "", "engine": kind,
            # True: the gate answers; False: no hook at all; None: a hook that has not run yet, so not known
            "hooked": True if hook and ("gate" in hook or gate_seen) else (None if hook and not last else False),
            "last_run": when(fired), "last_done": when(finished),
            "last_check": ({"at": when(last["at"]), "result": last["result"]} if last else None)}


def _cindy_last_run(db, schedule_id):
    """Cindy's own log of the latest round: when, what the pre-run check said, whether the AI ran."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schedule_runs'").fetchone():
        return None
    cols = {c[1] for c in db.execute("PRAGMA table_info(schedule_runs)")}
    if not {"schedule_id", "fired_at", "status"} <= cols:
        return None
    hook = "pre_run_hook_result" if "pre_run_hook_result" in cols else "NULL"
    rows = db.execute(f"SELECT fired_at, status, {hook} FROM schedule_runs WHERE schedule_id=? ORDER BY fired_at DESC LIMIT 6",
                      (schedule_id,)).fetchall()
    if not rows:
        return None

    def said(out):
        try:
            return str(json.loads(out).get("stdout") or "") if out else ""   # Cindy keeps the hook result as JSON
        except (ValueError, AttributeError):
            return str(out or "")
    fired, status, out = rows[0]
    result = {"skipped": "没活，跳过", "running": "正在做", "failed": "出错了", "error": "出错了"}.get(status, "领了活" if status else "")
    return {"at": fired, "result": result, "hook": said(out), "answered": any('"work"' in said(o) for _, _, o in rows)}


_CINDY_SEEN = [0, [], None]   # when, projects, schedule: the last time Cindy's records could be read


def cindy_status(root):
    """For the settings page: installed, projects to choose from, and whether the pickup schedule is in place."""
    if not CINDY_APP.is_dir():
        return {"installed": False}
    from .thread import setting
    try:
        projects, schedule = cindy_projects(), cindy_schedule()
        _CINDY_SEEN[:] = [time.time(), projects, schedule]
    except ISSUES:
        # Cindy was writing its records at that moment: what was read a moment ago still holds
        projects, schedule = (_CINDY_SEEN[1], _CINDY_SEEN[2]) if time.time() - _CINDY_SEEN[0] < 900 else ([], None)
    chosen = setting(Path(root), "cindy_project", "") or (projects[0]["folder"] if projects else "")
    ok = bool(schedule and schedule["status"] == "active" and schedule["hooked"] is True)
    return {"installed": True, "projects": projects, "project": chosen, "schedule": schedule, "ready": ok,
            "same_project": bool(schedule and chosen and schedule["folder"] == chosen)}


def cindy_setup_text(root):
    """The one message the owner sends to Cindy: its own AI creates (or updates) the pickup schedule."""
    from .workenv import shim_path
    status = cindy_status(root)
    project = status.get("project") or "（你的项目文件夹）"
    cmd = str(shim_path())
    return f"""请用你的定时任务工具帮我建（已有同名的就改成下面这样）一个定时任务，替心响做事：

- 名称：{SCHEDULE_NAME}
- 频率：每 5 分钟（cronExpr="*/5 * * * *"，timezone="Asia/Shanghai"，recurring=true）
- 引擎：codex（用我在 Cindy 里绑定的 GPT 订阅），effort=high
- 工作目录（workingDir）：{project}
- silentWhenIdle=true；不发桌面和飞书通知
- 前置检查：用 schedule_set_pre_run_hook 挂一个前置检查，执行 `{cmd} gate`，原样沿用它的退出码：0 = 有活放行，2 = 没活跳过本轮（不烧 token）
- prompt 用下面这段：

你是本人在 Cindy 里的 AI 员工，替心响做事。一次只做一件，做完就结束。
1. 运行 `{cmd} next --driver Cindy`，拿到这件事（folder、instructions、job）。返回 {{"idle": true}} 就直接结束。
2. 回复第一行写「心响 #编号 标题」。
3. kind 是 item 的，属于当前项目：按本项目的规范、模板和记忆来做；心响只是派活、收交付的系统，不是要做需求的产品。kind 是 meeting、chat、triage、learn 的是整理类的活：只按 instructions 做，不要套用项目模板。
4. 按 instructions 做，交付文件写到 folder 里；按 instructions 末尾的命令报进度、交活：`{cmd} submit --job <job>`。返回 fix 就按 problem 改好再交。

建好后用 schedule_get 读回来给我看一眼：名称、频率、工作目录、前置检查都对就行。"""


def open_session(root, config, job_id):
    """Open one conversation so the owner can keep talking to it: a link → returned for the browser;
    a local GPT / Claude conversation → a Terminal window that resumes it."""
    if job_id.startswith("local:"):
        return open_local(root, config, job_id[6:])
    from . import sessions
    job = sessions._job(root, job_id)
    engine, session, link = sessions._session_of(job)
    if link:
        return {"link": link}
    if engine == "claude" and session:
        try:
            return {**open_claude(session), "engine": engine}
        except MatrixError:
            # no Claude desktop app to take claude:// links: the command line, resumed in the item's folder
            sessions.open_terminal(sessions.command(root, config, job_id))
            return {"opened": "terminal", "engine": engine}
    # the built-in GPT runs `codex exec`: read here (default), in the ChatGPT app, or resumed in Terminal (设置 → AI 对话)
    where = gpt_open_choice(root)
    if where == "chatgpt" and session:
        return {**open_gpt_in_chatgpt(session), "engine": engine}
    if where == "terminal" and session:
        sessions.open_terminal(sessions.command(root, config, job_id))
        return {"opened": "terminal", "engine": engine}
    return {"transcript": True, "engine": engine}
