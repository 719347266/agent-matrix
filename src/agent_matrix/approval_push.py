"""开发模式 on the phone: a request waiting for the owner is pushed as a Feishu card and a Slack message, each with
「批准」「拒绝」buttons. Plain code, run by the listener (listen.py).

Feishu: the card button sends {"cmd": "批准 <id>"} back through card.action.trigger → reply.handle → approvals.command,
the same path as a typed「拒绝 <id> 理由」.
Slack: a Slack App in Socket Mode (bot token xoxb- to post, app-level token xapp- to receive the taps over a websocket,
so nothing on this computer has to be reachable from the internet). Setup: slack-app-manifest.yaml.
"""
import base64
import json
import os
import socket
import ssl
import struct
import time
from urllib.parse import urlparse

from . import approvals
from .models import MatrixError
from .sources import slack


def _wanted(config, channel):
    return channel in (config.approval_notify or [])


def feishu_card(entry):
    hint = f"也可以直接回复「拒绝 {entry['id']} 理由」。{approvals.OWNER_WAIT_HOURS} 小时没回，这一步就不做。"

    def button(label, verb, kind):
        return {"tag": "button", "text": {"tag": "plain_text", "content": label}, "type": kind, "size": "small",
                "behaviors": [{"type": "callback", "value": {"cmd": f"{verb} {entry['id']}"}}]}
    body = [{"tag": "div", "text": {"tag": "plain_text", "content": entry["what"]}}]
    if entry.get("why"):
        body.append({"tag": "div", "text": {"tag": "plain_text", "content": "为什么：" + entry["why"]}})
    body += [{"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px", "columns": [
                 {"tag": "column", "width": "auto", "elements": [button("✓ 批准", "批准", "primary")]},
                 {"tag": "column", "width": "auto", "elements": [button("拒绝", "拒绝", "danger")]}]},
             {"tag": "markdown", "content": hint, "text_size": "notation"}]
    return {"schema": "2.0", "config": {"summary": {"content": "开发模式等你批准：" + entry["what"][:60]}},
            "header": {"title": {"tag": "plain_text", "content": "开发模式 · 等你批准"}, "template": "orange",
                       "subtitle": {"tag": "plain_text", "content": entry.get("tool") or ""}},
            "body": {"direction": "vertical", "elements": body}}


def slack_blocks(entry, result=None):
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": "开发模式 · 等你批准"}},
              {"type": "section", "text": {"type": "plain_text", "text": entry["what"][:2900] or "（空）"}}]
    if entry.get("why"):
        blocks.append({"type": "context", "elements": [{"type": "plain_text", "text": "为什么：" + entry["why"]}]})
    if result:
        blocks.append({"type": "section", "text": {"type": "plain_text", "text": result}})
    else:
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "action_id": "approve", "value": entry["id"], "style": "primary",
             "text": {"type": "plain_text", "text": "✓ 批准"}},
            {"type": "button", "action_id": "deny", "value": entry["id"], "style": "danger",
             "text": {"type": "plain_text", "text": "拒绝"}}]})
    return json.dumps(blocks, ensure_ascii=False)


def slack_ready(config):
    return _wanted(config, "slack") and config.slack_bot_token.startswith("xoxb-") \
        and config.slack_app_token.startswith("xapp-") and bool(slack.token(config))


def slack_owner(config, state, api=slack.call):
    """The owner's Slack user id, from the user token they connected for reading chats."""
    uid = state.get("slack:owner_id")
    if not uid:
        uid = api(slack.token(config), "auth.test")["user_id"]
        state.set("slack:owner_id", uid)
    return uid


def push_slack(root, config, state, *, api=slack.post, call=slack.call):
    """Post each new waiting request to the owner's DM with the bot; turn the buttons into the outcome once it is
    decided anywhere (心响, Feishu, Slack) or expired."""
    sent = state.get("approval:slack") or {}
    waiting = {e["id"]: e for e in approvals.pending(root)}
    changed = 0
    for aid, entry in waiting.items():
        if aid in sent:
            continue
        msg = api(config.slack_bot_token, "chat.postMessage", channel=slack_owner(config, state, call),
                  text="开发模式等你批准：" + entry["what"][:100], blocks=slack_blocks(entry))
        sent[aid] = {"channel": msg["channel"], "ts": msg["ts"]}
        changed += 1
    for aid, where in sent.items():
        if aid in waiting or where.get("closed"):
            continue
        entry = approvals.get(root, aid)
        if entry:
            api(config.slack_bot_token, "chat.update", channel=where["channel"], ts=where["ts"],
                text="开发模式：" + approvals.STATUS.get(entry["status"], entry["status"]),
                blocks=slack_blocks(entry, approvals.STATUS.get(entry["status"], entry["status"]) + (f"：{entry['note']}" if entry.get("note") else "")))
        where["closed"] = True
        changed += 1
    if changed:
        state.set("approval:slack", sent)   # ponytail: grows by one small entry per approval; prune if it ever matters
    return changed


