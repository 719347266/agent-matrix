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
    # "62 改 换成蓝色": back to the AI in the same conversation, even after it was finished (改到/改成 a date is a reschedule)
    (r"(?:让\s*(?:AI|ai)\s*)?(?:再改|重改|修改|调整|改)(?![到成])\s*[:：]?\s*(.+)", "revise"),
    (r"(?:合并到|并入|并到|和|跟|同)\s*#?(\d{1,5})(?:\s*(?:是)?同一件(?:事)?)?", "merge"),
    (r"(不重复|不是重复|不是同一件(?:事)?|不一样)", "distinct"),
    (r"(?:完成|做完了?|搞定|done)?\s*[，,]?\s*(无交付|不需要交付|不用交付|没有交付|无文件|不用文件|没有文件)", "no_deliverable"),
    (r"(完成|做完了?|搞定|done)", "complete"),
    (r"(不做|取消|不要了)", "cancel"),
    (r"(我来|我做)", "me"),
    (r"(AI做|ai做|交给AI|交给ai|AI来|ai来)", "ai"),
    (r"(AI备|ai备|AI准备|ai准备)", "prep"),
    (r"(先做|优先|插队|先做这个)", "first"),
    (r"(恢复|捞回来|撤销|撤回)", "restore"),
    (r"(继续|接着做|继续做|往下做)", "resume"),
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
                arg = found.group(found.lastindex) if verb in {"reject", "revise", "delegate", "option", "material", "merge"} else ""
                if verb == "reject" and not arg.strip():
                    problems.append(f"#{numbers[0]} 退回的话，要改哪里？例如：{numbers[0]} 退 少了定价")
                    break
                ops.append(Op(numbers, verb, arg.strip(), raw))
                break
        else:
            # "3个会都开完了" is chat; only a #-prefixed or short reply gets a question back.
            if hashed or len(rest) <= 6:
                problems.append(f"没看懂「{raw}」。可回：过 / 退 意见 / 改 要求 / 完成 / 周五 / 不做 / 我来 / AI做 / AI备 / 先做 / 继续 / 展开")
    return ops, problems


def _now(config):
    return datetime.now(ZoneInfo(config.timezone))


def _title(row, n=12):
    t = re.sub(r"\s+", "", str(row.get("标题") or ""))
    return t if len(t) <= n else t[:n - 1] + "…"


def _append(old, line):
    return ((old or "").rstrip() + "\n" + line).strip()


