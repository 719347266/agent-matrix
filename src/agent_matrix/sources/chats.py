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
MAX_CHATS = 25   # chats listed per poll (Feishu) / conversations read per poll (Slack)
POLL_MINUTES = 30
HISTORY_PAGES = 5
SEARCH_GROUP = 10
SEEN_KEEP = 2000  # Lark: message ids already seen by the cross-chat search


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
        # Lark's API quota is per company and small: one cross-chat search a round; Feishu reads each busy chat in full
        reader = _poll_search if getattr(config, "lark_brand", "feishu") == "lark" else _poll
        result = reader(config, client, state, now, skip_chat_ids)
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


def _poll_search(config, client, state, now, skip_chat_ids):
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
    return {"chats": len(chats), "messages_scanned": scanned, "candidates": kept, "via": "search"}


def pending(state):
    table(state)
    return [dict(zip(("message_id", "chat_id", "chat_name", "chat_mode", "sender_id", "sender_name", "sent_at",
                      "text", "context", "kind"), r))
            for r in state.db.execute("SELECT message_id,chat_id,chat_name,chat_mode,sender_id,sender_name,sent_at,"
                                      "text,context,kind FROM chat_candidates WHERE status='pending' ORDER BY sent_at")]
