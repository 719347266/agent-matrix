"""Slack → the same candidate pile as Feishu chats. Pure code, no model.

Reads the owner's DMs and channels with a Slack *user* token (xoxp-, scopes: search:read, channels:history,
groups:history, im:history, mpim:history, channels:read, groups:read, im:read, mpim:read, users:read),
filters with the same rules as Feishu chats, and writes into chat_candidates, so chat_review
extracts and publishes them unchanged.
"""
from datetime import timedelta
import json
import os
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..models import MatrixError
from .chats import MAX_CHATS, classify, muted_chat, owner_words, recent_lines, skipped_chat, skipped_text, table

API = "https://slack.com/api/"
# Subtypes that are still a person talking: a picture or file with its caption, a thread reply also sent to the channel.
USER_SUBTYPES = {None, "file_share", "thread_broadcast"}


def token(config):
    return config.slack_token or os.environ.get("SLACK_USER_TOKEN", "")


def call(tok, method, **params):
    req = Request(API + method + "?" + urlencode({k: v for k, v in params.items() if v is not None}),
                  headers={"Authorization": "Bearer " + tok})
    with urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    if not data.get("ok"):
        raise MatrixError(f"Slack {method} 失败：{data.get('error')}")  # error code only, never the token
    return data


def permalink(base, channel, ts, thread=None):
    link = f"{base.rstrip('/')}/archives/{channel}/p{ts.replace('.', '')}"
    return link + f"?thread_ts={thread}&cid={channel}" if thread and thread != ts else link


def _thread(m):
    """The thread a message replies in (search results only carry it inside their permalink)."""
    found = re.search(r"thread_ts=(\d+\.\d+)", m.get("permalink") or "")
    return m.get("thread_ts") or (found.group(1) if found else None)


def _names(api, tok):
    people = {}

    def who(uid):
        if uid not in people:
            u = api(tok, "users.info", user=uid).get("user") or {}
            people[uid] = (u.get("profile") or {}).get("display_name") or u.get("real_name") or u.get("name") or uid
        return people[uid]
    return who


def learn_name(root, config, api, tok, uid):
    """No owner name yet: take it from the Slack profile, so "you" in a message is recognised as the owner."""
    from ..config import load, save
    path = root / "config.yaml"
    if config.owner_name or not path.exists():
        return
    u = api(tok, "users.info", user=uid).get("user") or {}
    p = u.get("profile") or {}
    names = list(dict.fromkeys(x.strip() for x in (p.get("real_name") or u.get("real_name"), p.get("display_name"), u.get("name"))
                               if x and x.strip()))
    if not names:
        return
    saved = load(path)
    if not saved.owner_name:
        saved.owner_name = names[0]
        saved.owner_aliases = list(dict.fromkeys([*saved.owner_aliases, *names[1:]]))
        save(path, saved)
    config.owner_name, config.owner_aliases = saved.owner_name, saved.owner_aliases


def link_parts(link):
    """Slack permalink → (channel, ts, thread_ts or None), or None."""
    m = re.search(r"slack\.com/archives/([A-Z0-9]+)/p(\d{10})(\d{6})(?:\?.*?thread_ts=(\d+\.\d+))?", link or "")
    return (m.group(1), f"{m.group(2)}.{m.group(3)}", m.group(4)) if m else None


MAX_THREADS = 10


def history(config, channel, oldest, latest=None, *, thread=None, api=call, limit=50):
    """[(ts, who, text)] of one conversation after `oldest` (and before `latest`), oldest first; [] if unreadable.
    With `thread`: that thread's replies. Without: the channel, plus replies in its threads (marked ↳)."""
    tok = token(config)
    if not tok:
        return []
    try:
        me = api(tok, "auth.test")["user_id"]
        who = _names(api, tok)
        window = {"oldest": oldest, "latest": latest, "limit": limit}
        if thread:
            msgs = api(tok, "conversations.replies", channel=channel, ts=thread, **window).get("messages") or []
        else:
            msgs = api(tok, "conversations.history", channel=channel, **window).get("messages") or []
            busy = [m for m in msgs if m.get("reply_count") and float(m.get("latest_reply") or 0) > float(oldest)]
            for m in busy[:MAX_THREADS]:  # ponytail: newest 10 threads per read; raise if long channels miss replies
                msgs += api(tok, "conversations.replies", channel=channel, ts=m["ts"], **window).get("messages") or []
        out, seen = [], set()
        for m in sorted(msgs, key=lambda m: float(m["ts"])):
            if m["ts"] in seen or m.get("subtype") not in USER_SUBTYPES or not m.get("user") or not m.get("text") or float(m["ts"]) <= float(oldest):
                continue
            seen.add(m["ts"])
            text = " ".join(re.sub(r"<@(U\w+)>", lambda x: "@" + who(x.group(1)), m["text"]).split())
            reply = not thread and m.get("thread_ts") not in (None, m["ts"])
            out.append((m["ts"], "本人" if m["user"] == me else who(m["user"]), ("↳ " if reply else "") + text))
        return out[:limit]
    except (MatrixError, OSError, KeyError):
        return []


