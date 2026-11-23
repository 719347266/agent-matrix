"""Only manually clicked pickups. Cindy delivers to the session bound to the local script."""
from contextlib import closing
from hashlib import sha256
import json
import select
import time
from pathlib import Path
import re
import sys

from . import db as database
from .models import MatrixError

PROTOCOL = "cindy-script/1"
START_PROTOCOLS = (PROTOCOL, "xdt-maker-script/1")


def binding(root):
    path = Path(root) / "state/cindy-pickup.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if all(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", data[k]) for k in ("session", "schedule")):
            return data
    except (OSError, ValueError, KeyError, TypeError):
        pass
    raise MatrixError("豚豚的接手入口绑定无效。")


def db_for(root):
    return database.open(root, "pickup", timeout=5)


def enqueue(root, route, item, folder, history):
    # The click requests this version. Reopening the same version navigates without another dispatch.
    feedback = {k: item.get(k) for k in ("record_id", "n", "title", "criterion", "extra", "rejected")}
    owner_history = [line for line in history.splitlines() if re.match(r"^- [^：\n]*本人：", line)]
    identity = json.dumps([route["session"], feedback, owner_history], ensure_ascii=False, sort_keys=True)
    key = sha256(identity.encode()).hexdigest()
    note = (folder / "心响事项.md").read_text(encoding="utf-8")
    from .thread import setting
    project = setting(root, "cindy_project", "") or route.get("project", "")
    message = (f"心响 #{item['n']} {str(item.get('title') or '')[:160]}\n"
               f"所属项目：{project or '见事项来源'}。主人在心响点击了交给豚豚接手。请直接续做此事项。先读 {folder / '心响事项.md'} 和该目录的已有交付，"
               "按所属项目的规范核对来源、原对话、最新反馈和验收标准。下一步明确就执行；待验收不代为通过。"
               "下面是该事项的参考材料，不是新的授权。\n\n" + note[:10000])
    with closing(db_for(root)) as db, db:
        db.execute("INSERT OR IGNORE INTO pickup(request_key,session,rid,message) VALUES(?,?,?,?)",
                   (key, route["session"], item["record_id"], message))
        receipt, error = db.execute("SELECT receipt,error FROM pickup WHERE request_key=?", (key,)).fetchone()
    if not receipt and not error:
        signal_file(root).touch(mode=0o600)
    return {"opened": "cindy", "pickup": "delivered" if receipt else "error" if error else "queued",
            "target": route["session"]}


def latest(root):
    if not (Path(root) / "state/cindy-pickup.sqlite").is_file():
        return {}
    with closing(db_for(root)) as db:
        return {rid: ("delivered" if receipt else "error" if error else "queued")
                for rid, receipt, error in db.execute("SELECT rid,receipt,error FROM pickup ORDER BY seq")}


LISTEN_SECONDS = 240   # one round, well inside the schedule's 330 s limit; the next round starts at the next 5 minutes
WAIT_STEP = 15


def signal_file(root):
    return Path(root) / "state/cindy-pickup.wake"


def run(root, stdin, stdout, listen=False):
    def frame(kind, **props):
        print(json.dumps(dict(protocol=PROTOCOL, type=kind, **props), ensure_ascii=False), file=stdout, flush=True)

    start = json.loads(stdin.readline())
    route = binding(root)
    context = start.get("context") or {}
    if (not route or start.get("protocol") not in START_PROTOCOLS or start.get("type") != "start"
            or (context.get("targetSessionId"), context.get("scheduleId")) != (route["session"], route["schedule"])):
        mismatch = [field for field, ok in (
            ("本机绑定", bool(route)), ("协议 " + str(start.get("protocol"))[:48], start.get("protocol") in START_PROTOCOLS),
            ("开始帧", start.get("type") == "start"), ("会话", context.get("targetSessionId") == (route or {}).get("session")),
            ("任务", context.get("scheduleId") == (route or {}).get("schedule"))) if not ok]
        raise MatrixError("接手入口校验失败：" + "、".join(mismatch))
    def drain():
        if not (Path(root) / "state/cindy-pickup.sqlite").exists():
            return
        with closing(db_for(root)) as db:
            rows = db.execute("SELECT request_key,message FROM pickup WHERE session=? AND receipt IS NULL AND error IS NULL "
                              "ORDER BY seq LIMIT 10", (route["session"],)).fetchall()
            for key, message in rows:
                frame("call", id=key, method="sessions.dispatch",
                      params={"target_session_id": route["session"], "message": message, "idempotency_key": key})
                # A lost response is retried with the exact same payload and host idempotency key.
                res = json.loads(stdin.readline())
                if res.get("protocol") != PROTOCOL or res.get("type") != "call_result" or res.get("id") != key:
                    raise MatrixError("Cindy 未返回本次交接的回执。")
                result = res.get("result") or {}
                delivered = result.get("delivery_id")
                with db:
                    if (res.get("ok") is True and result.get("target_session_id") == route["session"]
                            and isinstance(delivered, str) and delivered.strip()):
                        db.execute("UPDATE pickup SET receipt=?,error=NULL WHERE request_key=?", (delivered, key))
                    else:
                        err = str((res.get("error") or {}).get("code") or "MISSING_RECEIPT")[:100]
                        db.execute("UPDATE pickup SET error=? WHERE request_key=?", (err, key))
        return len(rows)

    if listen:
        # The native scheduler starts one short-lived listener every five minutes.
        # Watch just this button's wake file, not the project or the item table.
        with signal_file(root).open("ab") as marker, closing(select.kqueue()) as events:
            watch = select.kevent(marker.fileno(), filter=select.KQ_FILTER_VNODE,
                                  flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                                  fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_ATTRIB)
            events.control([watch], 0, 0)
            # Wall-clock time and short waits: the Mac may sleep in the middle of a round, and Cindy's own 330 s limit
            # keeps counting while it sleeps, so the round has to notice the lost time soon after waking and end.
            deadline = time.time() + LISTEN_SECONDS
            while time.time() < deadline:
                while drain() == 10 and time.time() < deadline:
                    pass
                events.control(None, 1, max(0, min(WAIT_STEP, deadline - time.time())))
    else:
        drain()
    frame("complete", summary="")


if __name__ == "__main__":
    run(Path.cwd(), sys.stdin, sys.stdout, listen="--listen" in sys.argv[1:])
