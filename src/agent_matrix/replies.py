"""Replies the AI drafted for the person who asked. Nothing is sent on its own: the owner reads the draft
in 心响, may edit it, and presses 发送; only then does 心响 post it, as the owner, under the original message.

One JSON file per item under state/replies/ (the worker writes drafts, the app server sends), each side
replacing the whole file. The AI itself cannot send: its lark-cli send commands are refused (engines.LARK_DENY).

Where it goes is read from the item's 去重键, which carries the chat message it came from:
    chat:om_xxx:<hash>                                    Feishu / Lark  → im +messages-reply
    chat:https://x.slack.com/archives/C1/p…[?thread_ts=…]:<hash>  Slack → chat.postMessage (in that thread)
Anything else (meetings, tasks, WeChat) has no place to send to: the draft is shown for copying only.

A second, separate draft per item is a group notice (结果.json "notify": {"channel": "C…", "text": …}), e.g. telling
reviewers in a Slack channel that a PR is up. Same rule: kept as a draft, posted to that channel only on 发送.
Stored as <record_id>.notify.json next to the reply."""
import json
import os
import re
from datetime import datetime
from pathlib import Path

from .models import MatrixError
from .timeutil import TZ

MAX_TEXT = 4000


NOTICE = "notify"
SLACK_CHANNEL = re.compile(r"[CG][A-Z0-9]{6,}")


def _path(root, record_id, slot=""):
    if not record_id or not re.fullmatch(r"[A-Za-z0-9_-]+", str(record_id)):
        raise MatrixError("事项编号不对。")
    folder = Path(root) / "state/replies"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{record_id}{'.' + slot if slot else ''}.json"


def get(root, record_id, slot=""):
    try:
        return json.loads(_path(root, record_id, slot).read_text(encoding="utf-8"))
    except (OSError, ValueError, MatrixError):
        return None


def _write(root, record_id, entry, slot=""):
    path = _path(root, record_id, slot)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _now():
    return datetime.now(TZ).isoformat(timespec="seconds")


def save(root, record_id, text):
    """A new draft from the AI. A reply already sent stays sent: a later version does not reopen it."""
    text = str(text or "").strip()[:MAX_TEXT]
    if not text or (get(root, record_id) or {}).get("status") == "sent":
        return None
    entry = {"text": text, "status": "draft", "at": _now()}
    _write(root, record_id, entry)
    return entry


def discard(root, record_id, slot=""):
    entry = get(root, record_id, slot)
    if not entry or entry["status"] != "draft":
        return False
    _write(root, record_id, {**entry, "status": "dropped", "decided_at": _now()}, slot)
    return True


def save_notice(root, record_id, notice):
    """A group notice the AI drafted (结果.json "notify"). Only a Slack channel id is accepted as the place;
    anything else is ignored. A notice already sent stays sent."""
    notice = notice if isinstance(notice, dict) else {}
    channel = str(notice.get("channel") or "").strip()
    text = str(notice.get("text") or "").strip()[:MAX_TEXT]
    if not text or not SLACK_CHANNEL.fullmatch(channel) or (get(root, record_id, NOTICE) or {}).get("status") == "sent":
        return None
    entry = {"text": text, "channel": channel, "label": str(notice.get("label") or "").strip()[:40],
             "status": "draft", "at": _now()}
    _write(root, record_id, entry, NOTICE)
    return entry


def send_notice(root, config, record_id, text, *, slack_post=None):
    """The owner pressed 发送 on a group notice: post it to that Slack channel (not in a thread) and mark it sent."""
    entry = get(root, record_id, NOTICE)
    if not entry or entry["status"] != "draft":
        raise MatrixError("这条群通知已经发过或不发了。")
    text = str(text or "").strip()[:MAX_TEXT]
    if not text:
        raise MatrixError("群通知是空的。")
    from .sources import slack
    tok = slack.token(config)
    if not tok:
        raise MatrixError("Slack 没连接。")
    sent = (slack_post or slack.post)(tok, "chat.postMessage", channel=entry["channel"], text=text)
    _write(root, record_id, {**entry, "text": text, "status": "sent", "sent_at": _now(), "ref": sent.get("ts")}, NOTICE)
    from . import activity, thread
    where = entry.get("label") or entry["channel"]
    thread.add(root, record_id, "me", "say", f"已在 {where} 发群通知：" + text[:1000])
    activity.log(root, "send", f"群通知已发到 {where}：" + text[:30], status="ok")
    return {"ok": True, "where": "slack"}


SLACK_LINK = r"https://[^/\s\])]+\.slack\.com/archives/([A-Z0-9]+)/p(\d{10})(\d{6})(?:\?thread_ts=(\d+\.\d+))?"


def target(row):
    """Where a reply to this item goes, or None. Read from the 去重键; an item without one (made by hand or from
    another store) falls back to a Slack message link in its 来源链接."""
    key = str((row or {}).get("去重键") or "")
    if key.startswith("chat:"):
        message = key[len("chat:"):].rsplit(":", 1)[0]
    elif not key:
        found = re.search(SLACK_LINK, str((row or {}).get("来源链接") or ""))
        if not found:
            return None
        message = found.group(0)
    else:
        return None
    if re.fullmatch(r"om_[A-Za-z0-9_-]+", message):
        return {"kind": "lark", "message_id": message}
    found = re.match(SLACK_LINK, message)
    if found:
        channel, ts = found.group(1), f"{found.group(2)}.{found.group(3)}"
        # A DM gets a plain answer; in a channel the answer goes under the message, in its thread.
        thread = found.group(4) or (None if channel.startswith("D") else ts)
        return {"kind": "slack", "channel": channel, "thread_ts": thread}
    return None


def where(t):
    return {"lark": "Lark / 飞书里回复原消息", "slack": "Slack 里回复原消息"}.get((t or {}).get("kind"))


def send(root, config, row, text, *, slack_post=None, lark=None):
    """The owner pressed 发送. Posts `text` (the owner may have edited the draft) and marks it sent."""
    record_id = row["record_id"]
    entry = get(root, record_id)
    if not entry or entry["status"] != "draft":
        raise MatrixError("这条回复已经发过或不发了。")
    text = str(text or "").strip()[:MAX_TEXT]
    if not text:
        raise MatrixError("回复是空的。")
    t = target(row)
    if not t:
        raise MatrixError("这件事不是从 Lark / 飞书或 Slack 的消息来的，没法直接回复；复制过去自己发吧。")
    if t["kind"] == "slack":
        from .sources import slack
        tok = slack.token(config)
        if not tok:
            raise MatrixError("Slack 没连接。")
        sent = (slack_post or slack.post)(tok, "chat.postMessage", channel=t["channel"], text=text, thread_ts=t["thread_ts"])
        ref = sent.get("ts")
    else:
        from .feishu import Feishu
        client = lark or Feishu(config, root)
        # --content, not --text: a reply starting with "@" would make --text read a file of that name.
        sent = client._exec(["im", "+messages-reply", "--message-id", t["message_id"], "--content",
                             json.dumps({"text": text}, ensure_ascii=False), "--msg-type", "text",
                             "--idempotency-key", f"reply-{record_id}"[:50], "--as", "user", "--format", "json"])
        ref = (sent or {}).get("message_id")
    _write(root, record_id, {**entry, "text": text, "status": "sent", "sent_at": _now(), "ref": ref})
    from . import activity, thread
    thread.add(root, record_id, "me", "say", "已回复对方：" + text[:1000])
    activity.log(root, "send", "回复已发出：" + text[:30], status="ok")
    return {"ok": True, "where": t["kind"]}
