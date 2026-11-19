"""Discover this user's ongoing AI conversations from local runtime records (Cindy, Claude, Codex). No network calls.

A conversation counts as ongoing while it is open: a live Claude process, a Cindy session or a Codex thread used in
the last few hours. Each one says which app it is in, what it is about (its title and the latest request), whether it
is replying right now, and which 心响 item it belongs to (an exact recorded binding, or a suggestion the owner confirms)."""
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time

from .models import MatrixError
from .timeutil import TZ

ID = re.compile(r"[a-zA-Z0-9_-]{1,128}\Z")
LIVE_SECONDS = 3 * 3600  # Cindy / Codex: used within this window = still an open conversation
SHOWN = 10
NAMES = {"cindy": "Cindy", "codex": "Codex", "claude": "Claude"}
ISSUES = (OSError, sqlite3.Error, ValueError, TypeError, KeyError, OverflowError)


def _ro(p):
    return sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True, timeout=.2)


def _record(source, sid, title, stamp, *, sdk=None, cwd=None, working=True, doing="", app=None, live=False):
    if not isinstance(sid, str) or not ID.fullmatch(sid):
        return None
    return dict(key=f"{source}:{sid}", source=source, sid=sid, title=str(title or NAMES[source])[:100],
                stamp=float(stamp), sdk=sdk, cwd=str(cwd or ""), working=bool(working),
                doing=_short(doing), app=app or NAMES[source], live=bool(live))


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
    if type(pid) is not int or pid<=1:
        return False
    try:
        os.kill(pid,0)
        return True
    except OSError:
        return False


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
                          app="Claude 桌面" if desktop else "Claude 命令行",live=True)
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
    from .thread import _db as base
    db=base(root)
    db.execute("""CREATE TABLE IF NOT EXISTS local_session_items (
       key TEXT PRIMARY KEY, rid TEXT NOT NULL, data TEXT NOT NULL)""")
    return db


def _public(rec, present):
    """present: the conversation is open now (replying or waiting); otherwise only remembered on an item."""
    working = present and rec.get("working", True)
    return {"job":"local:"+rec["key"],"engine":{"codex":"gpt"}.get(rec["source"],rec["source"]),
            "driver":rec.get("app") or NAMES[rec["source"]],"session":rec["sid"],"title":rec["title"],
            "doing":rec.get("doing",""),"working":working,
            "status":"open" if working else ("idle" if present else "recorded"),"can_open":True,"local":True,
            "step":"正在回复" if working else ("等你" if present else ""),
            "started":datetime.fromtimestamp(rec["stamp"],TZ).strftime("%m-%d %H:%M")}


def _suggest(rec, items):
    """A likely item for an unbound conversation: "#62" in its title or latest request, or an item's full title.
    Only a suggestion; the owner confirms it with one click."""
    text = f"{rec.get('title','')} {rec.get('doing','')}"
    open_items = [i for i in items if not i.get("hidden") and i.get("status") not in ("完成", "取消")]
    numbers = {int(x) for x in re.findall(r"#(\d{1,5})(?!\d)", text)}
    hit = [i for i in open_items if i["n"] in numbers]
    if not hit:
        hit = [i for i in open_items if len(i.get("title") or "") >= 6 and i["title"] in text]
    return hit[0]["n"] if len(hit) == 1 else None


def view(root,by_item,running,items):
    """Attach by exact recorded conversation ID only. Never infer an item from its title."""
    obs=discover()
    with closing(_db(root)) as db:
        records={key:(rid,json.loads(data)) for key,rid,data in db.execute("SELECT key,rid,data FROM local_session_items")}
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
            extra=_public(r,key in obs)
            by_item.setdefault(rid,[]).append({**extra,"first":extra["started"],"rounds":1})
    for key,r in obs.items():
        if r["sid"] in active or (r.get("sdk") and r["sdk"] in active): continue
        candidates=known.get(r["sid"],set()) | known.get(r.get("sdk"),set())
        rid=records[key][0] if key in records else next(iter(candidates)) if len(candidates)==1 else None
        n=assigned.get(rid)
        result.append({**_public(r,True),"n":n,"key":key,"suggest":None if n else _suggest(r,items)})
    return result