def _searched(api, tok, oldest, who):
    """Every message the owner can see since `oldest`, newest pages first: one or two calls instead of one per
    conversation. Needs the search:read scope; raises MatrixError(missing_scope) without it."""
    from datetime import datetime, timedelta
    day = (datetime.fromtimestamp(float(oldest)) - timedelta(days=1)).strftime("%Y-%m-%d")  # after: is a whole day
    found, page = [], 1
    while True:
        res = api(tok, "search.messages", query=f"after:{day}", sort="timestamp", sort_dir="desc", count=100, page=page)
        box = res.get("messages") or {}
        matches = box.get("matches") or []
        found += [m for m in matches if float(m["ts"]) > float(oldest)]
        pages = (box.get("paging") or {}).get("pages") or 1
        # ponytail: stop at 10 pages (1000 messages) per poll; the cursor still advances past what was seen.
        if page >= min(pages, 10) or not matches or float(matches[-1]["ts"]) <= float(oldest):
            break
        page += 1
    convs = {}
    for m in found:
        c = m.get("channel") or {}
        if not c.get("id"):
            continue
        im = bool(c.get("is_im"))
        name = who(c.get("user") or c.get("name")) if im else (c.get("name") or c["id"])
        convs.setdefault(c["id"], {"id": c["id"], "is_im": im, "name": name, "messages": []})["messages"].append(m)
    return list(convs.values())


def _listed(api, tok, oldest, who, state):
    """Fallback without search:read: read conversation by conversation (DMs first, then channels)."""
    convs = api(tok, "users.conversations", types="im,mpim,public_channel,private_channel",
                exclude_archived="true", limit=200).get("channels") or []
    # ponytail: `updated` is when the channel's settings changed, not its last message; DMs first, then by it.
    convs = sorted(convs, key=lambda c: (bool(c.get("is_im") or c.get("is_mpim")), c.get("updated") or 0), reverse=True)[:MAX_CHATS]
    out = []
    for c in convs:
        cid = c["id"]
        since = state.get("slack:cursor:" + cid) or oldest
        msgs = api(tok, "conversations.history", channel=cid, oldest=since, limit=200).get("messages") or []
        out.append({"id": cid, "is_im": bool(c.get("is_im")), "name": who(c["user"]) if c.get("is_im") else c.get("name") or cid,
                    "messages": msgs, "cursor": True})
    return out