def unsent_feishu(root, config, state):
    if not _wanted(config, "feishu"):
        return []
    # a card that surely did not go out is tried again; an unconfirmed one is not (no duplicate cards)
    return [e for e in approvals.pending(root)
            if (state.operation("approval-card:" + e["id"]) or {"status": "not_sent"})["status"] == "not_sent"]


def push_feishu(config, client, state):
    from .delivery import send_once
    if not client.can_message_self or not config.owner_open_id.startswith("ou_"):
        return "飞书机器人还没绑定本人，不发卡片"
    out = 0
    for entry in unsent_feishu(client.root, config, state):
        send_once(state, "approval-card:" + entry["id"],
                  lambda: client.send_card(feishu_card(entry), "approval-" + entry["id"], channel="feishu"))
        out += 1
    return out


def on_slack_action(root, config, state, payload, *, api=slack.post, call=slack.call):
    """A tapped Slack button. Only the owner's taps count."""
    if payload.get("type") != "block_actions" or (payload.get("user") or {}).get("id") != slack_owner(config, state, call):
        return "ignored"
    for action in payload.get("actions") or []:
        verb = {"approve": "批准", "deny": "拒绝"}.get(action.get("action_id"))
        aid = str(action.get("value") or "")
        if not verb:
            continue
        answer = approvals.command(root, f"{verb} {aid}") or "没看懂这个按钮"
        entry = approvals.get(root, aid)
        channel, ts = (payload.get("channel") or {}).get("id"), (payload.get("message") or {}).get("ts")
        if entry and channel and ts:
            api(config.slack_bot_token, "chat.update", channel=channel, ts=ts, text=answer, blocks=slack_blocks(entry, answer))
            sent = state.get("approval:slack") or {}
            sent[aid] = {"channel": channel, "ts": ts, "closed": True}
            state.set("approval:slack", sent)
        return answer
    return "no_action"


# ---- 要你决定的事项: the same choices as the buttons on the 心响 page (rowButtons in xinxiang.html) -----------

# status → (what it is, [(label, command verb, style)])
CHOICES = {
    "待确认": ("新事项，要你决定谁来做", [("我来", "我来", None), ("交给 AI", "AI做", "primary"), ("不做", "不做", "danger")]),
    "卡住": ("AI 卡住了", [("按现有资料先做", "继续", "primary"), ("我自己来", "我来", None), ("不做", "不做", "danger")]),
    "待验收": ("AI 做完了，等你验收", [("通过", "过", "primary"), ("不做", "不做", "danger")]),
}


def attention(rows):
    """{record_id: row} for the items waiting on the owner's choice."""
    from .values import single
    out = {}
    for r in rows:
        try:
            status, who = single(r.get("状态")), single(r.get("执行方"))
        except MatrixError:
            continue
        if r.get("排除本人提醒") or not r.get("record_id"):
            continue
        if (status == "待确认" and who != "别人") or (status == "卡住" and who == "AI") or status == "待验收":
            out[r["record_id"]] = r
    return out


def item_blocks(row, result=None):
    from .values import draft_url, number, single
    n, status = number(row.get("编号")), single(row.get("状态"))
    what, choices = CHOICES[status] if status in CHOICES else (status, [])
    lines = [what]
    if status == "卡住" and row.get("卡点"):
        lines.append("卡在：" + str(row["卡点"])[:500])
    if status == "待验收" and row.get("预做结果摘要"):
        lines.append(str(row["预做结果摘要"])[:800])
    if status == "待确认" and row.get("原话引用"):
        lines.append("原话：" + str(row["原话引用"])[:500])
    extra = " · ".join(x for x in (row.get("来源") and "来自" + str(row["来源"]),
                                   row.get("截止日") and "截止 " + str(row["截止日"])[:10]) if x)
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": f"#{n} {row.get('标题') or ''}"[:150]}},
              {"type": "section", "text": {"type": "plain_text", "text": "\n".join(lines)[:2900]}}]
    if extra:
        blocks.append({"type": "context", "elements": [{"type": "plain_text", "text": extra}]})
    if result:
        blocks.append({"type": "section", "text": {"type": "plain_text", "text": result[:2900]}})
        return json.dumps(blocks, ensure_ascii=False)
    buttons = []
    for i, (label, verb, style) in enumerate(choices):
        b = {"type": "button", "action_id": f"item-{i}", "value": f"{row['record_id']}|{n} {verb}",
             "text": {"type": "plain_text", "text": label}}
        if style:
            b["style"] = style
        buttons.append(b)
    url = draft_url(row.get("交付物链接"))
    if status == "待验收" and url:
        buttons.append({"type": "button", "action_id": "item-open", "url": url, "text": {"type": "plain_text", "text": "打开交付物"}})
    blocks.append({"type": "actions", "elements": buttons})
    if status == "待验收":
        blocks.append({"type": "context", "elements": [{"type": "plain_text", "text": "要退回改的话，在心响里写意见。"}]})
    return json.dumps(blocks, ensure_ascii=False)