def associate(root,key,n):
    from .monitor import _snapshot
    rec=discover().get(key)
    if not rec: raise MatrixError("这段会话已经不在运行，刷新后再试。")
    rid=next((r.get("record_id") for r in _snapshot(root).get("rows",[]) if str(r.get("编号"))==str(n)),None)
    if not rid: raise MatrixError("找不到这个事项编号。")
    with closing(_db(root)) as db, db:
        db.execute("INSERT OR REPLACE INTO local_session_items VALUES(?,?,?)",(key,rid,json.dumps(rec,ensure_ascii=False)))
    return {"n":n}


def open_local(root,config,key):
    from . import sessions, worker
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
    if src=="claude" and rec.get("live") and rec.get("app")=="Claude 桌面" and key in discover():
        # still open in the Claude app: bring it to the front (a second copy in Terminal would talk over it)
        try: subprocess.run(["open","-a","Claude"],capture_output=True,check=True,timeout=10)
        except (OSError,subprocess.SubprocessError) as exc: raise MatrixError("无法打开 Claude。") from exc
        return {"opened":"claude-app","hint":f"在 Claude 左侧会话列表里找「{rec.get('title') or '这段对话'}」"}
    binary=(worker.claude_binary if src=="claude" else worker.codex_binary)(config)
    if not binary: raise MatrixError("没有找到 "+NAMES[src]+" 命令行。")
    cwd=Path(rec.get("cwd") or Path.home())
    if not cwd.is_dir(): raise MatrixError("原会话的工作目录已不存在。")
    args=[binary,"--resume" if src=="claude" else "resume",sid]
    if src=="codex": args=["env","CODEX_HOME="+str(Path.home()/".codex"),*args]
    sessions.open_terminal("cd "+shlex.quote(str(cwd))+" && "+shlex.join(args))
    return {"opened":"terminal"}


def start_for_item(root, config, item, history=""):
    """Open a new Claude conversation for one item, already bound to it: the item's facts and its conversation so far
    go into a note in the item's folder, so the owner does not paste the context again."""
    from . import sessions, worker
    from uuid import uuid4
    import shlex
    binary = worker.claude_binary(config)
    if not binary:
        raise MatrixError("没有找到 Claude 命令行（claude）。")
    folder = worker.workdir_for(root, item["record_id"])
    lines = [f"# 心响事项 #{item['n']}：{item.get('title') or ''}", "",
             f"- 状态：{item.get('status') or ''}；谁来做：{item.get('who') or ''}；截止：{item.get('due') or '无'}",
             f"- 来源：{item.get('source') or '手动'} {item.get('source_url') or ''}".rstrip(),
             f"- 要做：{item.get('next') or item.get('title') or ''}"]
    if item.get("criterion"):
        lines.append(f"- 做到算完：{item['criterion']}")
    if item.get("quote"):
        lines += ["", "## 原话", str(item["quote"])[:1500]]
    if item.get("url"):
        lines += ["", f"## 已有交付", item["url"]]
    if history:
        lines += ["", "## 这件事之前的对话（心响记录）", history]
    (folder / "心响事项.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    sid = str(uuid4())
    prompt = ("读当前文件夹的 心响事项.md，这是本人在心响里的一件事和它之前的对话。"
              "先用两三句话说清现在到哪一步了、还差什么，然后问本人接下来要你做什么。")
    title = f"心响 #{item['n']} {item.get('title') or ''}"[:60]
    sessions.open_terminal(f"cd {shlex.quote(str(folder))} && " + shlex.join([binary, "--session-id", sid, "-n", title, prompt]))
    rec = _record("claude", sid, title, time.time(), cwd=folder, working=True, doing=prompt, app="Claude 命令行", live=True)
    with closing(_db(root)) as db, db:
        db.execute("INSERT OR REPLACE INTO local_session_items VALUES(?,?,?)", (rec["key"], item["record_id"], json.dumps(rec, ensure_ascii=False)))
    return {"opened": "terminal", "session": sid}
