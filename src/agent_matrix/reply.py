"""One parser for owner replies, from the Cindy chat and from the Feishu private chat.

    #15 过 · 19 退 少了定价 · 8 周五 · 8 11-20 · 8 不做 · 12 完成 · 12 我来
    12 AI做 · 12 给张三 · 18 AI备 · 24 A · 20 + 上周数据 · 展开 12 · 15 19 过

Every change is written through Gateway (re-read, patch, read back). Only after the
read-back matches does the owner get "✓". Unknown numbers or unclear wording get
one short question; nothing is guessed.

Commands:
    python -m agent_matrix.reply --text "<owner message>"   # Cindy chat; prints the reply
    python -m agent_matrix.reply poll                         # Feishu replies + brief catch-up + 12/17 batch
"""
import argparse
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, time
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

from .bitable import js, require_owned
from .brief_migration import single, number
from .config import load
from .feishu import Feishu
from .models import MatrixError, MatrixBusy
from .state import State, exclusive_lock

WEEKDAYS = "一二三四五六日"


@dataclass
class Op:
    numbers: list
    verb: str
    arg: str = ""
    raw: str = ""


def parse_date(text, today):
    """Owner wording → date. Returns None when the text is not a date."""
    t = text.strip()
    if t in {"今天", "今日"}:
        return today
    if t in {"明天", "明日"}:
        return today + timedelta(days=1)
    if t == "后天":
        return today + timedelta(days=2)
    m = re.fullmatch(r"(下下|下|本|这)?(?:周|星期|礼拜)([一二三四五六日天])", t)
    if m:
        target = WEEKDAYS.index(m[2].replace("天", "日"))
        base = today - timedelta(days=today.weekday())
        offset = {"下": 7, "下下": 14}.get(m[1] or "", 0)
        found = base + timedelta(days=target + offset)
        if not m[1] and found < today:
            found += timedelta(days=7)  # "周五" said on Saturday means next Friday
        return found
    m = re.fullmatch(r"(?:(\d{4})[-/.年])?(\d{1,2})[-/.月](\d{1,2})[日号]?", t)
    if m:
        year = int(m[1]) if m[1] else today.year
        try:
            found = date(year, int(m[2]), int(m[3]))
        except ValueError:
            return None
        if not m[1] and found < today - timedelta(days=60):
            found = date(year + 1, found.month, found.day)
        return found
    return None


VERBS = [
    (r"(过|通过|可以|ok|OK|好的?)", "approve"),
    (r"(退|退回|打回)\s*[:：]?\s*(.*)", "reject"),
    (r"(完成|做完了?|搞定|done)", "complete"),
    (r"(不做|取消|不要了)", "cancel"),
    (r"(我来|我做)", "me"),
    (r"(AI做|ai做|交给AI|交给ai|AI来|ai来)", "ai"),
    (r"(AI备|ai备|AI准备|ai准备)", "prep"),
    (r"给\s*(\S{1,12})", "delegate"),
    (r"([A-Da-d])", "option"),
    (r"\+\s*(.+)", "material"),
]


