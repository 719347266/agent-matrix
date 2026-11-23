"""Chats → a small pile of candidate messages. Pure code, no model.

Reads the owner's most active chats as the owner (user identity), and keeps only lines
that can be about the owner's work:
  - the owner commits to something ("我来" "明天给你" "我跟进"),
  - someone talks to the owner: anything in a private chat, or a group line that @-s or names the owner,
  - a reply to something the owner posted, or an @all / @here notice (the owner is one of "all").
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
POLL_MINUTES = 30
HISTORY_PAGES = 5
SEARCH_GROUP = 10


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
        return "我的承诺" if len(text) >= 6 and COMMIT.search(text) else None
    if mentioned:  # @you in a group: always worth a look, however short ("@你 人生模拟器反馈")
        return "@你" if len(re.sub(r"@\S+", "", text).strip()) >= 2 else None
    if SMALL_TALK.match(text):
        return None
    # Talking to the owner is enough; whether they want something is for the model to judge.
    if chat_mode == "p2p" or any(w in text for w in names):
        return "找你的"
    return "回你的" if replied else "全员" if broadcast else None


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


def _stamp(value):
    """A saved read position as Feishu accepts it: whole seconds. Older code saved positions with microseconds
    (…T12:27:43.379881+08:00), which the message search rejects as invalid (99992402) and the whole round fails."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).isoformat(timespec="seconds")
    except ValueError:
        return None


def poll(config, client, state, now, *, skip_chat_ids=()):
    """Persist the next attempt so a restart or API failure cannot cause a burst."""
    deadline = state.get("chats:next-poll")
    if deadline and now < datetime.fromisoformat(deadline):
        return {"deferred_until": deadline}
    failures = state.get("chats:poll-failures", 0)
    state.set("chats:next-poll", (now + timedelta(minutes=POLL_MINUTES)).isoformat())
    try:
        result = _poll(config, client, state, now, skip_chat_ids)
    except Exception:
        delay = min(360, POLL_MINUTES * 2 ** min(failures + 1, 4))
        state.set("chats:next-poll", (now + timedelta(minutes=delay)).isoformat())
        state.set("chats:poll-failures", failures + 1)
        raise
    state.set("chats:poll-failures", 0)
    state.set("chats:last-poll", now.isoformat())
    return result


