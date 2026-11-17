from contextlib import contextmanager
import json
try:  # macOS / Linux
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt
from pathlib import Path
import sqlite3
from .models import MatrixError, MatrixBusy
from .timeutil import now_iso


def _try_lock(stream):
    if fcntl:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError(str(exc)) from exc


def _unlock(stream):
    if fcntl:
        fcntl.flock(stream, fcntl.LOCK_UN)
    else:
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def exclusive_lock(root: Path, wait=None):
    """Project-wide lock. `wait` seconds to queue behind another run (default: env
    AGENT_MATRIX_LOCK_WAIT or 0). Frequent checks keep 0 and simply skip a busy tick;
    scheduled jobs and model-run commands wait, so a collision never drops them."""
    import os, time
    wait = float(os.environ.get("AGENT_MATRIX_LOCK_WAIT", "0")) if wait is None else wait
    root.mkdir(parents=True, exist_ok=True)
    with (root / "matrix.lock").open("a+") as stream:
        deadline = time.monotonic() + wait
        while True:
            try:
                _try_lock(stream)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise MatrixBusy("已有实例正在执行；本次未运行。") from exc
                time.sleep(1)
        try:
            yield
        finally:
            _unlock(stream)


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS operations (key TEXT PRIMARY KEY, status TEXT NOT NULL, receipt TEXT, at TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, command TEXT, started TEXT, ended TEXT, status TEXT, error_type TEXT)")
        self.db.commit()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))
        self.db.commit()

    def operation(self, key):
        row = self.db.execute("SELECT status,receipt FROM operations WHERE key=?", (key,)).fetchone()
        return {"status": row[0], "receipt": json.loads(row[1]) if row[1] else None} if row else None

    def confirm(self, key, receipt):
        """Only called after authoritative readback proves an uncertain write applied."""
        changed = self.db.execute("UPDATE operations SET status='done',receipt=? WHERE key=?",
                                  (json.dumps(receipt, ensure_ascii=False), key)).rowcount
        if changed != 1:
            raise MatrixError("缺少待核对的操作记录，不能补记成功。")
        self.db.commit()

    def invalidate_done_receipt(self, key):
        """A proven invalid receipt is unresolved, not a reason to resend."""
        changed = self.db.execute("UPDATE operations SET status='pending' WHERE key=? AND status='done'", (key,)).rowcount
        if changed != 1:
            raise MatrixError("操作状态已变化，未修改发送回执。")
        self.db.commit()

    def once(self, key, fn):
        row = self.db.execute("SELECT status,receipt FROM operations WHERE key=?", (key,)).fetchone()
        if row:
            if row[0] == "done":
                return json.loads(row[1])
            if row[0] != "not_sent":
                raise MatrixError(f"操作 {key} 的上次结果未确认；已阻止重复写入，请先核对远端结果。")
        self.db.execute("INSERT OR REPLACE INTO operations VALUES (?, 'pending', NULL, ?)", (key, now_iso()))
        self.db.commit()
        # A timeout, error or interruption deliberately leaves pending; no blind replay.
        try:
            result = fn()
        except Exception as exc:
            if getattr(exc, "not_sent", False):
                self.db.execute("UPDATE operations SET status='not_sent' WHERE key=?", (key,))
                self.db.commit()
            raise
        self.db.execute("UPDATE operations SET status='done',receipt=? WHERE key=?", (json.dumps(result, ensure_ascii=False), key))
        self.db.commit()
        return result

    def begin(self, command):
        c = self.db.execute("INSERT INTO runs(command,started,status) VALUES (?,?,'running')", (command, now_iso()))
        self.db.commit()
        return c.lastrowid

    def finish(self, run_id, ok, error_type=None):
        self.db.execute("UPDATE runs SET ended=?,status=?,error_type=? WHERE id=?", (now_iso(), "ok" if ok else "failed", error_type, run_id))
        self.db.commit()

    def last_run(self, exclude=None):
        row = self.db.execute("SELECT command,started,ended,status,error_type FROM runs WHERE id != ? AND command != 'health' ORDER BY id DESC LIMIT 1", (exclude or -1,)).fetchone()
        return dict(zip(["command", "started", "ended", "status", "error_type"], row)) if row else None

    def pending(self):
        return [row[0] for row in self.db.execute("SELECT key FROM operations WHERE status NOT IN ('done','cancelled') ORDER BY at")]

    def close(self):
        self.db.close()