def parse(text, today):
    """Return (ops, problems). ops empty and problems empty → not a command at all."""
    ops, problems = [], []
    for raw in [x.strip() for x in str(text).splitlines() if x.strip()]:
        m = re.fullmatch(r"(?:展开|详情|看看)\s*#?(\d+)", raw)
        if m:
            ops.append(Op([int(m[1])], "expand", raw=raw)); continue
        if not re.match(r"#?\d{1,5}(?!\d)", raw):
            continue  # ordinary chat, not an item command
        hashed = raw.startswith("#")
        single_number = re.fullmatch(r"#?(\d{1,5})\s*(?:改到|改成|推到|延到|挪到|放到|改)?\s*(.+)", raw)
        if single_number and parse_date(single_number[2], today):
            # "8 11-20" / "8 周五": a date right after one number.
            ops.append(Op([int(single_number[1])], "reschedule", parse_date(single_number[2], today).isoformat(), raw))
            continue
        m = re.fullmatch(r"((?:#?\d{1,5}\s*[,，、]?\s*)+?)\s*([^\d#,，、\s].*)?", raw)
        if not m:
            continue
        numbers = [int(x) for x in re.findall(r"\d{1,5}", m[1])]
        rest = (m[2] or "").strip()
        if not rest:
            if hashed or len(numbers) > 1:
                problems.append(f"「{raw}」要做什么？比如：{numbers[0]} 过 / {numbers[0]} 周五 / {numbers[0]} 不做")
            continue
        for pattern, verb in VERBS:
            found = re.fullmatch(pattern, rest)
            if found:
                arg = found.group(found.lastindex) if verb in {"reject", "delegate", "option", "material"} else ""
                if verb == "reject" and not arg.strip():
                    problems.append(f"#{numbers[0]} 退回的话，要改哪里？例如：{numbers[0]} 退 少了定价")
                    break
                ops.append(Op(numbers, verb, arg.strip(), raw))
                break
        else:
            # "3个会都开完了" is chat; only a #-prefixed or short reply gets a question back.
            if hashed or len(rest) <= 6:
                problems.append(f"没看懂「{raw}」。可回：过 / 退 意见 / 完成 / 周五 / 不做 / 我来 / AI做 / AI备 / 展开")
    return ops, problems


def _now(config):
    return datetime.now(ZoneInfo(config.timezone))


def _title(row, n=12):
    t = re.sub(r"\s+", "", str(row.get("标题") or ""))
    return t if len(t) <= n else t[:n - 1] + "…"


def _append(old, line):
    return ((old or "").rstrip() + "\n" + line).strip()


