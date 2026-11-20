"""WeChat → the same candidate pile as Feishu chats and Slack. Pure code, no model.

Reads the owner's own WeChat on this computer through the `wechat-cli` command (npm: @canghe_ai/wechat-cli),
which decrypts the local message store. Nothing is sent anywhere: the same filter as the other chats keeps
only lines that can be about the owner's work, and only those are later shown to the model.

The command is slow (about 20 s to list chats, 30 s to read one), so reading is split in two:
  collect()  talks to the command; takes minutes; holds no lock and writes nothing
  store()    files what was read; quick; done under the project lock
A round reads the few chats that changed most recently; the rest wait for the next round.
"""
from datetime import datetime, timedelta
from hashlib import sha1
import json
import os
import re
import shutil
import subprocess

from ..models import MatrixError
from .chats import classify, owner_words, skipped_chat, skipped_text

MAX_CHATS_PER_ROUND = 4
LINE = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d)(?::\d\d)?\] (.*?): (.*)$", re.S)
NOT_TEXT = re.compile(r"^\[(图片|视频|语音|表情|动画表情|文件|链接|位置|通话|系统|名片|小程序|转账|红包|拍一拍|引用|合并转发|聊天记录)\]")
NOT_PEOPLE = {"filehelper", "weixin", "notifymessage", "fmessage", "medianote", "floatbottle", "qmessage", "newsapp", "mphelper"}
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def binary(config):
    return getattr(config, "wechat_binary", "") or shutil.which("wechat-cli")