class Applier:
    def __init__(self, config, client, state, *, source, message_ref, immediate=False, progress=None):
        from .workflow import Gateway
        self.config, self.client, self.state = config, client, state
        from .monitor import save_snapshot
        self.gateway = Gateway(client, state, on_rows=(lambda rows: save_snapshot(client.root, rows)) if immediate else None, progress=progress)
        self.immediate = immediate
        self.deferred = []
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

    def stop_ai(self, row):
        """If an AI is working on this item right now, it stops (built-in within seconds, others at their next call)."""
        if self.client is None:
            return ""
        try:
            from .drive import stop_record
            return "，已通知 AI 停止" if stop_record(self.client.root, row["record_id"]) else ""
        except Exception:
            return ""

    def write(self, row, patch, op, n):
        receipt = self.gateway.update(row, patch, self.key(op, n), f"reply:{self.source}:{self.ref}")
        # What the owner says about an item goes into its notebook, so whoever works on it next sees it.
        said = {"reject": "本人退回：", "revise": "本人要改：", "material": "本人补充：", "reschedule": "本人改期到 ", "delegate": "本人交给 "}.get(op.verb)
        if said and self.client is not None:
            try:
                from .prework import add_note
                add_note(self.client.root, row["record_id"], said + op.arg)
            except Exception:
                pass
        return receipt

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
        if getattr(self, "immediate", False) and self.state is not None:
            # A lost HTTP receipt can be retried even if the original transition
            # already changed which actions are allowed (notably 恢复 / 验收).
            from .workflow import matches
            key = self.key(op, n)
            intent = self.state.get(key + ":intent")
            previous = self.state.operation(key)
            if previous and intent and intent.get("record_id") == row["record_id"] and matches(row, intent["patch"]):
                self.gateway.update(row, intent["patch"], key, f"reply:{self.source}:{self.ref}")
                stopped = self.stop_ai(row) if op.verb in {"complete", "no_deliverable", "cancel"} else ""
                return f"#{n} {name} 上次操作已保存 ✓" + stopped
        owner = [{"id": self.config.owner_open_id}]
        if op.verb == "expand":
            return self.expand(row)
        if op.verb == "restore":
            if st not in {"完成", "取消"}:
                return f"#{n} {name} 现在是{st}，不用恢复。"
            self.write(row, {"状态": ["待确认"], "进度": "0/1 待确认", "完成时间": None, "交付情况": None,
                             "卡点": None, "验收结果": None,
                             "补充材料": _append(row.get("补充材料"), f"{self.now.isoformat()} 本人恢复：重新确认这件事")}, op, n)
            if self.client is not None:
                from .thread import forget_judgment
                forget_judgment(self.client.root, row["record_id"])
            return f"#{n} {name} → 恢复了，回到「新进来」等你定 ✓"
        if op.verb == "revise":
            if st == "取消":
                return f"#{n} {name} 已经标为不做。要接着做，先点「恢复」。"
            patch = {"状态": ["进行中"], "执行方": ["AI"], "进度": "按意见修改", "卡点": None,
                     "退回意见": _append(row.get("退回意见"), f"{self.now:%m-%d} {op.arg}")}
            if st in {"完成", "待验收"}:
                patch.update({"验收结果": ["退回"], "完成时间": None})
            self.write(row, patch, op, n)
            return f"#{n} {name} → AI 在原来的对话里接着改，改好出新版本 ✓"
        if st == "取消" and op.verb == "cancel":
            return f"#{n} {name} 已经标为不做 ✓" + self.stop_ai(row)
        if st in {"完成", "取消"} and op.verb not in {"reschedule", "no_deliverable", "material", "complete"}:
            return f"#{n} {name} 已经是{st}，没动。"
        if st == "取消" and op.verb in {"no_deliverable", "material", "complete"}:
            return f"#{n} {name} 已经取消了，没动。"
        if op.verb == "approve":
            if st != "待验收":
                return f"#{n} {name} 现在是{st}，还没交付，不能验收。"
            from .prework import _draft_url
            self.write(row, {"状态": ["完成"], "验收结果": ["通过"], "完成时间": self.now.isoformat(), "进度": "完成",
                             "交付情况": ["有交付" if _draft_url(row.get("交付物链接")) else "待补交付"]}, op, n)
            unlocked = self.unlock_parent(row, n)
            return f"#{n} {name} → 完成 ✓" + (f"；#{unlocked} 可以开始了" if unlocked else "")
        if op.verb == "reject":
            if st != "待验收":
                return f"#{n} {name} 现在是{st}，没有待验收的结果可退。"
            self.write(row, {"状态": ["进行中"], "验收结果": ["退回"], "进度": "按意见修改",
                             "退回意见": _append(row.get("退回意见"), f"{self.now:%m-%d} {op.arg}")}, op, n)
            return f"#{n} {name} → 退回修改 ✓"
        if op.verb in {"complete", "no_deliverable"}:
            return self.complete(row, op, n, name, st, no_file=op.verb == "no_deliverable") + self.stop_ai(row)
        if op.verb == "cancel":
            self.write(row, {"状态": ["取消"], "进度": "不做"}, op, n)
            return f"#{n} {name} → 不做了 ✓" + self.stop_ai(row)
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
        if op.verb == "material" and (st == "完成" or single(row.get("交付情况")) == "待补交付"):
            link = re.search(r"https?://\S+", op.arg)
            if not link:
                return f"#{n} {name} 已完成，补交付请回「{n} + 文件链接」，或「{n} 无交付」。"
            self.write(row, {"交付物链接": link.group(0), "交付情况": ["有交付"]}, op, n)
            return f"#{n} {name} → 交付物已挂上 ✓"
        if op.verb == "material":
            patch = {"补充材料": _append(row.get("补充材料"), f"{self.now:%m-%d} {op.arg}")}
            if st == "卡住" and ex == "AI":
                patch.update({"状态": ["进行中"], "卡点": None, "进度": "材料已补，继续做"})
            self.write(row, patch, op, n)
            return f"#{n} {name} → 材料已记下 ✓" + ("，AI 接着做" if patch.get("状态") else "")
        if op.verb == "merge":
            return self.merge(row, op, n, name, int(op.arg))
        if op.verb == "distinct":
            from .merge import duplicate_pairs
            marked = self.state.get("merge:distinct", []) if self.state else []
            keys = [p["key"] for p in duplicate_pairs(self.rows(), marked) if n in (p["keep"], p["drop"])]
            if self.state:
                self.state.set("merge:distinct", sorted(set(marked) | set(keys)))
            return f"#{n} {name} → 记下了，不是重复，以后不再提示 ✓"
        if op.verb in {"first", "resume"}:
            if ex != "AI":
                return f"#{n} {name} 不是 AI 在做。要交给 AI，回「{n} AI做」。"
            if op.verb == "resume":
                patch = {"补充材料": _append(row.get("补充材料"), f"{self.now:%m-%d} 本人：继续做，按现有资料先做出来")}
                if st == "卡住":
                    patch.update({"状态": ["进行中"], "卡点": None, "进度": "继续做"})
                self.write(row, patch, op, n)
            if self.state is not None:
                first = [x for x in self.state.get("work:first", []) if x != row["record_id"]]
                self.state.set("work:first", [row["record_id"], *first] if op.verb == "first" else first)
            return f"#{n} {name} → " + ("AI 下一件就做 ✓" if op.verb == "first" else "AI 接着做 ✓")
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

    def complete(self, row, op, n, name, st, *, no_file):
        """完成 = the work is done; then settle the deliverable: attached, not needed, or still missing."""
        from .prework import _draft_url
        patch = {} if st == "完成" else {"状态": ["完成"], "完成时间": self.now.isoformat(), "进度": "完成"}
        if no_file:
            patch["交付情况"] = ["无交付"]; note = "，标为无交付"
        elif _draft_url(row.get("交付物链接")):
            patch["交付情况"] = ["有交付"]; note = "，交付物已在"
        elif not expects_deliverable(row.get("标题")):
            patch["交付情况"] = ["无交付"]; note = "（行动项，不需要交付文件）"
        elif getattr(self, "immediate", False):
            patch["交付情况"] = ["待补交付"]
            note = "，交付文件在后台查找，可以稍后补上"
        else:
            found = find_deliverable(self.client, self.config, row.get("标题"), self.now)
            if found and found.get("candidates"):
                patch["交付情况"] = ["待补交付"]
                options = "；".join(f"《{c['title'][:22]}》{c['url']}" for c in found["candidates"])
                note = f"。交付文件可能是：{options}。是哪份就回「{n} + 链接」，没有文件回「{n} 无交付」"
            elif found:
                patch.update({"交付物链接": found["url"], "交付情况": ["有交付"]})
                note = f"，交付物：《{found['title'][:24]}》（自动找到，不对回「{n} + 正确链接」）"
            else:
                patch["交付情况"] = ["待补交付"]
                note = f"。交付文件没找到：回「{n} + 链接」补上，没有文件就回「{n} 无交付」"
        if st == "完成" and single(row.get("交付情况")) == single(patch.get("交付情况")):
            return f"#{n} {name} 已经完成 ✓"
        receipt = self.write(row, patch, op, n)
        if getattr(self, "immediate", False) and patch.get("交付情况") == ["待补交付"]:
            self.deferred.append(receipt.get("readback") or {**row, **patch})
        return f"#{n} {name} → 完成 ✓{note}"

    def merge(self, row, op, n, name, into):
        """「31 合并到 3」: #31's facts go into #3 (never overwriting #3's owner/date), #31 is closed as merged."""
        from .merge import merge_into
        from .prework import _draft_url
        if into == n:
            return f"#{n} 不能合并到自己。"
        target = self.find(into)
        if not target or single(target.get("状态")) in {"完成", "取消"}:
            return f"#{into} 找不到或已经结束了，没合并。"
        detail = str(row.get("下一步动作") or row.get("原话引用") or "")[:120]
        merge_into(self.gateway, target, title=str(row.get("标题")), source=f"#{n} 手动合并", detail=detail,
                   task_link=row.get("关联任务链接"), deliverable=_draft_url(row.get("交付物链接")),
                   key=f"merge:{row['record_id']}:{target['record_id']}", now=self.now)
        self.write(row, {"状态": ["取消"], "进度": f"并入#{into}", "卡点": None, "排除本人提醒": True}, op, n)
        return f"#{n} {name} → 已并入 #{into} ✓（原信息记在 #{into} 的补充材料里）"

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
        try:
            from .prework import read_notes
            notes = read_notes(self.client.root, row["record_id"], last=3) if self.client is not None else ""
        except Exception:
            notes = ""
        if notes:
            lines.append("最近进展：\n" + notes)
        return "\n".join(lines)


