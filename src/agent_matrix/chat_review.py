"""Scheduled chat extraction: the model reads only the pre-filtered candidate lines.

    python -m agent_matrix.chat_review check      # 0 = a batch is due (wake the model), 2 = nothing to do
    python -m agent_matrix.chat_review prepare    # freeze the due batch; prints its path
    python -m agent_matrix.chat_review validate --review <batch>/review.json
    python -m agent_matrix.chat_review publish  --review <batch>/review.json

Quotes must be the message's own words, the owner must be the sender, named, or the one written to
privately, dates only when written. A candidate the model does not take goes in `skipped` with a kind;
a private message is never left out silently (see _every_private_line_decided). No task → no message.
"""
import argparse
from datetime import datetime, time, timedelta
from hashlib import sha1, sha256
import json
from pathlib import Path
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .bitable import js, require_owned
from .config import load
from .feishu import Feishu
from .models import MatrixError, MatrixBusy
from .sources import chats
from .state import State, exclusive_lock
from .timeutil import TZ

DEFAULT_TIMES = ["11:30", "14:30", "17:30", "20:30"]


class ChatAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: str
    title: str = Field(min_length=1, max_length=60)
    quote: str = Field(min_length=1, max_length=500)
    commitment_phrase: str = Field(min_length=1, max_length=40)
    owner_name: str | None = None
    due_date: str | None = None
    due_phrase: str | None = None
    executor: Literal["我", "AI", "别人"] | None = None
    ai_can_prep: bool = False
    done_when: str | None = Field(default=None, max_length=60)
    confidence: float = Field(ge=0, le=1)
    # Number of an item this one continues (the next step after it); see related_items.json. Not the same thing.
    follows: int | None = None
    # The owner's own words agreeing to take it ("好的", "我来"), copied from the message's `after` lines.
    accepted_quote: str | None = Field(default=None, max_length=80)


# Why a candidate is not an item. Not taking a line means saying which of these it is; a private message that
# fits none of them is someone asking the owner for something, so it becomes an item.
SKIP_KINDS = ("闲聊", "表态", "无关通知", "已办完", "点了别人", "同事的活", "并入另一条")