def _run(cli, *args, timeout=150):
    """One `wechat-cli` call, its JSON answer. The command's own words are the error (never message text)."""
    try:
        done = subprocess.run([cli, *args, "--format", "json"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise MatrixError("微信命令行太久没有返回。") from None
    except OSError as exc:
        raise MatrixError(f"微信命令行启动不了（{type(exc).__name__}）。") from None
    if done.returncode != 0 or not done.stdout.strip():
        raise MatrixError((done.stderr or "微信命令行没有返回内容").strip().splitlines()[-1][:120])
    try:
        return json.loads(done.stdout)
    except ValueError:
        raise MatrixError("微信命令行返回的不是预期的格式。") from None


def cursors(state):
    """{chat id: timestamp of the last message already read}."""
    return {k[len("wechat:cursor:"):]: int(json.loads(v)) for k, v in
            state.db.execute("SELECT key, value FROM kv WHERE key LIKE 'wechat:cursor:%'")}


def _chat_ok(config, s):
    user, name = str(s.get("username") or ""), str(s.get("chat") or "")
    return bool(user and name) and not user.startswith(("gh_", "@")) and user not in NOT_PEOPLE and not skipped_chat(config, name)


def collect(config, seen, now, *, run=_run, since_hours=2, max_chats=MAX_CHATS_PER_ROUND):
    """Read what changed since `seen` ({chat id: timestamp}). Slow; no lock, no writes. Raises MatrixError when
    the command cannot list chats at all."""
    cli = binary(config)
    if not cli:
        raise MatrixError("这台电脑上没有装微信命令行（wechat-cli）。")
    floor = int((now - timedelta(hours=since_hours)).timestamp())
    sessions = run(cli, "sessions", "--limit", "40")
    changed = [(s, seen.get(s["username"], floor)) for s in sessions if isinstance(s, dict) and _chat_ok(config, s)]
    changed = sorted((x for x in changed if int(x[0].get("timestamp") or 0) > x[1]), key=lambda x: -int(x[0]["timestamp"]))
    chats, behind = [], 0
    for s, since in changed[:max_chats]:
        start = datetime.fromtimestamp(since, now.tzinfo).strftime("%Y-%m-%d %H:%M")
        try:
            lines = run(cli, "history", s["username"], "--start-time", start, "--limit", "80").get("messages") or []
        except MatrixError:
            lines = []   # a chat the command cannot open: do not ask again for the same message
        if not lines:
            # The chat has a new message but its records cannot be read (the command's keys are older than WeChat's
            # newest message store). The chat list still carries the last message: better that one than none.
            behind += 1
            lines = _last_line(s, now)
        chats.append({"id": s["username"], "name": s["chat"], "group": bool(s.get("is_group")), "upto": int(s["timestamp"]), "lines": lines})
    return {"chats": chats, "waiting": max(0, len(changed) - len(chats)), "listed": len(sessions), "behind": behind}


def _last_line(s, now):
    """The chat list's last message as one history line; [] when it is not text or it is not clear who said it."""
    text = " ".join(str(s.get("last_message") or "").split())
    if not text or s.get("msg_type") not in ("文本", "text"):
        return []
    # a private chat names no sender: unread means the other person wrote it; read may be either side, so leave it
    sender = str(s.get("sender") or "").strip() or (s["chat"] if not s.get("is_group") and int(s.get("unread") or 0) > 0 else "")
    if not sender:
        return []
    return [f"[{datetime.fromtimestamp(int(s['timestamp']), now.tzinfo).strftime('%Y-%m-%d %H:%M')}] {sender}: {text}"]


def store(config, root, state, now, read):
    """File what collect() read: candidates into chat_candidates, cursors forward. Quick; call under the lock."""
    names = owner_words(config)
    me = state.get("wechat:me") or ""
    kept = scanned = 0
    for chat in read["chats"]:
        mode, cname = ("group" if chat["group"] else "p2p"), "微信 · " + chat["name"]
        history = []
        for raw in chat["lines"]:
            found = LINE.match(str(raw))
            if not found:
                continue
            sent, sender, text = found.group(1), found.group(2).strip(), " ".join(found.group(3).split())
            if mode == "p2p" and sender and sender != chat["name"] and not me:
                me = sender   # in a private chat whoever is not the other person is the owner: that is their WeChat name
                state.set("wechat:me", me)
            from_owner = bool(sender) and (sender == me or (mode == "p2p" and sender != chat["name"]))
            if not text or NOT_TEXT.match(text) or skipped_text(config, text):
                continue
            scanned += 1
            mine = names | ({me} if len(me) >= 2 else set())
            kind = classify(text, from_owner=from_owner, chat_mode=mode, names=mine,
                            mentioned=not from_owner and any("@" + w in text for w in mine))
            if kind:
                mid = "wechat:" + sha1(f"{chat['id']}|{sent}|{sender}|{text}".encode("utf-8")).hexdigest()[:24]
                state.db.execute("INSERT OR IGNORE INTO chat_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)", (
                    mid, chat["id"], cname, mode, sender, "我" if from_owner else sender, sent, text[:500],
                    json.dumps(history[-2:], ensure_ascii=False), kind, "pending"))
                kept += state.db.execute("SELECT changes()").fetchone()[0]
            history.append(f"{'我' if from_owner else sender}：{text[:120]}")
        state.set("wechat:cursor:" + chat["id"], chat["upto"])
    state.db.commit()
    if kept:
        from .. import activity
        activity.log(root, "detect", f"微信里发现 {kept} 条可能跟你有关的话，很快统一提取")
    state.set("wechat:last-poll", now.isoformat())
    out = {"chats": len(read["chats"]), "messages_scanned": scanned, "candidates": kept, "waiting": read.get("waiting", 0)}
    # behind: chats whose records could not be read this round, so only their last message was looked at
    behind = bool(read["chats"]) and read.get("behind", 0) == len(read["chats"])
    was = (state.get("wechat:status") or {}).get("behind", False)
    state.set("wechat:status", {"ok": True, "at": now.strftime("%m-%d %H:%M"), "behind": behind if read["chats"] else was, **out})
    return out


def check(config, *, run=_run):
    """Can WeChat be read right now? {"ok", "chats" | "error"}: for the settings page and the setup check."""
    cli = binary(config)
    if not cli:
        return {"ok": False, "installed": False, "error": "这台电脑上没有装微信命令行（wechat-cli）。"}
    try:
        sessions = run(cli, "sessions", "--limit", "5", timeout=90)
        return {"ok": True, "installed": True, "chats": len(sessions)}
    except MatrixError as exc:
        return {"ok": False, "installed": True, "error": str(exc)}


def dry_run(config, *, hours=24, run=_run, max_chats=MAX_CHATS_PER_ROUND):
    """Read WeChat, return what the filter keeps. Throwaway state: no cursor moves, nothing is written."""
    import tempfile
    from pathlib import Path
    from ..state import State
    from ..timeutil import TZ
    from .chats import pending
    tmp = Path(tempfile.mkdtemp())
    state = State(tmp / "state.db")
    try:
        now = datetime.now(TZ)
        out = store(config, tmp, state, now, collect(config, {}, now, run=run, since_hours=hours, max_chats=max_chats))
        return {**out, "lines": pending(state)}
    finally:
        state.close()


def main(argv=None):
    """Dry run: read WeChat, print how much the filter keeps (not the messages). Writes nothing real.

    python -m agent_matrix.sources.wechat --hours 24
    """
    import argparse
    from pathlib import Path
    from ..config import load
    p = argparse.ArgumentParser(description="微信试跑：只读，不写表")
    p.add_argument("--hours", type=float, default=24, help="往回看多少小时")
    p.add_argument("--config", type=Path, default=Path("config.yaml"))
    p.add_argument("--show", action="store_true", help="把筛中的话也打印出来")
    args = p.parse_args(argv)
    try:
        out = dry_run(load(args.config), hours=args.hours)
    except MatrixError as exc:
        print(exc)
        return 1
    print(f"看了 {out['chats']} 个聊天、{out['messages_scanned']} 条消息，筛中 {out['candidates']} 条；还有 {out['waiting']} 个聊天等下一轮。")
    if args.show:
        for m in out["lines"]:
            print(f"[{m['kind']}] {m['chat_name']} {m['sent_at']} {m['sender_name']}：{m['text']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
