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

from .timeutil import TZ

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
    from .monitor import _snapshot
    from .worker import is_running, read_status
    root = Path(root)
    snap = _snapshot(root)
    rows = snap.get("rows") or []
    n_of = {r.get("record_id"): _num(r.get("编号")) for r in rows}
    history = thread.history(root)
    items, threads, deliverables = [], {}, {}
    for r in rows:
        n = n_of.get(r.get("record_id"))
        if n is None:
            continue
        parent = (r.get("父事项") or [None])[0] if isinstance(r.get("父事项"), list) else r.get("父事项")
        url = _url(r.get("交付物链接"))
        item = {"n": n, "rid": r["record_id"], "title": r.get("标题") or "", "who": r.get("执行方") or "", "status": r.get("状态") or "",
                "due": _day(r.get("截止日")), "progress": r.get("进度") or "", "blocker": r.get("卡点"), "url": url,
                "summary": r.get("预做结果摘要"), "quote": _clean(r.get("原话引用")), "source": r.get("来源"),
                "source_url": _url(r.get("来源链接")), "criterion": r.get("完成标准"), "extra": r.get("补充材料"),
                "rejected": r.get("退回意见"), "parent": n_of.get(parent), "next": r.get("下一步动作"),
                "finished": _day(r.get("完成时间")), "hidden": bool(r.get("排除本人提醒"))}
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
    from . import drive
    ws, running = read_status(root), is_running(root)
    jobs = drive.open_jobs(root)

    def minutes(at):
        try:
            return max(0, int((datetime.now(TZ) - datetime.fromisoformat(at)).total_seconds() // 60))
        except (TypeError, ValueError):
            return 0
    doing = [{"n": j.get("n"), "kind": j["kind"], "title": j.get("title") or "", "driver": j.get("driver") or "AI",
              "step": j.get("step") or "", "minutes": minutes(j.get("created"))} for j in jobs]
    current = next((d for d in doing if d["kind"] == "item"), doing[0] if doing else None)
    last = ws.get("last") or None
    worker = {"running": running or bool(jobs), "current": current, "doing": doing, "drivers": drive.drivers(root),
              "queue": [n_of.get(q.get("record_id")) for q in (ws.get("queue") or []) if n_of.get(q.get("record_id"))] if running else [],
              "last": {"n": _num(last.get("n")), "title": last.get("title"), "outcome": last.get("outcome"), "text": last.get("text"),
                       "time": str(last.get("at", ""))[11:16]} if last else None}
    box = [{"id": m["id"], "time": m["time"] if m["day"] == datetime.now(TZ).date().isoformat() else m["day"][5:] + " " + m["time"],
            "title": m["title"], "text": m["summary"], "kind": m["kind"], "n": (m["refs"] or [None])[0], "read": m["read"]}
           for m in inbox.messages(root, 150)]
    judged = {n_of[rid]: j for rid, j in thread.judgments(root).items() if n_of.get(rid)}
    settings = {"auto_assign": bool(thread.setting(root, "auto_assign", False)),
                "builtin_driver": bool(thread.setting(root, "builtin_driver", True))}
    return {"demo": False, "today": datetime.now(TZ).date().isoformat(), "generated_at": datetime.now(TZ).strftime("%H:%M:%S"),
            "settings": settings, "judgments": judged,
            "guide": drive.GUIDE.format(cmd=str(drive.shim_path())),
            "snapshot_at": str(snap.get("at") or "")[11:16], "items": items, "threads": threads, "deliverables": deliverables,
            "worker": worker, "inbox": box, "system": system_info(root)}


def system_info(root):
    import sqlite3
    names = {"im.message.receive_v1": "飞书回复", "card.action.trigger": "卡片按钮", "task.task.update_user_access_v2": "飞书任务",
             "vc.meeting.participant_meeting_ended_v1": "会议结束", "vc.note.generated_v1": "纪要生成"}
    try:
        db = sqlite3.connect(f"file:{Path(root) / 'state/state.db'}?mode=ro", uri=True, timeout=5)
        status = json.loads((db.execute("SELECT value FROM kv WHERE key='listen:status'").fetchone() or ["{}"])[0])
        beat = json.loads((db.execute("SELECT value FROM kv WHERE key='listen:heartbeat'").fetchone() or ["null"])[0])
        db.close()
    except Exception:
        status, beat = {}, None
    alive = False
    try:
        alive = datetime.now(TZ) - datetime.fromisoformat(beat) < timedelta(minutes=3)
    except (TypeError, ValueError):
        pass
    return {"listener": "运行中" if alive else "没在运行", "subs": "、".join(f"{names.get(k, k)} {'✓' if v == 'live' else '未订阅'}"
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


def say(root, n, text):
    """The owner talks to an item's AI conversation: recorded as an addition, and the AI picks it up."""
    from . import thread
    from .monitor import _snapshot
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
            page = PAGE.read_text(encoding="utf-8").replace("/*__TOKEN__*/\"\"", json.dumps(self.server.token))
            return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/icon.png":  # the app icon doubles as the page logo
            icon = Path(__file__).resolve().parents[2] / "macapp/AppIcon.png"
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
            if path == "/api/settings":
                from . import thread
                if "builtin_driver" in body:
                    thread.set_setting(root, "builtin_driver", bool(body["builtin_driver"]))
                if "auto_assign" in body:
                    thread.set_setting(root, "auto_assign", bool(body["auto_assign"]))
                    if body["auto_assign"]:  # judge the waiting items right away
                        from .config import load
                        from .worker import kick
                        kick(root, load(root / "config.yaml"), force=True)
                return self._send(200, {"ok": True})
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


def start_background(root, config):
    """Called by the listener: serve the client from the always-on process. A second copy just skips."""
    try:
        from . import drive, inbox, thread
        thread.backfill(root)
        inbox.import_history(root)
        drive.install_shim(root)  # `~/.agent-matrix/bin/xinxiang`: the command any AI calls
    except Exception:
        pass
    try:
        server = make_server(root, config.app_port)
    except OSError:
        return None  # port busy: another instance already serves it
    threading.Thread(target=server.serve_forever, daemon=True, name="xinxiang").start()
    return server


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