def _poll(config, client, state, now, skip_chat_ids):
    from ..feishu import FeishuError
    from ..models import MatrixError
    data = client._exec(["im", "+chat-list", "--as", "user", "--types", "p2p,group", "--exclude-muted",
                         "--sort", "active_time", "--page-size", str(MAX_CHATS), "--format", "json"])
    chats = (data.get("items") or data.get("chats") or []) if isinstance(data, dict) else data
    names = owner_words(config)
    exclude = set(skip_chat_ids) | set(state.get("chats:exclude", []))
    eligible = {c["chat_id"]: c for c in chats[:MAX_CHATS] if c.get("chat_id") and c["chat_id"] not in exclude
                and not skipped_chat(config, c.get("name") or "")}
    # Search is indexed asynchronously. Leave the newest five minutes to the next round.
    end = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
    cursors = {}
    for cid in eligible:
        since = _stamp(state.get("chats:cursor:" + cid))
        if not since:
            since = (now - timedelta(hours=2)).isoformat(timespec="seconds")
            state.set("chats:cursor:" + cid, since)
        cursors[cid] = since
    active = []
    ids = [cid for cid, since in cursors.items() if datetime.fromisoformat(since) < datetime.fromisoformat(end)]
    probe = state.get("chats:search-unavailable-until", "") < now.isoformat()
    for offset in range(0, len(ids), SEARCH_GROUP):
        group = ids[offset:offset + SEARCH_GROUP]
        hits = set(group)
        if probe:
            try:
                found = client._exec(["im", "+messages-search", "--as", "user", "--chat-id", ",".join(group),
                                      "--start", min(cursors[c] for c in group), "--end", end, "--sender-type", "user",
                                      "--no-reactions", "--page-size", "50", "--format", "json"])
                messages = found.get("messages")
                if isinstance(messages, list) and found.get("has_more") is False and all(m.get("chat_id") for m in messages):
                    hits = {m["chat_id"] for m in messages}
            except FeishuError as exc:
                if str(exc.code) not in {"missing_scope", "99991672", "99991679"}:
                    raise
                # Older grants can still read histories. Try search again tomorrow.
                probe = False
                state.set("chats:search-unavailable-until", (now + timedelta(days=1)).isoformat())
        for cid in group:
            if cid in hits or state.get("chats:page:" + cid):
                active.append(cid)
            else:
                state.set("chats:cursor:" + cid, end)
    # Do not fan out across a busy account. Rotate deferred conversations ahead of recently read ones.
    active.sort(key=lambda c: state.get("chats:visited:" + c, ""))
    kept = scanned = 0
    for cid in active[:HISTORY_PAGES]:
        chat = eligible[cid]
        cname = str(chat.get("name") or "")
        muted = muted_chat(config, cname)
        cursor_key = "chats:cursor:" + cid
        page_key = "chats:page:" + cid
        page = state.get(page_key) or {}
        until = page.get("end", end)
        args = ["im", "+chat-messages-list", "--chat-id", cid, "--start", cursors[cid], "--end", until,
                "--order", "asc", "--no-reactions", "--as", "user", "--format", "json"]
        if page:
            args += ["--page-token", page["token"]]
        state.set("chats:visited:" + cid, now.isoformat())
        got = client._exec(args)
        if got.get("has_more") and not got.get("page_token"):
            raise MatrixError("飞书聊天记录还有下一页，但未返回续读位置。")
        messages = got.get("messages") or got.get("items") or []
        history_key = "chats:recent:" + cid
        history = recent_lines(state, history_key)
        mine = set(state.get("chats:mine", []))
        for m in messages:
            sender = m.get("sender") or {}
            text = _text(m)
            if (sender.get("sender_type") != "user" or m.get("msg_type") not in {"text", "post"} or not text
                    or skipped_text(config, text)):
                continue
            scanned += 1
            from_owner = sender.get("id") == config.owner_open_id
            if from_owner:
                remember_mine(state, "chats:mine", m.get("message_id"))
                mine.add(m.get("message_id"))
            kind = classify(text, from_owner=from_owner, chat_mode=chat.get("chat_mode"), names=names,
                            mentioned=not from_owner and mentions_owner(m, config.owner_open_id),
                            replied=bool({m.get("root_id"), m.get("parent_id")} & mine - {None}),
                            broadcast=mentions_all(m))
            if kind and (not muted or kind == "@你"):
                inserted = state.db.execute("INSERT OR IGNORE INTO chat_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)", (
                    m.get("message_id"), cid, cname, chat.get("chat_mode"), sender.get("id"),
                    "我" if from_owner else (sender.get("name") or ""), m.get("create_time"), text[:500],
                    json.dumps(history[-2:], ensure_ascii=False), kind, "pending"))
                kept += inserted.rowcount
            history.append(f"{'我' if from_owner else sender.get('name') or '对方'}：{text[:120]}")
        state.db.commit()
        state.set(history_key, history[-2:])
        if got.get("has_more"):
            state.set(page_key, {"end": until, "token": got["page_token"]})
        else:
            state.set(cursor_key, until)
            state.set(page_key, None)
    if kept:
        from .. import activity
        pace = (f"其余 {config.chat_review_every_minutes} 分钟内统一提取" if config.chat_review_every_minutes
                else f"其余到下一个整理时间（{'、'.join(config.chat_review_times)}）统一提取")
        activity.log(client.root, "detect", f"聊天里发现 {kept} 条可能跟你有关的话：@你或带时间、催办的马上提取，{pace}")
    return {"chats": len(chats), "histories_read": min(len(active), HISTORY_PAGES), "messages_scanned": scanned, "candidates": kept}


def pending(state):
    table(state)
    return [dict(zip(("message_id", "chat_id", "chat_name", "chat_mode", "sender_id", "sender_name", "sent_at",
                      "text", "context", "kind"), r))
            for r in state.db.execute("SELECT message_id,chat_id,chat_name,chat_mode,sender_id,sender_name,sent_at,"
                                      "text,context,kind FROM chat_candidates WHERE status='pending' ORDER BY sent_at")]
