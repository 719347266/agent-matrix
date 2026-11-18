"""Slack → the same candidate pile as Feishu chats. Pure code, no model.

Reads the owner's DMs and channels with a Slack *user* token (xoxp-, scopes: channels:history,
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
from .chats import MAX_CHATS, SKIP_CHAT, classify, owner_words, table

API = "https://slack.com/api/"


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


def permalink(base, channel, ts):
    return f"{base.rstrip('/')}/archives/{channel}/p{ts.replace('.', '')}"


def poll(config, root, state, now, *, api=call, since_hours=2):
    tok = token(config)
    if not tok:
        return "no_token"
    table(state)
    me = api(tok, "auth.test")
    my_id, base = me["user_id"], me["url"]
    names = owner_words(config)
    people = {}

    def who(uid):
        if uid not in people:
            u = api(tok, "users.info", user=uid).get("user") or {}
            people[uid] = (u.get("profile") or {}).get("display_name") or u.get("real_name") or u.get("name") or uid
        return people[uid]

    # ponytail: newest-updated MAX_CHATS conversations only; paginate if the owner lives in more.
    convs = api(tok, "users.conversations", types="im,mpim,public_channel,private_channel",
                exclude_archived="true", limit=200).get("channels") or []
    convs = sorted(convs, key=lambda c: c.get("updated") or 0, reverse=True)[:MAX_CHATS]
    exclude = set(state.get("slack:exclude", []))
    kept = scanned = 0
    for c in convs:
        cid = c["id"]
        mode = "p2p" if c.get("is_im") else "group"
        cname = "Slack · " + (who(c["user"]) if c.get("is_im") else c.get("name") or cid)
        if cid in exclude or SKIP_CHAT.search(cname):
            continue
        cursor_key = "slack:cursor:" + cid
        oldest = state.get(cursor_key) or str((now - timedelta(hours=since_hours)).timestamp())
        msgs = api(tok, "conversations.history", channel=cid, oldest=oldest, limit=200).get("messages") or []
        history, newest = [], oldest
        for m in sorted(msgs, key=lambda m: float(m["ts"])):  # API returns newest first
            newest = max(newest, m["ts"], key=float)
            if m.get("subtype") or m.get("bot_id") or not m.get("user") or not m.get("text"):
                continue
            from_owner = m["user"] == my_id
            # Owner mentions become the owner's name so the "被点名" rule in chat_review sees them.
            text = " ".join(m["text"].replace(f"<@{my_id}>", "@" + (config.owner_name or "我")).split())
            text = re.sub(r"<@(U\w+)>", lambda x: "@" + who(x.group(1)), text)
            scanned += 1
            sender = "我" if from_owner else who(m["user"])
            kind = classify(text, from_owner=from_owner, chat_mode=mode, names=names)
            if kind:
                sent = now.fromtimestamp(float(m["ts"]), now.tzinfo).strftime("%Y-%m-%d %H:%M")
                # ponytail: the permalink is the message id, so chat_review can link back without a schema change.
                state.db.execute("INSERT OR IGNORE INTO chat_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)", (
                    permalink(base, cid, m["ts"]), cid, cname, mode, m["user"], sender, sent, text[:500],
                    json.dumps(history[-2:], ensure_ascii=False), kind, "pending"))
                kept += 1
            history.append(f"{sender}：{text[:120]}")
        state.db.commit()
        state.set(cursor_key, newest)
    if kept:
        from .. import activity
        activity.log(root, "detect", f"Slack 里发现 {kept} 条可能跟你有关的话，到下一个整理时间统一提取")
    state.set("slack:last-poll", now.isoformat())
    return {"chats": len(convs), "messages_scanned": scanned, "candidates": kept}



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