class ChatSkip(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: str
    kind: Literal[SKIP_KINDS]
    why: str = Field(min_length=1, max_length=40)


class ChatReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    batch_id: str
    actions: list[ChatAction] = Field(default_factory=list)
    skipped: list[ChatSkip] = Field(default_factory=list)


def _times(config):
    return getattr(config, "chat_review_times", None) or DEFAULT_TIMES


def due_slot(config, state, now):
    """The latest review time already passed today that has no batch yet; or, every N minutes, "every"."""
    every = getattr(config, "chat_review_every_minutes", 0)
    if every:
        last = state.get("chat-review:last")
        return None if last and now - datetime.fromisoformat(last) < timedelta(minutes=every) else "every"
    passed = [t for t in _times(config) if now.time() >= time.fromisoformat(t)]
    if not passed:
        return None
    slot = f"{now.date().isoformat()}T{passed[-1]}"
    return None if state.get("chat-review:slot:" + slot) else slot


URGENT = re.compile(r"今天|今晚|马上|立刻|立即|尽快|赶紧|抓紧|急|现在|等下|一会|待会|asap|ASAP|\d{1,2}[:：点]")


def _sent(message, now):
    try:
        at = datetime.fromisoformat(str(message.get("sent_at") or "").replace(" ", "T"))
        return at if at.tzinfo else at.replace(tzinfo=TZ)
    except ValueError:
        return now


def ready(config, waiting, now, slot=None):
    """When the waiting chat lines go to the AI. Chats are read every few minutes; each extraction costs a model
    call, so it runs only when there is something worth it:
        @you, a private ask, or someone asks you with a time or a push (今天 / 尽快 / 3点)  → at once
        otherwise at the pace chosen on the 系统 page: every N minutes (chat_review_every_minutes), or the fixed times
        late at night only the urgent ones run; the rest wait for the morning
    Returns why it should run now, or None."""
    if not waiting:
        return None
    urgent = [m for m in waiting if m.get("kind") == "@你" or (m.get("kind") == "找你的" and (
        m.get("chat_mode") == "p2p" or URGENT.search(m.get("text") or "")))]  # a private ask is someone waiting on you
    if urgent:
        return f"有 {len(urgent)} 条私聊、@你、带时间或催办，马上提取"
    hour = now.strftime("%H:%M")
    if hour < config.chat_extract_from or hour >= config.chat_extract_until:
        return None
    if slot == "every":
        return f"离上一轮满 {config.chat_review_every_minutes} 分钟"
    if slot:
        return f"到了整理时间 {slot[-5:]}"
    return None


def check(config, state, now):
    open_batch = state.get("chat-review:open")
    if open_batch:
        return {"ok": True, "run_model": True, "reason": "batch_open", "batch": open_batch}
    waiting = chats.pending(state)
    slot = due_slot(config, state, now)
    why = ready(config, waiting, now, slot)
    if why:
        return {"ok": True, "run_model": True, "reason": "batch_due", "why": why, "candidates": len(waiting), "slot": slot,
                "instruction": "按 prompts/chat_extract.md 处理：先 chat_review prepare，再写 review.json，validate 后 publish。"}
    if slot and slot != "every" and not waiting:
        state.set("chat-review:slot:" + slot, "empty")
    return {"ok": True, "run_model": False, "reason": "waiting" if waiting else "idle", "candidates": len(waiting)}


def prepare(config, state, root, now):
    open_batch = state.get("chat-review:open")
    if open_batch:
        return open_batch
    waiting = chats.pending(state)
    if not waiting:
        raise MatrixError("没有待提取的聊天。")
    batch = "chat-" + now.strftime("%Y%m%d-%H%M")
    folder = Path(root) / "state/chat-batches" / batch
    folder.mkdir(parents=True, exist_ok=True); folder.chmod(0o700)
    (folder / "candidates.json").write_text(json.dumps({"batch_id": batch, "owner_name": config.owner_name,
                                                        "owner_aliases": config.owner_aliases, "messages": waiting},
                                                       ensure_ascii=False, indent=1))
    for m in waiting:
        state.db.execute("UPDATE chat_candidates SET batch=? WHERE message_id=?", (batch, m["message_id"]))
    state.db.commit()
    result = {"batch_id": batch, "candidates_path": str(folder / "candidates.json"),
              "review_path": str(folder / "review.json"), "messages": len(waiting)}
    state.set("chat-review:open", result)
    slot = due_slot(config, state, now)
    if getattr(config, "chat_review_every_minutes", 0):
        state.set("chat-review:last", now.isoformat())  # an urgent round also restarts the regular pace
    elif slot:
        state.set("chat-review:slot:" + slot, batch)
    return result


def _batch(root, batch_id):
    return json.loads((Path(root) / "state/chat-batches" / batch_id / "candidates.json").read_text())


def owner_accepted(config, a, m):
    """Did the owner agree to take this? Their own message is a yes; otherwise one of the lines they wrote
    after it must contain the words the model quoted. Checked in code: the model cannot invent a "好的"."""
    if m.get("sender_name") == "我":
        return True
    quote = (a.accepted_quote or "").strip()
    if not quote:
        return False
    names = {config.owner_name, "我", *getattr(config, "owner_aliases", [])} - {""}
    for line in m.get("after") or []:
        head, _, said = str(line).partition("] ")
        if quote in said and any(head.endswith(" " + name) for name in names):
            return True
    return False


def validate(config, review, root):
    from .meeting_review import aliases, resolve_due, MIN_CONFIDENCE
    batch = _batch(root, review.batch_id)
    by_id = {m["message_id"]: m for m in batch["messages"]}
    kept = []
    for a in review.actions:
        m = by_id.get(a.message_id)
        if not m:
            raise MatrixError(f"「{a.title}」引用的消息不在本批里。")
        from .meeting_review import _loose
        for name in ("quote", "commitment_phrase", "due_phrase"):  # same words, different spacing: use the message's own
            value = getattr(a, name, None)
            if value and value not in m["text"]:
                found = _loose(value).search(m["text"]) if _loose(value) else None
                if found:
                    setattr(a, name, found.group(0))
        if a.quote not in m["text"]:
            raise MatrixError(f"「{a.title}」的原话不在那条消息里，不能改写。")
        if a.commitment_phrase not in m["text"]:
            raise MatrixError(f"「{a.title}」缺少承接或指派的原话，不算待办。")
        if a.owner_name:
            sender = config.owner_name if m["sender_name"] == "我" else m["sender_name"]
            extra = config.owner_aliases if a.owner_name == config.owner_name else ()
            named = any(x in m["text"] for x in aliases(a.owner_name, extra))
            private_to_owner = m["chat_mode"] == "p2p" and a.owner_name == config.owner_name
            if a.owner_name != sender and not named and not private_to_owner:
                raise MatrixError(f"「{a.title}」的负责人{a.owner_name}既不是发消息的人，也没被点名；改为 null。")
        if a.due_date:
            # "下周", "月底": no exact day can be worked out from the words. The date is left empty (the item
            # then waits for the owner to set one) instead of sending the whole batch back to the model.
            sent = datetime.fromisoformat(m["sent_at"].replace(" ", "T")) if m.get("sent_at") else datetime.now(TZ)
            from .timeutil import parse_date
            found = parse_date(a.due_phrase.rstrip("前之"), sent.date()) if a.due_phrase and a.due_phrase in m["text"] else None
            if not found or found.isoformat() != a.due_date:
                a.due_date = a.due_phrase = None
        if (getattr(config, "intake_mode", "all") == "accepted" and a.owner_name == config.owner_name
                and not owner_accepted(config, a, m)):
            continue  # 全自动: asked of the owner but not taken on by them, so it is not an item
        # A private message is someone talking to the owner: no "宁可漏" there, any confidence is kept.
        if a.confidence >= MIN_CONFIDENCE or private(m):
            kept.append((a, m))
    _every_private_line_decided(review, batch, by_id)
    return kept


def private(m):
    return m.get("chat_mode") == "p2p" and m.get("sender_name") != "我"


def _every_private_line_decided(review, batch, by_id):
    """Someone wrote to the owner directly: such a line is never dropped in silence. Each one is an item or is
    in `skipped` with a reason kind; "并入另一条" needs an item from the same chat in this batch to go into."""
    for s in review.skipped:
        if s.message_id not in by_id:
            raise MatrixError(f"skipped 里的 {s.message_id} 不在本批里。")
    acted = {a.message_id for a in review.actions}
    chats_with_item = {by_id[i]["chat_id"] for i in acted if i in by_id}
    for s in review.skipped:
        m = by_id[s.message_id]
        if s.kind == "并入另一条" and private(m) and m["chat_id"] not in chats_with_item:
            raise MatrixError(f"私聊「{m['text'][:30]}」写了并入另一条，但这个私聊在本批里没有提取任何待办；"
                              "它说的事要么提取成待办，要么换成别的类别。")
    decided = acted | {s.message_id for s in review.skipped}
    missing = [m for m in batch["messages"] if private(m) and m["message_id"] not in decided]
    if missing:
        lines = "；".join(f"{m['message_id']}（{m['sender_name']}：{m['text'][:30]}）" for m in missing[:5])
        raise MatrixError(f"这几条私聊没有结论：{lines}。私聊的每一条要么提取成待办，要么写进 skipped 并选一个类别。")


def task_fields(config, a, m):
    own = a.owner_name == config.owner_name
    executor = ("AI" if a.executor == "AI" else "我") if own else "别人"
    ready = bool(a.due_date) and (own or executor == "别人")
    status = "未开始" if ready else "待确认"
    fields = {"标题": a.title, "类型": ["临时"], "状态": [status], "执行方": [executor],
              "进度": {"未开始": "0/1 等对方" if executor == "别人" else "0/1 未开始", "待确认": "0/1 待确认"}[status],
              "下一步动作": a.title, "来源": ["聊天"],
              "来源链接": m["message_id"] if m["message_id"].startswith("https://")  # Slack permalink
                          else f"https://applink.feishu.cn/client/chat/open?openChatId={m['chat_id']}",
              "原话引用": f"[{m['chat_name']} {m.get('sent_at') or ''} {m['sender_name']}] {m['text'][:300]}"}
    if executor == "别人" and m["kind"] != "找你的":
        fields["排除本人提醒"] = True
    if own:
        fields["负责人"] = [{"id": config.owner_open_id}]
        if executor == "我" and a.ai_can_prep:
            fields["能否AI预做"] = True
    if a.done_when:
        fields["完成标准"] = a.done_when
    if a.due_date:
        fields["截止日"] = fields["首次截止日"] = a.due_date + " 00:00"
    return fields


def publish(config, client, state, review):
    from .workflow import Gateway
    from .brief_migration import number
    from .delivery import send_once
    require_owned(config, state)
    kept = validate(config, review, client.root)
    gateway = Gateway(client, state)
    created = []
    from . import itemlinks
    from .merge import find_same, merge_into
    rows, merged = gateway.rows(), []
    for a, m in kept:
        key = "chat:" + a.message_id + ":" + sha1(re.sub(r"\s+", "", a.title).encode()).hexdigest()[:10]
        target, _, _ = find_same(rows, a.title)
        if target is not None:
            merge_into(gateway, target, title=a.title, source="聊天「" + m["chat_name"][:10] + "」",
                       detail=f"{m['sender_name']}：{m['text'][:120]}", key=key + ":merge", now=datetime.now(TZ))
            merged.append(target.get("编号"))
            continue
        created.append(gateway.create(task_fields(config, a, m), key, "chat-review")["record_id_list"][0])
        itemlinks.link_by_number(client.root, rows, created[-1], a.follows)
    state.db.execute("UPDATE chat_candidates SET status='done' WHERE batch=?", (review.batch_id,))
    state.db.commit()
    state.set("chat-review:open", None)
    message_id = None
    rows = {r["record_id"]: r for r in gateway.rows() if r["record_id"] in created}
    shown = [r for r in rows.values() if not r.get("排除本人提醒")]
    if shown:
        groups = {}
        for r in shown:
            who = {"我": "你", "AI": "AI", "别人": "别人"}.get((r.get("执行方") or ["我"])[0], "你")
            groups.setdefault(who, []).append(f"#{number(r['编号'])}")
        parts = " / ".join(k + " " + " ".join(v) for k, v in groups.items())
        first = next(iter(groups.values()))[0].lstrip("#")
        text = f"聊天 · 新增 {len(shown)} 件：{parts}\n回复：展开 {first} 看原话"
        if merged:
            text += "\n另有 " + "、".join(f"#{n}" for n in merged) + " 收到新进展，已并入"
        key = "chat-review-" + sha256(review.batch_id.encode()).hexdigest()[:24]
        message_id = send_once(state, key, lambda: client.send_self(text, key)).get("message_id")
    from . import activity
    activity.log(client.root, "detect", f"聊天提取完成：看了 {len(_batch(client.root, review.batch_id)['messages'])} 条，" +
                 (f"新增 {len(created)} 件" if created else "没有新待办"))
    return {"ok": True, "batch_id": review.batch_id, "created": len(created), "shown": len(shown), "message_id": message_id}


def main(argv=None):
    p = argparse.ArgumentParser(description="聊天定时提取")
    p.add_argument("command", choices=["check", "prepare", "validate", "publish"])
    p.add_argument("--review", type=Path)
    p.add_argument("--config", type=Path, default=Path("config.yaml"))
    args = p.parse_args(argv)
    path = args.config.resolve(); root = path.parent
    try:
        config = load(path)
        with exclusive_lock(root / "state", wait=0 if args.command == "check" else 90):
            state = State(root / "state/state.db")
            try:
                now = datetime.now(TZ)
                if args.command == "check":
                    result = check(config, state, now)
                    from . import activity
                    activity.beat(root, "聊天检查", f"候选 {result.get('candidates', 0)} 条")
                    print(js(result)); return 0 if result["run_model"] else 2
                if args.command == "prepare":
                    print(js({"ok": True, **prepare(config, state, root, now)})); return 0
                review = ChatReview.model_validate_json(args.review.read_text())
                if args.command == "validate":
                    kept = validate(config, review, root)
                    print(js({"ok": True, "actions": len(kept), "skipped": len(review.skipped),
                              "dropped_low_confidence": len(review.actions) - len(kept)})); return 0
                client = Feishu(config, root); client.verify_owner(client.auth())
                print(js(publish(config, client, state, review))); return 0
            finally:
                state.close()
    except MatrixBusy:
        print(js({"ok": True, "run_model": False, "reason": "busy"})); return 2
    except (MatrixError, OSError, ValueError) as exc:
        from . import activity
        activity.log(root, "error", f"聊天 {args.command} 出错", detail=str(exc), status="failed")
        print(js({"ok": False, "error": str(exc) if isinstance(exc, MatrixError) else f"处理失败（{type(exc).__name__}）"})); return 1


if __name__ == "__main__":
    raise SystemExit(main())