class Applier:
    def __init__(self, config, client, state, *, source, message_ref):
        from .workflow import Gateway
        self.config, self.client, self.state = config, client, state
        self.gateway = Gateway(client, state)
        self.source, self.ref = source, message_ref
        self.now = _now(config)

    def rows(self):
        return self.gateway.rows()

    def find(self, n):
        for row in self.rows():
            try:
                if number(row.get("编号")) == n:
                    return row
            except MatrixError:
                continue
        return None

    def key(self, op, n):
        return f"reply:{self.ref}:{n}:{op.verb}:{sha256(op.arg.encode()).hexdigest()[:8]}"

    def write(self, row, patch, op, n):
        return self.gateway.update(row, patch, self.key(op, n), f"reply:{self.source}:{self.ref}")

    def ready_after(self, row, patch):
        """待确认 becomes 未开始 once executor, owner (or 别人) and date are all known."""
        merged = {**row, **patch}
        executor = single(merged.get("执行方"))
        if single(merged.get("状态")) != "待确认" or not executor or not merged.get("截止日"):
            return patch
        if executor == "别人" or merged.get("负责人"):
            return {**patch, "状态": ["未开始"], "进度": "0/1 等对方" if executor == "别人" else "0/1 未开始"}
        return patch

    def apply(self, op):
        out = []
        for n in op.numbers:
            row = self.find(n)
            if not row:
                out.append(f"#{n} 找不到这个编号，看一下简报里的数字？"); continue
            try:
                out.append(self.one(op, n, row))
            except MatrixError as exc:
                out.append(f"#{n} 没改成：{exc}")
        return out

    def one(self, op, n, row):
        st, ex, name = single(row.get("状态")), single(row.get("执行方")), _title(row)
        owner = [{"id": self.config.owner_open_id}]
        if op.verb == "expand":
            return self.expand(row)
        if st in {"完成", "取消"} and op.verb not in {"reschedule"}:
            return f"#{n} {name} 已经是{st}，没动。"
        if op.verb == "approve":
            if st != "待验收":
                return f"#{n} {name} 现在是{st}，还没交付，不能验收。"
            self.write(row, {"状态": ["完成"], "验收结果": ["通过"], "完成时间": self.now.isoformat(),
                             "进度": "完成"}, op, n)
            unlocked = self.unlock_parent(row, n)
            return f"#{n} {name} → 完成 ✓" + (f"；#{unlocked} 可以开始了" if unlocked else "")
        if op.verb == "reject":
            if st != "待验收":
                return f"#{n} {name} 现在是{st}，没有待验收的结果可退。"
            self.write(row, {"状态": ["进行中"], "验收结果": ["退回"], "进度": "按意见修改",
                             "退回意见": _append(row.get("退回意见"), f"{self.now:%m-%d} {op.arg}")}, op, n)
            return f"#{n} {name} → 退回修改 ✓"
        if op.verb == "complete":
            self.write(row, {"状态": ["完成"], "完成时间": self.now.isoformat(), "进度": "完成"}, op, n)
            return f"#{n} {name} → 完成 ✓"
        if op.verb == "cancel":
            self.write(row, {"状态": ["取消"], "进度": "不做"}, op, n)
            return f"#{n} {name} → 不做了 ✓"
        if op.verb == "reschedule":
            from .workflow import day
            old, new = day(row.get("截止日")), date.fromisoformat(op.arg)
            patch = {"截止日": new.strftime("%Y-%m-%d 00:00")}
            if old and new > old:
                count = int(row.get("顺延次数") or 0) + 1
                patch["顺延次数"] = count
                patch["顺延历史"] = _append(row.get("顺延历史"), js({"at": self.now.isoformat(), "from": old.isoformat(),
                                                                "to": new.isoformat(), "count": count, "source": "owner"}))
            if not row.get("首次截止日") and old:
                patch["首次截止日"] = old.strftime("%Y-%m-%d 00:00")
            if st in {"完成", "取消"}:
                return f"#{n} {name} 已经是{st}，没改日期。"
            patch = self.ready_after(row, patch)
            self.write(row, patch, op, n)
            span = f"{old:%m-%d}→{new:%m-%d}" if old else f"定在 {new:%m-%d}"
            return f"#{n} {name} → {span} ✓" + (f"（顺延{patch['顺延次数']}次）" if patch.get("顺延次数") else "")
        if op.verb in {"me", "ai"}:
            executor = "我" if op.verb == "me" else "AI"
            patch = {"执行方": [executor]}
            if not row.get("负责人"):
                patch["负责人"] = owner
            if op.verb == "ai" and st == "待确认" and not row.get("截止日"):
                patch["截止日"] = (self.now.date() + timedelta(days=2)).strftime("%Y-%m-%d 00:00")
            patch = self.ready_after(row, patch)
            self.write(row, patch, op, n)
            return f"#{n} {name} → {'你来做' if executor == '我' else 'AI 来做'} ✓"
        if op.verb == "delegate":
            patch = self.ready_after(row, {"执行方": ["别人"], "进度": ("交给" + op.arg)[:15],
                                           "下一步动作": _append(row.get("下一步动作"), f"已交给 {op.arg}")})
            self.write(row, patch, op, n)
            return f"#{n} {name} → 交给{op.arg} ✓"
        if op.verb == "option":
            patch = self.ready_after(row, {"拍板结果": op.arg.upper()})
            self.write(row, patch, op, n)
            return f"#{n} {name} → 选 {op.arg.upper()} ✓"
        if op.verb == "material":
            patch = {"补充材料": _append(row.get("补充材料"), f"{self.now:%m-%d} {op.arg}")}
            if st == "卡住" and ex == "AI":
                patch.update({"状态": ["进行中"], "卡点": None, "进度": "材料已补，继续做"})
            self.write(row, patch, op, n)
            return f"#{n} {name} → 材料已记下 ✓" + ("，AI 接着做" if patch.get("状态") else "")
        if op.verb == "prep":
            if ex != "我":
                return f"#{n} {name} 不是你的事，不用 AI备。"
            child = self.gateway.create({
                "标题": ("备材料：" + str(row.get("标题")))[:60], "执行方": ["AI"], "状态": ["未开始"],
                "进度": "0/1 未开始", "负责人": owner, "父事项": [{"id": row["record_id"]}],
                "截止日": (row.get("截止日") and self._before(row["截止日"])) or (self.now.date() + timedelta(days=1)).strftime("%Y-%m-%d 00:00"),
                "完成标准": ("能直接用于：" + str(row.get("标题")))[:60], "来源": ["手动"], "类型": ["临时"],
            }, "prep:" + row["record_id"], f"reply:{self.source}:{self.ref}")
            child_id = child["record_id_list"][0]
            child_row = next(r for r in self.rows() if r["record_id"] == child_id)
            m = number(child_row["编号"])
            fresh = self.find(n)
            self.write(fresh, {"状态": ["卡住"], "卡点": f"等#{m}备材料", "进度": "0/1 等AI备材料"}, op, n)
            return f"#{n} {name} → AI 先备材料（#{m}），备好你验收后再开始 ✓"
        return f"#{n} 没看懂要做什么。"

    def _before(self, value):
        from .workflow import day
        found = day(value) - timedelta(days=1)
        return max(found, self.now.date()).strftime("%Y-%m-%d 00:00")

    def unlock_parent(self, row, n):
        """An approved AI child unlocks the owner's step that waited for it."""
        parents = [x.get("id", x.get("record_id")) for x in row.get("父事项") or [] if isinstance(x, dict)]
        for pid in parents:
            parent = next((r for r in self.rows() if r["record_id"] == pid), None)
            if not parent or single(parent.get("状态")) != "卡住" or f"#{n}" not in str(parent.get("卡点") or ""):
                continue
            patch = {"状态": ["未开始"], "卡点": None, "进度": "0/1 可开始"}
            from .prework import _draft_url
            link = _draft_url(row.get("交付物链接"))
            if link and not parent.get("交付物链接"):
                patch["交付物链接"] = link
            self.gateway.update(parent, patch, f"unlock:{pid}:{row['record_id']}", f"reply:{self.source}:{self.ref}")
            return number(parent["编号"])
        return None

    def expand(self, row):
        from .prework import _draft_url
        from .workflow import day
        n = number(row["编号"])
        lines = [f"#{n} {row.get('标题')}",
                 f"{single(row.get('执行方')) or '待定'} · {single(row.get('状态'))} · {row.get('进度') or ''}"]
        due = day(row.get("截止日"))
        if due:
            lines.append(f"截止 {due:%m-%d}" + (f" · 顺延{row.get('顺延次数')}次" if row.get("顺延次数") else ""))
        for label, key in [("卡点", "卡点"), ("完成标准", "完成标准"), ("下一步", "下一步动作"), ("退回意见", "退回意见")]:
            if row.get(key):
                lines.append(f"{label}：{str(row[key]).strip()[:80]}")
        link = _draft_url(row.get("交付物链接"))
        if link:
            lines.append(f"交付物：{link}")
        if row.get("原话引用"):
            lines.append("原话：" + str(row["原话引用"]).strip()[:80])
        return "\n".join(lines)