ACTION_START = re.compile(r"^(确认|跟进|联系|找|约|沟通|对齐|安排|验收|回复|发给|提醒|催|通知|参加|开会|面试|交接|协调|推进)")
DELIVERABLE = re.compile(r"文档|方案|需求|报告|文案|设计|稿|清单|材料|复盘|周报|PPT|ppt|表格|计划|策略|预览|海报|视频|脚本|总结|纪要|调研|分析|帖子|原型|图")


def expects_deliverable(title):
    """Does finishing this naturally leave a file behind? Action items (确认/跟进…) do not."""
    core = re.sub(r"^(备材料|准备|撰写|整理|输出|完成|补齐|细化)[：:]?", "", str(title or "")).strip()
    if ACTION_START.search(str(title or "")) and not str(title or "").startswith("备材料"):
        return False
    return bool(DELIVERABLE.search(core))


def _common(a, b):
    """Length of the longest common substring (titles are short)."""
    best = 0
    for i in range(len(a)):
        for j in range(i + best + 1, len(a) + 1):
            if a[i:j] in b:
                best = j - i
            else:
                break
    return best


def find_deliverable(client, config, title, now, days=21):
    """The owner's own document, edited recently, whose title clearly matches the task; else None."""
    if client is None:
        return None
    core = re.sub(r"^(备材料|准备|撰写|整理|输出|完成|补齐|细化)[：:]?", "", str(title or ""))
    core = re.sub(r"[（(].*?[)）]|\s+", "", core)[:14]
    if len(core) < 2:
        return None
    # Feishu search is literal: widen step by step until something comes back.
    generic = re.sub(r"(需求文档|文档|方案|需求|计划|材料|清单|报告|撰写|输出)+$", "", core)
    queries = list(dict.fromkeys(q for q in [core, generic, generic[:6], generic[:4]] if len(q) >= 2))
    results, seen = [], set()
    for q in queries:  # pool every round: an early, loosely related hit must not stop the search
        try:
            data = client._exec(["docs", "+search", "--query", q, "--page-size", "15", "--as", "user", "--format", "json"])
        except MatrixError:
            return None
        for r in data.get("results") or []:
            url = (r.get("result_meta") or {}).get("url")
            if url and url not in seen:
                seen.add(url); results.append(r)
    # Score on the subject words only: verbs like 撰写/准备 never appear in document titles.
    subject = re.sub(r"撰写|准备|整理|输出|补齐|细化|完成|跟进|核对|优化|和|与|及|的", "", core)
    pairs = {subject[i:i + 2] for i in range(len(subject) - 1)}
    scored = []
    for r in results:
        m = r.get("result_meta") or {}
        doc_title = re.sub(r"<[^>]+>", "", r.get("title_highlighted") or "")
        if doc_title.startswith(("智能纪要", "文字记录")) or not m.get("url"):
            continue  # meeting notes are not deliverables
        if config.owner_open_id not in {m.get("owner_id"), m.get("edit_user_id")}:
            continue
        try:
            updated = datetime.fromisoformat(m.get("update_time_iso") or "")
        except ValueError:
            continue
        if now - updated > timedelta(days=days):
            continue
        hit = sum(1 for pair in pairs if pair in doc_title)
        cover = hit / len(pairs) if pairs else 0
        if hit >= 2 and cover >= 0.4:
            scored.append((round(cover, 2), m.get("edit_user_id") == config.owner_open_id, updated, doc_title, m["url"]))
    scored.sort(reverse=True)
    if not scored:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.15:
        # Two similar candidates: show both and let the owner pick, never guess.
        return {"candidates": [{"title": x[3], "url": x[4]} for x in scored[:2]]}
    return {"title": scored[0][3], "url": scored[0][4]}


