"""Chats → a small pile of candidate messages. Pure code, no model.

Reads the owner's most active chats as the owner (user identity), and keeps only lines
that can be about the owner's work:
  - the owner commits to something ("我来" "明天给你" "我跟进"),
  - someone asks the owner (@ / nickname / in a private chat) with a request or a time.
Everything else never leaves the machine. The model only reads the kept lines, in batches.
"""
from datetime import datetime, timedelta
import json
import re

COMMIT = re.compile(r"我来|我去|我会|我负责|我晚点|我稍后|我待会|我明天|我今天|我这边|回头(我)?(发|给|弄|看|整理)|"
                    r"我跟进|我处理|我整理|我弄|我改|我补|我出|包在我|交给我|收到[，,。！!]?我")
ASK = re.compile(r"麻烦|帮忙|帮我|帮看|能不能|可不可以|可以.{0,10}吗|需要你|请你|辛苦|尽快|抓紧|跟进|确认一下|"
                 r"看一下|过一下|反馈|评审|审核|ddl|DDL|deadline|截止|之前(给|发|交|完成|出)|记得|别忘")
WHEN = re.compile(r"今天|今晚|明天|后天|周[一二三四五六日天]|下周|本周|这周|月底|\d{1,2}[:：点]\d{0,2}|\d{1,2}月\d{1,2}|\d{1,2}[./-]\d{1,2}")
# In a private chat most asks are casual: "借我登一下" "账号密码有吗" "加好了和我说一下".
P2P_ASK = re.compile(r"[?？]|https?://|吗|一下|借我|给我|和我说|告诉我|跟我说|好了没|怎么样了|进展")
SKIP_CHAT = re.compile(r"助手|安全中心|服务台|通知|机器人|Bot|bot")
MAX_CHATS = 25


def table(state):
    state.db.execute("""CREATE TABLE IF NOT EXISTS chat_candidates (
        message_id TEXT PRIMARY KEY, chat_id TEXT, chat_name TEXT, chat_mode TEXT, sender_id TEXT,
        sender_name TEXT, sent_at TEXT, text TEXT, context TEXT, kind TEXT, status TEXT, batch TEXT)""")
    state.db.commit()


def skipped_chat(config, name):
    """Built-in noise (bots, notices) or a chat the owner chose to ignore on the 系统 page."""
    return bool(SKIP_CHAT.search(name)) or any(w and w.lower() in name.lower() for w in config.chat_skip_chats)


def skipped_text(config, text):
    return any(w and w.lower() in text.lower() for w in config.chat_skip_words)


def owner_words(config):
    name = config.owner_name or ""
    words = {name, *config.owner_aliases}
    if len(name) >= 3:
        words.add(name[1:])
    return {w for w in words if len(w) >= 2}


def _text(message):
    body = message.get("content")
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
            body = parsed.get("text", body) if isinstance(parsed, dict) else body
        except ValueError:
            pass
    return " ".join(str(body or "").split())


def classify(text, *, from_owner, chat_mode, names, mentioned=False):
    """Which kind of candidate this line is, or None to drop it."""
    if from_owner:
        return "我的承诺" if len(text) >= 6 and COMMIT.search(text) else None
    if mentioned:  # @you in a group: always worth a look, however short ("@你 人生模拟器反馈")
        return "@你" if len(re.sub(r"@\S+", "", text).strip()) >= 2 else None
    if len(text) < 6:
        return None
    to_owner = chat_mode == "p2p" or any(w in text for w in names)
    if to_owner and (ASK.search(text) or WHEN.search(text) or (chat_mode == "p2p" and P2P_ASK.search(text))):
        return "找你的"
    return None


def mentions_owner(message, owner_open_id):
    return any(m.get("id") == owner_open_id for m in message.get("mentions") or [] if isinstance(m, dict))


def poll(config, client, state, now, *, skip_chat_ids=()):
    table(state)
    data = client._exec(["im", "+chat-list", "--as", "user", "--types", "p2p,group", "--exclude-muted",
                         "--sort", "active_time", "--page-size", str(MAX_CHATS), "--format", "json"])
    chats = (data.get("items") or data.get("chats") or []) if isinstance(data, dict) else data
    names = owner_words(config)
    exclude = set(skip_chat_ids) | set(state.get("chats:exclude", []))
    kept = scanned = 0
    for chat in chats[:MAX_CHATS]:
        cid, cname = chat.get("chat_id"), str(chat.get("name") or "")
        if not cid or cid in exclude or skipped_chat(config, cname):
            continue
        cursor_key = "chats:cursor:" + cid
        since = state.get(cursor_key) or (now - timedelta(hours=2)).isoformat()
        got = client._exec(["im", "+chat-messages-list", "--chat-id", cid, "--start", since, "--end", now.isoformat(),
                            "--order", "asc", "--no-reactions", "--as", "user", "--format", "json"])
        messages = got.get("messages") or got.get("items") or []
        history = []
        for m in messages:
            sender = m.get("sender") or {}
            text = _text(m)
            if (sender.get("sender_type") != "user" or m.get("msg_type") not in {"text", "post"} or not text
                    or skipped_text(config, text)):
                continue
            scanned += 1
            from_owner = sender.get("id") == config.owner_open_id
            kind = classify(text, from_owner=from_owner, chat_mode=chat.get("chat_mode"), names=names,
                            mentioned=not from_owner and mentions_owner(m, config.owner_open_id))
            if kind:
                state.db.execute("INSERT OR IGNORE INTO chat_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)", (
                    m.get("message_id"), cid, cname, chat.get("chat_mode"), sender.get("id"),
                    "我" if from_owner else (sender.get("name") or ""), m.get("create_time"), text[:500],
                    json.dumps(history[-2:], ensure_ascii=False), kind, "pending"))
                kept += 1
            history.append(f"{'我' if from_owner else sender.get('name') or '对方'}：{text[:120]}")
        state.db.commit()
        state.set(cursor_key, now.isoformat())
    if kept:
        from .. import activity
        pace = (f"其余 {config.chat_review_every_minutes} 分钟内统一提取" if config.chat_review_every_minutes
                else f"其余到下一个整理时间（{'、'.join(config.chat_review_times)}）统一提取")
        activity.log(client.root, "detect", f"聊天里发现 {kept} 条可能跟你有关的话：@你或带时间、催办的马上提取，{pace}")
    state.set("chats:last-poll", now.isoformat())
    return {"chats": len(chats), "messages_scanned": scanned, "candidates": kept}


def pending(state):
    table(state)
    return [dict(zip(("message_id", "chat_id", "chat_name", "chat_mode", "sender_id", "sender_name", "sent_at",
                      "text", "context", "kind"), r))
            for r in state.db.execute("SELECT message_id,chat_id,chat_name,chat_mode,sender_id,sender_name,sent_at,"
                                      "text,context,kind FROM chat_candidates WHERE status='pending' ORDER BY sent_at")]
