"""Chats → a small pile of candidate messages. Pure code, no model.

Reads every chat the owner is in, as the owner (user identity), through one cross-chat search per poll. Every line the owner's account can see goes
to the model, which decides who it is for and whether it is work for the owner (like 数字分身's intake):
code does not guess from names or ask words, which lose too much ("你们帮忙升级一下" right after the owner
spoke names nobody). The kind only tells the model what code already knows: 我说的 / @你 / 找你的 (a private
chat, or the owner named) / 回你的 (a reply to the owner's post) / 全员 (@all) / 群聊 (anything else in a group).
Code drops only pure small talk ("好的" "哈哈" "👍"), and in a chat the owner muted keeps nothing but @你.
"""
from datetime import datetime, timedelta
import json
import re

# Nothing but an acknowledgement, a laugh, emoji or punctuation: never a request.
SMALL_TALK = re.compile(r"^(?:好的?|好嘞|好滴|行|收到|嗯+|哦+|噢+|ok(?:ay)?|谢谢|感谢|多谢|thx|thanks|哈+|呵+|嘿+|:[\w+-]+:|[\W_])*$", re.I)
SKIP_CHAT = re.compile(r"助手|安全中心|服务台|通知|机器人|Bot|bot")
MAX_CHATS = 25   # Slack: conversations read per poll
SEEN_KEEP = 2000


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


def classify(text, *, from_owner, chat_mode, names, mentioned=False, replied=False, broadcast=False):
    """Which kind of candidate this line is, or None to drop it. A muted chat keeps only "@你"."""
    if from_owner:
        return None if SMALL_TALK.match(text) else "我说的"
    if mentioned:  # @you in a group: always worth a look, however short ("@你 人生模拟器反馈")
        return "@你" if len(re.sub(r"@\S+", "", text).strip()) >= 2 else None
    if SMALL_TALK.match(text):
        return None
    # Whether anyone wants something of the owner is for the model to judge; the kind is just what code knows.
    if chat_mode == "p2p" or any(w in text for w in names):
        return "找你的"
    return "回你的" if replied else "全员" if broadcast else "群聊"


MINE_KEEP = 300


def remember_mine(state, key, message_id):
    """The owner's own recent messages, so a reply to one of them is known to be for the owner.
    ponytail: only what was seen since this started; a thread the owner opened before that is not known."""
    state.set(key, [*state.get(key, []), message_id][-MINE_KEEP:])


def recent_lines(state, key):
    """The last lines seen in a chat, kept across polls: with a poll every minute, the line before is often
    in the previous poll, and a kept line without it ("just a link") means nothing to the model."""
    return list(state.get(key, []))[-2:]


def mentions_owner(message, owner_open_id):
    return any(m.get("id") == owner_open_id for m in message.get("mentions") or [] if isinstance(m, dict))


def mentions_all(message):
    return any(m.get("id") in ("all", "@_all") or m.get("key") == "@_all"
               for m in message.get("mentions") or [] if isinstance(m, dict)) or "@_all" in str(message.get("content") or "")


def poll(config, client, state, now, *, skip_chat_ids=()):
    """One cross-chat search per poll (plus a message fetch when anything is new) instead of a call per chat:
    Lark's API quota is per company (10000 a month on the standard plan) and shared with every other app."""
    table(state)
    since = state.get("chats:cursor") or (now - timedelta(hours=2)).isoformat()
    # The search index lags a little: look back a few minutes more each time; seen ids drop the repeats.
    # whole seconds: search passes the times to the API as they are, and it refuses fractions (99992402)
    start = min(datetime.fromisoformat(since), now - timedelta(minutes=10)).isoformat(timespec="seconds")
    got = client._exec(["im", "+messages-search", "--query", "", "--sender-type", "user", "--start", start,
                        "--end", now.isoformat(timespec="seconds"), "--page-size", "50", "--page-limit", "4", "--no-reactions",
                        "--as", "user", "--format", "json"])
    messages = (got.get("messages") or got.get("items") or []) if isinstance(got, dict) else got or []
    names = owner_words(config)
    exclude = set(skip_chat_ids) | set(state.get("chats:exclude", []))
    seen = list(state.get("chats:seen", []))
    seen_set, mine = set(seen), set(state.get("chats:mine", []))
    kept = scanned = 0
    chats = set()
    for m in sorted(messages, key=lambda m: str(m.get("create_time") or "")):
        mid, cid, cname = m.get("message_id"), m.get("chat_id"), str(m.get("chat_name") or "")
        if not mid or mid in seen_set or not cid or cid in exclude or skipped_chat(config, cname):
            continue
        seen_set.add(mid)
        seen.append(mid)
        chats.add(cid)
        sender = m.get("sender") or {}
        text = _text(m)
        if (sender.get("sender_type") not in (None, "user") or m.get("msg_type") not in {"text", "post"} or not text
                or skipped_text(config, text)):
            continue
        scanned += 1
        chat_mode = m.get("chat_type") or m.get("chat_mode")
        history_key = "chats:recent:" + cid
        history = recent_lines(state, history_key)
        from_owner = sender.get("id") == config.owner_open_id
        if from_owner:
            remember_mine(state, "chats:mine", mid)
            mine.add(mid)
        kind = classify(text, from_owner=from_owner, chat_mode=chat_mode, names=names,
                        mentioned=not from_owner and mentions_owner(m, config.owner_open_id),
                        replied=bool({m.get("root_id"), m.get("parent_id")} & mine - {None}),
                        broadcast=mentions_all(m))
        if kind and (not muted_chat(config, cname) or kind == "@你"):
            state.db.execute("INSERT OR IGNORE INTO chat_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)", (
                mid, cid, cname, chat_mode, sender.get("id"),
                "我" if from_owner else (sender.get("name") or ""), m.get("create_time"), text[:500],
                json.dumps(history[-2:], ensure_ascii=False), kind, "pending"))
            kept += 1
        history.append(f"{'我' if from_owner else sender.get('name') or '对方'}：{text[:120]}")
        state.set(history_key, history[-2:])
    state.db.commit()
    state.set("chats:seen", seen[-SEEN_KEEP:])
    state.set("chats:cursor", now.isoformat())
    if kept:
        from .. import activity
        pace = (f"其余 {config.chat_review_every_minutes} 分钟内统一提取" if config.chat_review_every_minutes
                else f"其余到下一个整理时间（{'、'.join(config.chat_review_times)}）统一提取")
        activity.log(client.root, "detect", f"聊天里发现 {kept} 条可能跟你有关的话：@你或带时间、催办的马上提取，{pace}")
    state.set("chats:last-poll", now.isoformat())
    return {"chats": len(chats), "messages_scanned": scanned, "candidates": kept, "via": "search"}


def pending(state):
    table(state)
    return [dict(zip(("message_id", "chat_id", "chat_name", "chat_mode", "sender_id", "sender_name", "sent_at",
                      "text", "context", "kind"), r))
            for r in state.db.execute("SELECT message_id,chat_id,chat_name,chat_mode,sender_id,sender_name,sent_at,"
                                      "text,context,kind FROM chat_candidates WHERE status='pending' ORDER BY sent_at")]