WAKE_WORKER = {"material", "reject", "revise", "ai", "first", "resume", "prep"}


def handle(config, client, state, text, *, source, message_ref):
    ops, problems = parse(text, _now(config).date())
    if not ops and not problems:
        return None
    applier = Applier(config, client, state, source=source, message_ref=message_ref)
    lines = []
    for op in ops:
        lines.extend(applier.apply(op))
    from . import activity
    for line in lines:
        activity.log(client.root, "reply", f"你回复「{text.strip()[:20]}」→ {line[:60]}", status="ok" if "✓" in line else "info")
    if any(op.verb in WAKE_WORKER for op in ops) and any("✓" in x for x in lines):
        try:  # new material, a rejection or "先做": the AI employee starts now, not on the next tick
            from .worker import kick
            kick(client.root, config, force=True)
        except Exception:
            pass
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
        from contextlib import nullcontext
        from .itemstore import enabled
        local = args.command == "text" and enabled(config)  # local items: no Feishu round trip, no queueing
        with nullcontext() if local else exclusive_lock(path.parent / "state", wait=0 if args.command != "text" else 45):
            state = State(path.parent / "state/state.db")
            try:
                client = Feishu(config, path.parent)
                if not local:
                    client.verify_owner(client.auth())
                require_owned(config, state)
                if args.command in {"poll", "check"}:
                    result = poll(config, client, state)
                    from . import activity
                    activity.beat(path.parent, "回复轮询", f"处理 {result.get('handled', 0)} 条")
                    print(js(result))
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
        from . import activity
        activity.log(path.parent, "error", f"回复处理（{args.command}）出错", detail=message, status="failed")
        print(js({"ok": False, "error": message}) if args.command != "text" else message)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