def handle(config, client, state, text, *, source, message_ref):
    ops, problems = parse(text, _now(config).date())
    if not ops and not problems:
        return None
    applier = Applier(config, client, state, source=source, message_ref=message_ref)
    lines = []
    for op in ops:
        lines.extend(applier.apply(op))
    return "\n".join([*lines, *problems])


# ---- Feishu private chat polling -------------------------------------------

def _chat_id(state):
    found = state.get("inbox:chat_id")
    if found:
        return found
    for (receipt,) in state.db.execute("SELECT receipt FROM operations WHERE receipt LIKE '%chat_id%' ORDER BY at DESC LIMIT 20"):
        try:
            value = json.loads(receipt).get("chat_id")
        except (ValueError, AttributeError):
            continue
        if isinstance(value, str) and value.startswith("oc_"):
            state.set("inbox:chat_id", value)
            return value
    return None


def _message_text(message):
    body = message.get("content") or message.get("body", {}).get("content") or ""
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                body = parsed.get("text", body)
        except ValueError:
            pass
    return re.sub(r"@\S+", "", str(body)).strip()


def _sender(message):
    sender = message.get("sender") or {}
    return sender.get("id") or sender.get("sender_id", {}).get("open_id") or sender.get("open_id")


def poll_inbox(config, client, state, now):
    chat = _chat_id(state)
    if not chat:
        return {"inbox": "no_chat_yet"}
    since = state.get("inbox:cursor") or (now - timedelta(hours=2)).isoformat()
    data = client._exec(["im", "+chat-messages-list", "--chat-id", chat, "--start", since, "--end", now.isoformat(),
                         "--order", "asc", "--no-reactions", "--as", "bot", "--format", "json"])
    messages = data.get("messages") or data.get("items") or []
    handled = 0
    for message in messages:
        mid = message.get("message_id")
        if not mid or _sender(message) != config.owner_open_id or state.get("inbox:seen:" + mid):
            continue
        text = _message_text(message)
        reply = handle(config, client, state, text, source="feishu", message_ref=mid) if text else None
        if reply:
            from .delivery import send_once
            send_once(state, "reply-ack:" + mid, lambda r=reply, m=mid: client.send_self(r, "ack-" + sha256(m.encode()).hexdigest()[:32]))
            handled += 1
        state.set("inbox:seen:" + mid, True)
    state.set("inbox:cursor", now.isoformat())
    return {"inbox": "ok", "messages": len(messages), "handled": handled}


