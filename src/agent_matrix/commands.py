"""Durable local button requests. Acknowledgement never waits for Feishu.

One command runs at a time. Transport retries keep the same request ID; uncertain
writes are reconciled by Gateway instead of being replayed. Optional document
search runs after the verified completion and outside the project lock.
"""
from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
from uuid import uuid4

from .models import MatrixBusy, MatrixError
from .timeutil import TZ

ACTIVE = {"queued", "running"}


class Commands:
    def __init__(self, root, execute=None):
        self.root = Path(root)
        self.path = self.root / "state/app-commands.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.execute = execute or run_action
        self.guard = threading.Lock()
        self.working = False
        with self.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS commands (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, numbers TEXT NOT NULL,
                status TEXT NOT NULL, message TEXT NOT NULL, created REAL NOT NULL,
                updated REAL NOT NULL, dismissed INTEGER NOT NULL DEFAULT 0)""")
            db.execute("UPDATE commands SET status='failed', message=?, updated=? WHERE status='running'",
                       ("服务重启了，结果需要核对；点「核对并重试」会先检查上次是否已保存。", time.time()))
        self.start()

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def public(row):
        return {"id": row["id"], "numbers": json.loads(row["numbers"]), "status": row["status"],
                "message": row["message"], "created": row["created"], "updated": row["updated"],
                "text": json.loads(row["payload"])["text"]}

    def recent(self):
        with self.db() as db:
            rows = db.execute("""SELECT * FROM commands WHERE dismissed=0
                ORDER BY created DESC LIMIT 100""").fetchall()
        return [self.public(r) for r in rows]

    def submit(self, body):
        from .reply import parse
        text = re.sub(r"\s+", " ", str(body.get("text") or "")).strip()[:1000]
        if not text:
            raise ValueError("没有内容。")
        kind = body.get("kind", "cmd")
        if kind not in {"cmd", "say"}:
            raise ValueError("不支持这个操作。")
        n = int(body.get("n") or 0) if kind == "say" else None
        if kind == "say":
            if n < 1:
                raise ValueError("缺少事项编号。")
            numbers = [n]
        else:
            ops, problems = parse(text, datetime.now(TZ).date())
            if not ops or problems:
                raise ValueError("；".join(problems) or "没看懂，请输入编号和动作，例如：44 完成")
            numbers = sorted({int(n) for op in ops for n in op.numbers})
        request_id = str(body.get("request_id") or uuid4().hex)
        if not re.fullmatch(r"[a-zA-Z0-9_-]{8,80}", request_id):
            raise ValueError("请求编号无效。")
        payload = json.dumps({"kind": kind, "text": text, "n": n}, ensure_ascii=False, sort_keys=True)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM commands WHERE id=?", (request_id,)).fetchone()
            if old:
                if old["payload"] != payload:
                    raise ValueError("请求编号已用于另一条操作。")
                result = self.public(old)
            else:
                active = db.execute("SELECT * FROM commands WHERE status IN ('queued','running')").fetchall()
                same = next((r for r in active if set(numbers) & set(json.loads(r["numbers"]))), None)
                if same:
                    result = self.public(same)
                else:
                    now = time.time()
                    db.execute("INSERT INTO commands VALUES (?,?,?,?,?,?,?,0)",
                               (request_id, payload, json.dumps(numbers), "queued", "已收到，等待处理", now, now))
                    result = self.public(db.execute("SELECT * FROM commands WHERE id=?", (request_id,)).fetchone())
        self.start()
        return result

    def retry(self, request_id):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM commands WHERE id=?", (request_id,)).fetchone()
            if not row:
                raise ValueError("找不到上次操作。")
            if row["status"] == "failed":
                for other in db.execute("SELECT * FROM commands WHERE status IN ('queued','running')"):
                    if set(json.loads(row["numbers"])) & set(json.loads(other["numbers"])):
                        return self.public(other)
                db.execute("UPDATE commands SET status='queued',message=?,updated=?,dismissed=0 WHERE id=?",
                           ("已收到，先核对上次操作", time.time(), request_id))
            result = self.public(db.execute("SELECT * FROM commands WHERE id=?", (request_id,)).fetchone())
        self.start()
        return result

    def dismiss(self, request_id):
        with self.db() as db:
            db.execute("UPDATE commands SET dismissed=1 WHERE id=? AND status IN ('done','failed')", (request_id,))

    def update(self, request_id, status, message):
        with self.db() as db:
            db.execute("UPDATE commands SET status=?,message=?,updated=? WHERE id=?",
                       (status, str(message)[:2000], time.time(), request_id))

    def start(self):
        with self.guard:
            if self.working:
                return
            self.working = True
        threading.Thread(target=self.drain, daemon=True, name="xinxiang-buttons").start()

    def drain(self):
        while True:
            # Sharing the guard with start prevents losing a request at the idle boundary.
            with self.guard:
                with self.db() as db:
                    row = db.execute("SELECT * FROM commands WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
                if not row:
                    self.working = False
                    return
                self.update(row["id"], "running", "正在保存")
            try:
                result = self.execute(self.root, json.loads(row["payload"]), row["id"],
                                      lambda message: self.update(row["id"], "running", message))
                self.update(row["id"], "done" if result["ok"] else "failed", result["reply"])
                if result.get("deferred"):
                    threading.Thread(target=enrich_deliverables,
                                     args=(self.root, result["deferred"], row["id"]),
                                     daemon=True, name="xinxiang-delivery-search").start()
            except MatrixBusy:
                self.update(row["id"], "failed", "后台操作还没结束，本次尚未执行。可以重试。")
            except Exception as exc:
                message = str(exc) if isinstance(exc, MatrixError) else f"处理遇到问题（{type(exc).__name__}）"
                self.update(row["id"], "failed", message + " 点「核对并重试」会先检查上次是否已保存。")


def run_action(root, payload, request_id, progress):
    from .bitable import require_owned
    from .config import load
    from .feishu import Feishu
    from .monitor import _snapshot
    from .reply import Applier, parse, WAKE_WORKER
    from .state import State, exclusive_lock
    from . import activity, thread

    from contextlib import nullcontext
    from .itemstore import enabled
    config = load(root / "config.yaml")
    client = Feishu(config, root)
    local = enabled(config)  # items on this computer: saved at once, Feishu gets it in the background
    if not local:
        progress("正在连接飞书")
        client.verify_owner(client.auth())
        progress("正在等待其他后台操作结束")
    with nullcontext() if local else exclusive_lock(root / "state", wait=45):
        state = State(root / "state/state.db")
        try:
            require_owned(config, state)
            text = payload["text"]
            if payload["kind"] == "say":
                row = next((r for r in _snapshot(root).get("rows", []) if int(r.get("编号") or 0) == payload["n"]), None)
                status = (row.get("状态") or [None])[0] if row and isinstance(row.get("状态"), list) else (row or {}).get("状态")
                if row and status == "取消":
                    key = "app-say:" + request_id
                    if not state.get(key):
                        thread.add(root, row["record_id"], "me", "feedback", text)
                        state.set(key, True)
                    return {"ok": True, "reply": f"#{payload['n']} 已记进对话；这件事标为不做，要接着做先点「恢复」"}
                # On a finished item, what the owner says reopens it: the AI picks up the same conversation.
                text = f"{payload['n']} 改 {text}" if status == "完成" else f"{payload['n']} + {text}"
            ops, problems = parse(text, datetime.now(TZ).date())
            applier = Applier(config, client, state, source="cindy", message_ref="app-" + request_id,
                              immediate=True, progress=progress)
            progress("正在读取事项")
            lines = [line for op in ops for line in applier.apply(op)]
            for line in lines:
                activity.log(root, "reply", f"你回复「{text[:20]}」→ {line[:60]}", status="ok" if "✓" in line else "info")
            if any(op.verb in WAKE_WORKER for op in ops) and any("✓" in line for line in lines):
                from .worker import kick
                kick(root, config, force=True)
            return {"ok": bool(lines) and all("✓" in line for line in lines) and not problems,
                    "reply": "\n".join([*lines, *problems]), "deferred": applier.deferred}
        finally:
            state.close()


def enrich_deliverables(root, rows, request_id):
    """Best-effort attachment lookup never blocks the button or other commands."""
    from .config import load
    from .feishu import Feishu
    from .reply import find_deliverable
    from .brief_migration import single
    from .prework import _draft_url
    from .workflow import Gateway, normalize
    from .state import State, exclusive_lock
    from .monitor import save_snapshot
    from . import thread, activity

    try:
        config = load(root / "config.yaml")
        client = Feishu(config, root)
        client.verify_owner(client.auth())
        for original in rows:
            found = find_deliverable(client, config, original.get("标题"), datetime.now(TZ))
            if not found:
                continue
            with exclusive_lock(root / "state", wait=15):
                state = State(root / "state/state.db")
                try:
                    gateway = Gateway(client, state, on_rows=lambda rows: save_snapshot(root, rows))
                    fresh = next((r for r in gateway.rows() if r["record_id"] == original["record_id"]), None)
                    if (not fresh or single(fresh.get("状态")) != "完成"
                        or single(fresh.get("交付情况")) != "待补交付" or _draft_url(fresh.get("交付物链接"))
                        or normalize("完成时间", fresh.get("完成时间")) != normalize("完成时间", original.get("完成时间"))
                        or fresh.get("补充材料") != original.get("补充材料")):
                        continue
                    if found.get("candidates"):
                        options = "；".join(f"《{c['title']}》{c['url']}" for c in found["candidates"])
                        thread.add(root, fresh["record_id"], "system", "say", "已完成。交付文件可能是：" + options + "。可补充正确链接。")
                    else:
                        gateway.update(fresh, {"交付物链接": found["url"], "交付情况": ["有交付"]},
                                       f"app-delivery:{request_id}:{fresh['record_id']}", "app:delivery-search")
                        thread.add(root, fresh["record_id"], "system", "say", f"已找到并挂上交付文件：《{found['title']}》{found['url']}")
                finally:
                    state.close()
    except Exception as exc:
        activity.log(root, "error", "后台查找交付文件未完成，已保存的完成状态不受影响", detail=type(exc).__name__)