def poll(config, root, state, now, *, api=call, since_hours=2):
    tok = token(config)
    if not tok:
        return "no_token"
    table(state)
    me = api(tok, "auth.test")
    my_id, base = me["user_id"], me["url"]
    learn_name(root, config, api, tok, my_id)
    me_tag = "@" + (config.owner_name or "我")
    names = owner_words(config) | {me_tag}  # a Slack @mention of the owner counts even without owner_name
    who = _names(api, tok)

    oldest = state.get("slack:search-cursor") or str((now - timedelta(hours=since_hours)).timestamp())
    try:
        convs, how = _searched(api, tok, oldest, who), "search"
    except MatrixError as exc:
        if "missing_scope" not in str(exc):
            raise
        convs, how = _listed(api, tok, oldest, who, state), "list"
    exclude = set(state.get("slack:exclude", []))
    kept = scanned = 0
    newest_all = oldest
    for c in convs:
        cid = c["id"]
        mode = "p2p" if c["is_im"] else "group"
        cname = "Slack · " + c["name"]
        msgs = sorted(c["messages"], key=lambda m: float(m["ts"]))
        newest = max([oldest, *[m["ts"] for m in msgs]], key=float)
        newest_all = max(newest_all, newest, key=float)
        if c.get("cursor"):
            state.set("slack:cursor:" + cid, newest)
        if cid in exclude or skipped_chat(config, c["name"]):
            continue
        muted = muted_chat(config, c["name"])
        history_key = "slack:recent:" + cid
        history = recent_lines(state, history_key)
        for m in msgs:
            if m.get("subtype") not in USER_SUBTYPES or m.get("bot_id") or not m.get("user") or not m.get("text") or skipped_text(config, m["text"]):
                continue
            from_owner = m["user"] == my_id
            # Owner mentions become the owner's name so the "被点名" rule in chat_review sees them.
            text = " ".join(m["text"].replace(f"<@{my_id}>", me_tag).split())
            text = re.sub(r"<@(U\w+)>", lambda x: "@" + who(x.group(1)), text)
            scanned += 1
            sender = "我" if from_owner else who(m["user"])
            kind = classify(text, from_owner=from_owner, chat_mode=mode, names=names,
                            mentioned=f"<@{my_id}>" in m["text"])  # a real @, however it is worded
            if kind and (not muted or kind == "@你"):
                sent = now.fromtimestamp(float(m["ts"]), now.tzinfo).strftime("%Y-%m-%d %H:%M")
                # ponytail: the permalink is the message id, so chat_review can link back without a schema change.
                state.db.execute("INSERT OR IGNORE INTO chat_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)", (
                    permalink(base, cid, m["ts"], _thread(m)), cid, cname, mode, m["user"], sender, sent, text[:500],
                    json.dumps(history[-2:], ensure_ascii=False), kind, "pending"))
                kept += 1
            history.append(f"{sender}：{text[:120]}")
        state.set(history_key, history[-2:])
        state.db.commit()
    if how == "search":
        state.set("slack:search-cursor", newest_all)
    if kept:
        from .. import activity
        activity.log(root, "detect", f"Slack 里发现 {kept} 条可能跟你有关的话，很快统一提取")
    state.set("slack:last-poll", now.isoformat())
    return {"chats": len(convs), "messages_scanned": scanned, "candidates": kept, "via": how}


def dry_run(config, *, hours=24, api=call):
    """Read Slack, return what the filter keeps. Throwaway state: no cursor moves, nothing is written."""
    import tempfile
    from datetime import datetime
    from pathlib import Path
    from ..state import State
    from ..timeutil import TZ
    from .chats import pending
    tmp = Path(tempfile.mkdtemp())
    state = State(tmp / "state.db")
    try:
        out = poll(config, tmp, state, datetime.now(TZ), api=api, since_hours=hours)
        if out == "no_token":
            raise MatrixError("没有 Slack token：在 config.yaml 写 slack_token，或设环境变量 SLACK_USER_TOKEN。")
        return {**out, "lines": pending(state)}
    finally:
        state.close()


def connect(root, tok, *, api=call):
    """Check the token with Slack, then save it to config.yaml. Never echoes the token back."""
    from ..config import load, save
    from ..state import State
    tok = (tok or "").strip()
    if not tok.startswith("xoxp-"):
        raise MatrixError("要用户 token（xoxp- 开头），在 Slack App 的 OAuth & Permissions 页复制 User OAuth Token。")
    me = api(tok, "auth.test")
    path = root / "config.yaml"
    config = load(path)
    config.slack_token = tok
    save(path, config)
    learn_name(root, config, api, tok, me["user_id"])
    who = {"team": me.get("team"), "user": me.get("user"), "url": me.get("url"), "tail": tok[-4:]}
    state = State(root / "state/state.db")
    try:
        state.set("slack:identity", who)
    finally:
        state.close()
    return who


def disconnect(root):
    from ..config import load, save
    from ..state import State
    path = root / "config.yaml"
    config = load(path)
    config.slack_token = ""
    save(path, config)
    state = State(root / "state/state.db")
    try:
        state.set("slack:identity", None)
    finally:
        state.close()


def main(argv=None):
    """Dry run: read Slack, print what the filter keeps. Uses a throwaway state, writes nothing real.

    python -m agent_matrix.sources.slack --hours 24
    """
    import argparse
    from pathlib import Path
    from ..config import load
    p = argparse.ArgumentParser(description="Slack 试跑：只读、只打印，不写表")
    p.add_argument("--hours", type=float, default=24, help="往回看多少小时")
    p.add_argument("--config", type=Path, default=Path("config.yaml"))
    args = p.parse_args(argv)
    try:
        out = dry_run(load(args.config), hours=args.hours)
    except MatrixError as exc:
        print(exc); return 1
    print(f"看了 {out['chats']} 个会话、{out['messages_scanned']} 条消息，筛中 {out['candidates']} 条：\n")
    for m in out["lines"]:
        print(f"[{m['kind']}] {m['chat_name']} {m['sent_at']} {m['sender_name']}：{m['text']}\n  {m['message_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