def push_items(root, config, state, rows, *, api=slack.post, call=slack.call):
    """New items waiting on the owner → one Slack DM each with the choices; once handled anywhere (心响, Slack, a reply)
    the message turns into what happened. A status change (e.g. AI done → 待验收) is a new message."""
    from .values import number, single
    pushed = state.get("attention:slack") or {}
    need = attention(rows)
    by_id = {r.get("record_id"): r for r in rows}
    changed = 0

    def close(rid, p):
        r = by_id.get(rid)
        if not p.get("closed") and r:
            now = single(r.get("状态")) if r.get("状态") else "已删除"
            api(config.slack_bot_token, "chat.update", channel=p["channel"], ts=p["ts"], text=f"已处理（现在：{now}）",
                blocks=item_blocks({**r, "状态": p["status"]}, f"✓ 已处理（现在：{now}）"))
    for rid, p in list(pushed.items()):
        if rid in need and single(need[rid].get("状态")) == p["status"]:
            continue
        close(rid, p)
        del pushed[rid]
        changed += 1
    for rid, r in need.items():
        if rid in pushed:
            continue
        status = single(r.get("状态"))
        msg = api(config.slack_bot_token, "chat.postMessage", channel=slack_owner(config, state, call),
                  text=f"{CHOICES[status][0]}：{r.get('标题') or ''}"[:200], blocks=item_blocks(r))
        pushed[rid] = {"status": status, "channel": msg["channel"], "ts": msg["ts"], "n": number(r.get("编号"))}
        changed += 1
    if changed:
        state.set("attention:slack", pushed)
    return changed


def on_item_tap(config, client, state, payload, *, handle=None, api=slack.post, call=slack.call):
    """A choice tapped in Slack: run it exactly like the same button in 心响 (a reply command), then show the outcome."""
    if handle is None:
        from .reply import handle
    if (payload.get("user") or {}).get("id") != slack_owner(config, state, call):
        return "ignored"
    action = next((a for a in payload.get("actions") or [] if str(a.get("action_id", "")).startswith("item-")
                   and "|" in str(a.get("value") or "")), None)
    if not action:
        return "no_action"
    rid, cmd = action["value"].split("|", 1)
    answer = handle(config, client, state, cmd, source="slack", message_ref=str(action.get("action_ts") or "")) or "没改成"
    channel, ts = (payload.get("channel") or {}).get("id"), (payload.get("message") or {}).get("ts")
    pushed = state.get("attention:slack") or {}
    if channel and ts and "✓" in answer:
        api(config.slack_bot_token, "chat.update", channel=channel, ts=ts, text=answer,
            blocks=json.dumps([*[b for b in (payload.get("message") or {}).get("blocks") or [] if b.get("type") != "actions"],
                               {"type": "section", "text": {"type": "plain_text", "text": answer[:2900]}}], ensure_ascii=False))
        if rid in pushed:
            pushed[rid]["closed"] = True
            state.set("attention:slack", pushed)
    return answer


def on_text(config, client, state, event, *, handle=None, api=slack.post, call=slack.call):
    """The owner typed in the bot's DM: run it like a reply in 心响 (「32 退 少了定价」「拒绝 <id> 理由」), answer there.
    In the thread of an item message the number may be left out:「退 少了定价」."""
    import re
    if handle is None:
        from .reply import handle
    text = str(event.get("text") or "").strip()
    if (event.get("user") != slack_owner(config, state, call) or event.get("bot_id") or event.get("subtype")
            or event.get("channel_type") != "im" or not text):
        return "ignored"
    key = "slack:text:" + str(event.get("client_msg_id") or event.get("ts"))
    if state.get(key):
        return "seen"   # Slack resends an event it thinks was not acknowledged
    thread = event.get("thread_ts")
    from datetime import date
    from .reply import parse
    # a command missing only its number (「退 少了定价」「过」); 「你好」is chat and goes through as typed
    if not re.match(r"#?\d", text) and not re.match(r"(批准|拒绝)\s*[0-9a-f]{12}", text) and parse(f"1 {text}", date.today())[0]:
        open_items = [p for p in (state.get("attention:slack") or {}).values() if not p.get("closed") and p.get("n")]
        n = next((p["n"] for p in open_items if p.get("ts") == thread), None) if thread else None
        if not thread and len(open_items) == 1:
            n = open_items[0]["n"]      # only one item waiting: a reply without a number is about it
        if n:
            text = f"{n} {text}"
        elif not thread and len(open_items) > 1:
            nums = "、".join(f"#{p['n']}" for p in sorted(open_items, key=lambda p: p["n"]))
            answer = f"是哪一条？在等你的有 {nums}。前面加编号，比如「{open_items[0]['n']} {text}」，或者在那条消息的串里回。"
            api(config.slack_bot_token, "chat.postMessage", channel=event.get("channel"), text=answer)
            state.set(key, True)
            return answer
    answer = handle(config, client, state, text, source="slack", message_ref=str(event.get("ts") or ""))
    answer = answer or "没看懂。可以回：32 过 / 32 退 少了定价 / 32 + 补充材料 / 32 周五 / 32 不做；在某条事项消息的串里回可以不写编号。"
    api(config.slack_bot_token, "chat.postMessage", channel=event.get("channel"), text=answer, thread_ts=thread)
    state.set(key, True)
    return answer


