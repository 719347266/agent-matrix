"""Friday 17:00: what got done, what slipped and why (from the record, not guessed), next week.

Pure facts from the task table and calendar; no model. Sent once per week.
"""
from datetime import date, datetime, time, timedelta
import json
import re

from ..brief_migration import number, single

WEEKDAYS = "一二三四五六日"


def _history(row):
    out = []
    for line in str(row.get("顺延历史") or "").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def why(row):
    """The recorded reason an item slipped. Only facts that are written down."""
    st, ex = single(row.get("状态")), single(row.get("执行方"))
    blocker = str(row.get("卡点") or "")
    if st == "卡住":
        m = re.search(r"#(\d+)", blocker)
        return f"等你先看 AI 的 #{m.group(1)}" if m and ex == "我" else f"卡住：{blocker[:18]}"
    if ex == "别人":
        return "等对方交付"
    sources = {h.get("source") for h in _history(row)}
    if "owner" in sources and "evening" not in sources:
        return "你主动改了日期"
    if "evening" in sources:
        return "到期没做完，自动顺延"
    return "日期被往后改过"


def _metric(value, label, color="blue"):
    return {"tag": "column", "width": "weighted", "weight": 1, "background_style": "grey-50", "padding": "8px",
            "vertical_spacing": "2px", "elements": [
                {"tag": "markdown", "content": f"## <font color='{color}'>{value}</font>", "text_align": "center"},
                {"tag": "markdown", "content": f"<font color='grey'>{label}</font>", "text_align": "center", "text_size": "notation"}]}


def build(config, rows, next_week_events, today):
    from .. import brief_v1
    from ..workflow import day
    monday = today - timedelta(days=today.weekday())
    week = (monday, monday + timedelta(days=6))
    nxt = (monday + timedelta(days=7), monday + timedelta(days=13))
    seen = [r for r in rows if brief_v1.visible(r) and not str(r.get("去重键") or "").startswith("test:")]
    in_week = lambda d: d is not None and week[0] <= d <= week[1]
    done = [r for r in seen if single(r.get("状态")) == "完成" and in_week(day(r.get("完成时间")))]
    created = [r for r in seen if in_week(day(r.get("创建时间")))]
    open_ = [r for r in seen if single(r.get("状态")) not in {"完成", "取消"}]
    slipped = [r for r in open_ if int(r.get("顺延次数") or 0) > 0]
    waiting = [r for r in open_ if single(r.get("状态")) == "待验收"]
    due_next = [r for r in open_ if day(r.get("截止日")) and nxt[0] <= day(r.get("截止日")) <= nxt[1]]

    metrics = {"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px", "columns": [
        _metric(len(done), "本周完成", "green"), _metric(len(created), "本周新增"),
        _metric(len(slipped), "延期中", "red" if slipped else "blue"), _metric(len(waiting), "等你验收")]}
    blocks = [metrics]
    if slipped:
        slipped.sort(key=lambda r: (-int(r.get("顺延次数") or 0), number(r["编号"])))
        items = []
        for r in slipped[:5]:
            count = int(r.get("顺延次数") or 0)
            first, now_due = day(r.get("首次截止日")), day(r.get("截止日"))
            span = f"{first:%m-%d} → {now_due:%m-%d}" if first and now_due and first != now_due else ""
            hint = " · ".join(x for x in [f"顺延 {count} 次", span, why(r)] if x)
            items.append(brief_v1._row_item((f"#{number(r['编号'])} {brief_v1.short(r.get('标题'), 22)}", hint,
                                             "red" if count >= 2 else "grey", None)))
        blocks.append(brief_v1._block("这周延期了的事", "red", items, max(0, len(slipped) - 5)))
    if due_next:
        due_next.sort(key=lambda r: (day(r.get("截止日")), number(r["编号"])))
        items = [brief_v1._row_item((f"#{number(r['编号'])} {brief_v1.short(r.get('标题'), 22)}",
                                     f"周{WEEKDAYS[day(r['截止日']).weekday()]} 到期 · " +
                                     {"我": "你来做", "AI": "AI 来做", "别人": "等对方"}.get(single(r.get("执行方")), "待定"),
                                     "grey", None)) for r in due_next[:5]]
        blocks.append(brief_v1._block(f"下周要到期 · {len(due_next)} 件", "blue", items, max(0, len(due_next) - 5)))
    if next_week_events:
        per_day = {}
        minutes = 0
        for e in next_week_events:
            st, en = (e.get("start_time") or {}).get("datetime"), (e.get("end_time") or {}).get("datetime")
            if not st or not en or (e.get("self_rsvp_status") or "").lower() in {"decline", "declined"}:
                continue
            s, f = datetime.fromisoformat(st), datetime.fromisoformat(en)
            per_day[s.date()] = per_day.get(s.date(), 0) + 1
            minutes += (f - s).total_seconds() / 60
        if per_day:
            busiest = max(per_day, key=per_day.get)
            text = (f"下周 {sum(per_day.values())} 场会，共 {minutes/60:.1f} 小时；"
                    f"最满的是周{WEEKDAYS[busiest.weekday()]}（{per_day[busiest]} 场）")
            blocks.append(brief_v1._block("下周的会", "grey", [{"tag": "markdown", "content": text}]))
    sample = next((number(r["编号"]) for r in slipped), None) or next((number(r["编号"]) for r in open_), 1)
    hint = f"延期 2 次以上的，要么改计划，要么不做：回「{sample} 下周一」或「{sample} 不做」"
    title = f"本周复盘 · {week[0]:%m/%d}–{week[1]:%m/%d}"
    subtitle = f"完成 {len(done)} 件 · 新增 {len(created)} 件 · 延期中 {len(slipped)} 件"
    card = brief_v1.card(title, subtitle, "indigo", blocks, hint, None, summary=f"{title}：{subtitle}")
    card["header"]["icon"] = {"tag": "standard_icon", "token": "todo_colorful"}
    return card


def due(now, state):
    """Friday from 17:00 until midnight, once per ISO week."""
    key = "weekly:%d-W%02d" % now.isocalendar()[:2]
    return key if now.weekday() == 4 and now.time() >= time(17, 0) and not state.operation(key) else None


def run(config, client, state, now, *, preview=False):
    from ..delivery import send_once
    from ..workflow import Gateway
    key = due(now, state)
    if not key and not preview:
        return {"sent": False, "reason": "not_friday_17"}
    rows = Gateway(client, state).rows()
    monday = now.date() - timedelta(days=now.weekday()) + timedelta(days=7)
    start = datetime.combine(monday, time(), now.tzinfo)
    events = client._exec(["calendar", "+agenda", "--start", start.isoformat(),
                           "--end", (start + timedelta(days=5)).isoformat(), "--as", "user", "--format", "json"])
    events = events if isinstance(events, list) else events.get("events") or []
    card = build(config, rows, events, now.date())
    if preview:
        return {"sent": False, "card": card}
    receipt = send_once(state, key, lambda: client.send_card(card, key.replace(":", "-")))
    return {"sent": True, "message_id": receipt.get("message_id")}
