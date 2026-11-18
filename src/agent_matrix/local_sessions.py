"""Discover this user's active conversations using local runtime records. No network calls."""
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
NAMES = {"cindy": "Cindy", "codex": "Codex", "claude": "Claude"}
ISSUES = (OSError, sqlite3.Error, ValueError, TypeError, KeyError, OverflowError)


def _ro(p):
    return sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True, timeout=.2)


def _record(source, sid, title, stamp, *, sdk=None, cwd=None):
    if not isinstance(sid, str) or not ID.fullmatch(sid):
        return None
    return dict(key=f"{source}:{sid}", source=source, sid=sid, title=str(title or NAMES[source])[:100],
                stamp=float(stamp), sdk=sdk, cwd=str(cwd or ""))


def _cindy(home, now):
    base = home / "Library/Application Support/Cindy"
    for p in base.glob("cindy-*.db"):
        with closing(_ro(p)) as db:
            for sid,title,start,engine_sid in db.execute("""
                SELECT id,title,active_turn_started_at,sdk_session_id FROM sessions
                WHERE status='active' AND hidden_at IS NULL AND active_turn_started_at > COALESCE(last_turn_ended_at,0)
                  AND max(coalesce(last_activity_at,0),active_turn_started_at) BETWEEN ? AND ?
                ORDER BY active_turn_started_at DESC LIMIT 64""", (int((now-1800)*1000),int((now+30)*1000))):
                yield _record("cindy",sid,title,start/1000,sdk=engine_sid)


def _alive(pid):
    if type(pid) is not int or pid<=1:
        return False
    try:
        os.kill(pid,0)
        return True
    except OSError:
        return False


def _claude(home, now):
    for p in (home/".claude/sessions").glob("[0-9]*.json"):
        if not p.stem.isdigit() or p.stat().st_size>32768:
            continue
        try:
            obj=json.loads(p.read_bytes())
            stamp=float(obj.get("updatedAt") or obj.get("statusUpdatedAt") or 0)/1000
            if obj.get("status") not in {"busy","running","working","processing"} or not _alive(obj.get("pid")):
                continue
            if now-1800 <= stamp <= now+30:
                yield _record("claude",obj.get("sessionId"),obj.get("name"),stamp,cwd=obj.get("cwd"))
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
                if signal is True or (signal is None and sid in alive):
                    yield _record("codex",sid,title,min(now,max(float(updated or 0),path.stat().st_mtime)),cwd=cwd)
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
    return {k:v for k,v in out.items() if v["source"]=="cindy" or v["sid"] not in owned}


def _db(root):
    from .thread import _db as base
    db=base(root)
    db.execute("""CREATE TABLE IF NOT EXISTS local_session_items (
       key TEXT PRIMARY KEY, rid TEXT NOT NULL, data TEXT NOT NULL)""")
    return db


def _public(rec, working):
    return {"job":"local:"+rec["key"],"engine":{"codex":"gpt"}.get(rec["source"],rec["source"]),
            "driver":NAMES[rec["source"]],"session":rec["sid"],"title":rec["title"],
            "status":"open" if working else "recorded","can_open":True,"local":True,
            "started":datetime.fromtimestamp(rec["stamp"],TZ).strftime("%m-%d %H:%M")}


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
        result.append({**_public(r,True),"n":assigned.get(rid),"key":key})
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
    binary=(worker.claude_binary if src=="claude" else worker.codex_binary)(config)
    if not binary: raise MatrixError("没有找到 "+NAMES[src]+" 命令行。")
    cwd=Path(rec.get("cwd") or Path.home())
    if not cwd.is_dir(): raise MatrixError("原会话的工作目录已不存在。")
    args=[binary,"--resume" if src=="claude" else "resume",sid]
    if src=="codex": args=["env","CODEX_HOME="+str(Path.home()/".codex"),*args]
    sessions.open_terminal("cd "+shlex.quote(str(cwd))+" && "+shlex.join(args))
    return {"opened":"terminal"}