# ---- Slack Socket Mode: a minimal websocket client (RFC 6455, client side, text frames only) ------------------
# ponytail: stdlib instead of a websocket package; enough for Socket Mode's small JSON text messages.

def ws_connect(url, timeout=120):
    u = urlparse(url)
    raw = socket.create_connection((u.hostname, u.port or 443), timeout=timeout)
    sock = ssl.create_default_context().wrap_socket(raw, server_hostname=u.hostname)
    key = base64.b64encode(os.urandom(16)).decode()
    path = (u.path or "/") + ("?" + u.query if u.query else "")
    sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {u.hostname}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    stream = sock.makefile("rb")
    status = stream.readline()
    if b" 101 " not in status:
        raise ConnectionError("Slack websocket 握手失败：" + status.decode(errors="replace").strip())
    while stream.readline() not in (b"\r\n", b""):
        pass
    return sock, stream


def ws_send(sock, data, op=1):
    mask, n = os.urandom(4), len(data)
    head = bytes([0x80 | op]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n)
                                 if n < 65536 else bytes([0x80 | 127]) + struct.pack(">Q", n))
    sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


def _exact(stream, n):
    data = stream.read(n)
    if len(data) < n:
        raise ConnectionError("Slack websocket 断开")
    return data


def ws_recv(sock, stream):
    """The next whole text message (pings answered, fragments joined); None when the server closes."""
    parts = []
    while True:
        b1, b2 = _exact(stream, 2)
        op, n = b1 & 0x0F, b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", _exact(stream, 2))[0]
        elif n == 127:
            n = struct.unpack(">Q", _exact(stream, 8))[0]
        mask = _exact(stream, 4) if b2 & 0x80 else None
        data = _exact(stream, n)
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if op == 8:
            return None
        if op == 9:
            ws_send(sock, data, op=10)
            continue
        if op in (0, 1):
            parts.append(data)
            if b1 & 0x80:
                return b"".join(parts).decode()


def slack_loop(root, path, log, on_item=None, on_message=None):
    """Listener thread: keep one Socket Mode connection open while the Slack bot is set up; reconnect on drop.
    Approval taps are answered here; item choices go to `on_item` (the listener runs them under the project lock)."""
    from .config import load
    from .state import State
    delay = 5
    while True:
        config = load(path)
        if not slack_ready(config):
            time.sleep(60)
            continue
        try:
            url = slack.post(config.slack_app_token, "apps.connections.open")["url"]
            sock, stream = ws_connect(url)
            delay = 5
            try:
                while (text := ws_recv(sock, stream)) is not None:
                    msg = json.loads(text)
                    if msg.get("envelope_id"):
                        ws_send(sock, json.dumps({"envelope_id": msg["envelope_id"]}).encode())
                    if msg.get("type") == "disconnect":
                        break
                    payload = msg.get("payload") or {}
                    event = payload.get("event") or {}
                    if msg.get("type") == "events_api" and on_message and event.get("type") == "message":
                        on_message(event)
                    if msg.get("type") == "interactive" and on_item and any(
                            str(a.get("action_id", "")).startswith("item-") for a in payload.get("actions") or []):
                        on_item(payload)
                    elif msg.get("type") == "interactive":
                        state = State(root / "state/state.db")
                        try:
                            log("slack approval", on_slack_action(root, load(path), state, msg.get("payload") or {}))
                        finally:
                            state.close()
            finally:
                sock.close()
        except (OSError, ValueError, MatrixError, KeyError) as exc:
            log("slack approval socket", type(exc).__name__, str(exc)[:200])
            time.sleep(delay)
            delay = min(delay * 2, 300)