def catch_up_briefs(config, client, state, now):
    """Same-day catch-up for a missed 08:30 / 21:00 brief, once, marked 补发."""
    from . import brief_v1
    done = []
    if now.weekday() >= 5:
        return done
    for edition, slot in (("morning", time(8, 31)), ("evening", time(21, 1))):
        if now.time() < slot or (edition == "morning" and now.time() >= time(12, 0)):
            continue  # a morning brief after noon is no longer useful
        target = now.date() + timedelta(days=edition == "evening")
        op = state.operation(f"brief:{edition}:{target.isoformat()}")
        if op:
            continue
        result = brief_v1.run(config, client, state, edition=edition, now=now)
        done.append({"edition": edition, "sent": result.get("sent"), "message_id": result.get("message_id")})
    return done


def poll(config, client, state, now=None):
    now = now or _now(config)
    result = {"ok": True, "run_model": False, "reason": "idle"}
    beat = state.get("listen:heartbeat")
    listening = bool(beat) and now - datetime.fromisoformat(beat) < timedelta(minutes=3)
    result["listener"] = "live" if listening else "down"
    result.update(poll_inbox(config, client, state, now))
    if state.get("brief-v1:migration-stage") == "v1_active":
        result["catch_up"] = catch_up_briefs(config, client, state, now)
        from .agents import meeting_prep, weekly_review
        # Fallback only: while the listener is live it sends prep cards on time (~30 min before).
        jobs = [("weekly", lambda: weekly_review.run(config, client, state, now))]
        if not listening:
            jobs.insert(0, ("prep", lambda: meeting_prep.run(config, client, state, now)))
        # Each job stands alone: one failing never stops the others.
        for name, job in jobs:
            try:
                result[name] = job()
            except MatrixError as exc:
                result[name] = {"error": str(exc)}
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description="处理本人对事项的回复指令")
    p.add_argument("command", nargs="?", default="text", choices=["text", "poll", "check"])
    p.add_argument("--text")
    p.add_argument("--source", default="cindy", choices=["cindy", "feishu"])
    p.add_argument("--message-ref")
    p.add_argument("--config", type=Path, default=Path("config.yaml"))
    args = p.parse_args(argv)
    path = args.config.resolve()
    try:
        config = load(path)
        with exclusive_lock(path.parent / "state"):
            state = State(path.parent / "state/state.db")
            try:
                client = Feishu(config, path.parent)
                client.verify_owner(client.auth()); require_owned(config, state)
                if args.command in {"poll", "check"}:
                    print(js(poll(config, client, state)))
                    return 2
                if not args.text:
                    raise MatrixError("缺少 --text。")
                ref = args.message_ref or sha256((args.text + _now(config).strftime("%Y%m%d%H%M")).encode()).hexdigest()[:16]
                reply = handle(config, client, state, args.text, source=args.source, message_ref=ref)
                if reply is None:
                    print("NOT_A_COMMAND")
                    return 3
                print(reply)
                return 0
            finally:
                state.close()
    except MatrixBusy:
        print(js({"ok": True, "run_model": False, "reason": "busy"}) if args.command != "text" else "系统正忙，稍后再发一次。")
        return 2 if args.command != "text" else 1
    except (MatrixError, OSError, ValueError) as exc:
        message = str(exc) if isinstance(exc, MatrixError) else f"处理失败（{type(exc).__name__}）"
        print(js({"ok": False, "error": message}) if args.command != "text" else message)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
