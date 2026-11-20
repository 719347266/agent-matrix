"""Chats → a small pile of candidate messages. Pure code, no model.

Reads the owner's most active chats as the owner (user identity), and keeps only lines
that can be about the owner's work:
  - the owner commits to something ("我来" "明天给你" "我跟进"),
  - someone talks to the owner: anything in a private chat, or a group line that @-s or names the owner.
Whether that person actually wants something is the model's call, not a word list: "这个弹窗只展示一点"
has no ask word and is still a request. Code only drops pure small talk ("好的" "哈哈" "👍").
Everything else never leaves the machine. The model only reads the kept lines, in batches.
"""
from datetime import datetime, timedelta
import json
import re

COMMIT = re.compile(r"我来|我去|我会|我负责|我晚点|我稍后|我待会|我明天|我今天|我这边|回头(我)?(发|给|弄|看|整理)|"
                    r"我跟进|我处理|我整理|我弄|我改|我补|我出|包在我|交给我|收到[，,。！!]?我")
# Nothing but an acknowledgement, a laugh, emoji or punctuation: never a request.
SMALL_TALK = re.compile(r"^(?:好的?|好嘞|好滴|行|收到|嗯+|哦+|噢+|ok(?:ay)?|谢谢|感谢|多谢|thx|thanks|哈+|呵+|嘿+|:[\w+-]+:|[\W_])*$", re.I)
SKIP_CHAT = re.compile(r"助手|安全中心|服务台|通知|机器人|Bot|bot")
MAX_CHATS = 25


def table(state):
    """Kept for callers; the table itself is defined in db.py."""


def skipped_chat(config, name):
    """Built-in noise (bots, notices): never read."""
    return bool(SKIP_CHAT.search(name))


def muted_chat(config, name):
    """A chat the owner chose to ignore on the 系统 page: only someone @-ing the owner still gets through."""
    return any(w and w.lower() in name.lower() for w in config.chat_skip_chats)


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
    if SMALL_TALK.match(text):
        return None
    # Talking to the owner is enough; whether they want something is for the model to judge.
    return "找你的" if chat_mode == "p2p" or any(w in text for w in names) else None


def recent_lines(state, key):
    """The last lines seen in a chat, kept across polls: with a poll every minute, the line before is often
    in the previous poll, and a kept line without it ("just a link") means nothing to the model."""
    return list(state.get(key, []))[-2:]


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
        muted = muted_chat(config, cname)
        cursor_key = "chats:cursor:" + cid
        since = state.get(cursor_key) or (now - timedelta(hours=2)).isoformat()
        got = client._exec(["im", "+chat-messages-list", "--chat-id", cid, "--start", since, "--end", now.isoformat(),
                            "--order", "asc", "--no-reactions", "--as", "user", "--format", "json"])
        messages = got.get("messages") or got.get("items") or []
        history_key = "chats:recent:" + cid
        history = recent_lines(state, history_key)
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
            if kind and (not muted or kind == "@你"):
                state.db.execute("INSERT OR IGNORE INTO chat_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)", (
                    m.get("message_id"), cid, cname, chat.get("chat_mode"), sender.get("id"),
                    "我" if from_owner else (sender.get("name") or ""), m.get("create_time"), text[:500],
                    json.dumps(history[-2:], ensure_ascii=False), kind, "pending"))
                kept += 1
            history.append(f"{'我' if from_owner else sender.get('name') or '对方'}：{text[:120]}")
        state.db.commit()
        state.set(history_key, history[-2:])
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
