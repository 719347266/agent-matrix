"""Deterministic, read-only daily plans; only delivery has a persistent effect."""
from datetime import date, datetime, time, timedelta
import math
from zoneinfo import ZoneInfo

from ..bitable import find_value
from ..models import MatrixError


FIELDS = ["标题", "方向", "负责人", "截止日", "下一步动作", "状态", "优先级",
          "预计耗时(分钟)", "来源链接", "更新时间", "AI草稿链接"]
SUMMARY_FIELD = "预做结果摘要"
DONE = {"完成", "取消"}
ACTIONABLE = {"待办", "进行中"}


def _text(value):
    if value is None:
        return ""
    if isinstance(value, list):
        return "、".join(filter(None, (_text(x) for x in value)))
    if isinstance(value, dict):
        return _text(value.get("name") or value.get("text") or value.get("url") or "")
    return " ".join(str(value).split())


def _link(value):
    if isinstance(value, dict):
        value = value.get("url", "")
    if isinstance(value, list):
        return next((url for item in value if (url := _link(item))), "")
    value = str(value or "").strip()
    return value if value.startswith(("https://", "http://")) else ""


def _timestamp(value, tz):
    """Feishu dates are native ISO strings or Unix milliseconds; never invent a date."""
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            if not math.isfinite(value):
                return None
            return datetime.fromtimestamp(value / 1000, tz)
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, date):
            parsed = datetime.combine(value, time())
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=tz) if parsed.tzinfo is None else parsed.astimezone(tz)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _minutes(value):
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _merge(intervals):
    merged = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _length(intervals):
    return sum((end - start).total_seconds() / 60 for start, end in _merge(intervals))


def _clip(intervals, start, end):
    return [(max(a, start), min(b, end)) for a, b in intervals if b > start and a < end]


def _fmt(number):
    return f"{number:g}"


def _short(value, limit=90):
    return value if len(value) <= limit else value[:limit] + "…"


def _review_lines(rows, *, limit=None, compact=False, summary_limit=240):
    if not rows:
        return []
    displayed = rows if limit is None else rows[:limit]
    lines = ["", f"结果待确认 {len(rows)} 项（草稿已备，不代表业务完成）"]
    for row in displayed:
        title = _short(row["title"]) if compact else row["title"]
        lines.append("◦ " + title)
        if row["draft_summary"]:
            lines.append("  已备内容：" + _short(row["draft_summary"], summary_limit))
        else:
            lines.append("  结果摘要尚未提供，具体内容需查看草稿。")
        if row["draft_url"] and (not compact or len(row["draft_url"].encode("utf-8")) <= 350):
            lines.append("  查看草稿：" + row["draft_url"])
        elif row["draft_url"]:
            lines.append("  草稿链接较长，完整链接保留在事项表。")
        else:
            lines.append("  草稿链接尚未提供，预做结果仍待核验。")
    if len(rows) > len(displayed):
        lines.append(f"另有 {len(rows) - len(displayed)} 项结果待确认未展开。")
    lines.append("这些事项未计入重复执行工时；确认后的业务状态仍需单独更新。")
    return lines


def _compact(text, result, config):
    """Keep one bounded delivery; explicitly report omitted rows rather than hiding them."""
    if len(text.encode("utf-8")) <= 18000:
        return text, False
    calendar, pending, completed = result["calendar"], result["pending"], result["recent_completed"]
    lines = [text.splitlines()[0], "本条为合并摘要；详细列表较长，以下注明未展开条数。", "", "日程"]
    event_groups = [("已确认", calendar["accepted"]), ("参会待确认", calendar["tentative"]),
                    ("全天提醒，不按会议扣时", calendar["all_day"]), ("时间待确认", calendar["warnings"])]
    for label, entries in event_groups:
        if entries:
            lines.append(f"{label}：共 {len(entries)} 项")
        for entry in entries[:3]:
            lines.append(f"• {entry.get('time', '')} {_short(entry['title'])}")
            if entry["source_url"] and len(entry["source_url"].encode("utf-8")) <= 350:
                lines.append("  来源：" + entry["source_url"])
        if len(entries) > 3:
            lines.append(f"另有 {len(entries) - 3} 项未展开。")
    if not any(entries for _, entries in event_groups):
        lines.append("当前日历未查到该日日程。")
    review = result["awaiting_review"]
    lines.extend(_review_lines(review, limit=3, compact=True, summary_limit=100))
    focus = result["focus"]
    lines.extend(["", ("今日重点" if result["edition"] == "morning" else "明日重点") + "（最多 3 项本人可执行事项）",
                  f"事项表未完成 {len(pending)} 项（按逾期、当日到期、P0、近期到期排序）"])
    for number, row in enumerate(focus, 1):
        lines.append(f"□ {number}. {_short(row['title'])}｜{row['reason']}｜{row['status'] or '状态待确认'}"
                     f"｜截止 {row['due_date'] or '待确认'}")
        if row["source_url"] and len(row["source_url"].encode("utf-8")) <= 350:
            lines.append("  来源：" + row["source_url"])
    other_count = len(pending) - len(focus) - len(review)
    if other_count:
        lines.append(f"另有 {other_count} 项未展开，未将这些事项标为完成。")
    if not pending:
        lines.append("当前事项表无待办；这不代表没有其他工作。")
    lines.extend(["", "时间安排", text.split("\n时间安排\n", 1)[1].split("\n\n", 1)[0]])
    if completed:
        lines.extend(["", f"近期完成 {len(completed)} 项（按更新时间筛选，不等同于实际完成时刻）"])
        lines.extend("✓ " + _short(row["title"]) for row in completed[:3])
        if len(completed) > 3:
            lines.append(f"另有 {len(completed) - 3} 项未展开。")
    missing = result["confirmations"]
    if missing:
        lines.extend(["", f"有 {len(missing)} 项缺信息或待确认。"])
        lines.extend(f"? {_short(row['title'])}：{'、'.join(row['fields'])}" for row in missing[:3])
        if len(missing) > 3:
            lines.append(f"另有 {len(missing) - 3} 项待确认未展开。")
    base_url = _link(getattr(config, "base_url", ""))
    if base_url and len(base_url.encode("utf-8")) <= 1000:
        lines.extend(["", "完整事项表：" + base_url])
    else:
        lines.extend(["", "可在 Cindy 对话里回复“展开简报”，查看未展开内容。"])
    lines.append("可在 Cindy 对话里回复事项名称和变化。")
    if len("\n".join(lines).encode("utf-8")) > 18000:
        # Keep the new result links directly usable even on especially busy days.
        lines = [line for line in lines if not line.startswith("  来源：")]
        lines.append("部分来源链接未展开，可在 Cindy 对话里查看完整简报。")
    return "\n".join(lines), True


def _item(row, config, target_date, tz):
    raw = row.get("fields", row)
    due = _timestamp(raw.get("截止日"), tz)
    due_date = due.date() if due else None
    status = _text(raw.get("状态"))
    priority = _text(raw.get("优先级"))
    direction = _text(raw.get("方向"))
    owners = raw.get("负责人") or []
    owners = owners if isinstance(owners, list) else [owners]
    owner_ids = {x.get("id", x.get("open_id")) for x in owners if isinstance(x, dict)}
    owner_id = getattr(config, "owner_open_id", "")
    mine = direction == "我的" or bool(owner_id and owner_id in owner_ids)
    if direction in {"别人的", "别人答应我的"}:
        mine = False
    reason = "稍后安排"
    rank = 4
    if due_date and due_date < target_date:
        rank, reason = 0, "逾期"
    elif due_date == target_date:
        rank, reason = 1, "当日到期"
    elif priority == "P0":
        rank, reason = 2, "P0"
    elif due_date and due_date <= target_date + timedelta(days=3):
        rank, reason = 3, "3 天内到期"
    missing = []
    if not owners:
        missing.append("负责人")
    if not due:
        missing.append("截止日" if not raw.get("截止日") else "有效截止日")
    if not status:
        missing.append("状态")
    elif status == "待确认":
        missing.append("事项确认")
    elif status not in ACTIONABLE | DONE | {"阻塞", "草稿已备"}:
        missing.append("有效状态")
    if not direction and not (owner_id and owner_id in owner_ids):
        missing.append("归属")
    draft_url = _link(raw.get("AI草稿链接"))
    if status == "草稿已备" and not draft_url:
        missing.append("草稿链接")
    return {"record_id": row.get("record_id", ""), "title": _text(raw.get("标题")) or "未命名事项",
            "status": status, "priority": priority, "direction": direction, "mine": mine,
            "owners": _text(owners), "due_date": due_date.isoformat() if due_date else None,
            "next_action": _text(raw.get("下一步动作")), "minutes": _minutes(raw.get("预计耗时(分钟)")),
            "source_url": _link(raw.get("来源链接")), "missing": missing,
            "draft_url": draft_url, "draft_summary": _text(raw.get(SUMMARY_FIELD)),
            "updated_at": _timestamp(raw.get("更新时间"), tz), "rank": rank, "reason": reason}


def _sort_key(item):
    return (item["rank"], item["due_date"] or "9999-12-31",
            {"P0": 0, "P1": 1, "P2": 2}.get(item["priority"], 3), item["record_id"], item["title"])


def _calendar(events, target_date, tz):
    day_start = datetime.combine(target_date, time(), tz)
    day_end = day_start + timedelta(days=1)
    accepted, tentative, all_day, warnings, intervals = [], [], [], [], []
    for event in events:
        if not isinstance(event, dict):
            raise MatrixError("日程响应含无效记录，未生成不完整简报。")
        status = _text(event.get("status")).lower()
        rsvp = _text(event.get("self_rsvp_status")).lower()
        if status in {"cancelled", "canceled"} or rsvp in {"decline", "declined"}:
            continue
        entry = {"event_id": _text(event.get("event_id")),
                 "title": _text(event.get("summary")) or "未命名日程",
                 "source_url": _link(event.get("app_link")),
                 "confirmed": rsvp in {"accept", "accepted"}}
        start_info, end_info = event.get("start_time") or {}, event.get("end_time") or {}
        if not isinstance(start_info, dict) or not isinstance(end_info, dict):
            warnings.append({**entry, "reason": "日程时间格式待确认"})
            continue
        if start_info.get("date"):
            try:
                first = date.fromisoformat(start_info["date"])
                last = date.fromisoformat(end_info.get("date") or start_info["date"])
            except (TypeError, ValueError):
                warnings.append({**entry, "reason": "全天日程日期待确认"})
                continue
            if last < first:
                warnings.append({**entry, "reason": "全天日程起止日期待确认"})
                continue
            # +agenda already scopes this response to the target day. Preserve its
            # dates without guessing whether the provider's end date is inclusive.
            entry.update(date_start=first.isoformat(), date_end=last.isoformat())
            all_day.append(entry)
            continue
        start = _timestamp(start_info.get("datetime"), tz)
        end = _timestamp(end_info.get("datetime"), tz)
        if not start or not end or end <= start:
            warnings.append({**entry, "reason": "开始或结束时间待确认"})
            continue
        if end <= day_start or start >= day_end:
            continue
        start, end = max(start, day_start), min(end, day_end)
        entry.update(start=start.isoformat(), end=end.isoformat(),
                     time=f"{start:%H:%M}–{'24:00' if end == day_end else end.strftime('%H:%M')}")
        if entry["confirmed"]:
            accepted.append(entry)
            intervals.append((start, end))
        else:
            tentative.append(entry)
    by_time = lambda entry: (entry.get("start", ""), entry["event_id"], entry["title"])
    return {"accepted": sorted(accepted, key=by_time), "tentative": sorted(tentative, key=by_time),
            "all_day": sorted(all_day, key=by_time), "warnings": sorted(warnings, key=by_time),
            "confirmed_meeting_minutes": _length(intervals)}, intervals


def _work_clock(value, target_date, tz):
    try:
        if value == "24:00":
            return datetime.combine(target_date + timedelta(days=1), time(), tz)
        parsed = time.fromisoformat(value)
        if parsed.tzinfo or parsed.second or parsed.microsecond:
            raise ValueError
        return datetime.combine(target_date, parsed, tz)
    except (TypeError, ValueError):
        raise MatrixError("工作时段或休息时段需使用 HH:MM 格式。") from None


def _capacity(config, target_date, tz, intervals, pending):
    # A deadline alone does not make somebody else's work part of the owner's capacity.
    planned = [x for x in pending if x["mine"] and x["status"] in ACTIONABLE and x["rank"] <= 2]
    known = sum(x["minutes"] for x in planned if x["minutes"] is not None)
    unknown = sum(x["minutes"] is None for x in planned)
    result = {"configured": False, "scope": "本人逾期、当日到期及 P0 可执行事项",
              "planned_record_ids": [x["record_id"] for x in planned],
              "known_task_minutes": known, "unknown_task_count": unknown,
              "available_minutes": None, "planning_limit_minutes": None,
              "exceeds_planning_limit": None}
    start_value, end_value = getattr(config, "work_start", None), getattr(config, "work_end", None)
    if not start_value or not end_value:
        return result
    start = _work_clock(start_value, target_date, tz)
    end = _work_clock(end_value, target_date, tz)
    if end <= start or start.date() != target_date:
        raise MatrixError("工作时段需在目标日内，且结束时间须晚于开始时间。")
    breaks = []
    for pair in getattr(config, "work_breaks", []):
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise MatrixError("每个休息时段须包含开始和结束时间。")
        a, b = (_work_clock(v, target_date, tz) for v in pair)
        if b <= a:
            raise MatrixError("休息时段的结束时间须晚于开始时间。")
        breaks.extend(_clip([(a, b)], start, end))
    meetings = _clip(intervals, start, end)
    work_minutes = (end - start).total_seconds() / 60
    breaks_minutes = _length(breaks)
    occupied = _length([*breaks, *meetings])
    available = work_minutes - occupied
    limit = available * 0.7
    result.update(configured=True, work_minutes=work_minutes, break_minutes=breaks_minutes,
                  meeting_minutes_excluding_breaks=occupied - breaks_minutes,
                  available_minutes=available, planning_limit_minutes=round(limit, 2),
                  exceeds_planning_limit=known > limit)
    return result


def build_brief(config, items: list[dict], events: list[dict], target_date: date, edition: str) -> dict:
    """Build JSON-safe calculation details and a single readable message, without IO."""
    if edition not in {"morning", "evening"}:
        raise MatrixError("简报版本仅支持 morning 或 evening。")
    if not isinstance(target_date, date) or isinstance(target_date, datetime):
        raise MatrixError("简报目标日期须为日期。")
    if not isinstance(items, list) or not all(isinstance(row, dict) for row in items):
        raise MatrixError("事项响应格式异常。")
    if not isinstance(events, list):
        raise MatrixError("日程响应不是完整列表。")
    tz = ZoneInfo(config.timezone)
    decoded = [_item(row, config, target_date, tz) for row in items]
    # Exclude explicitly unrelated work before every section and count is built.
    # Promises to the owner and unknown ownership still need follow-up.
    decoded = [row for row in decoded if row["direction"] != "别人的"]
    pending = sorted((row for row in decoded if row["status"] not in DONE), key=_sort_key)
    focus = [row for row in pending if row["mine"] and row["status"] in ACTIONABLE][:3]
    review = [row for row in pending if row["status"] == "草稿已备"]
    # With no dedicated completion timestamp, disclose the weaker update-time evidence.
    cutoff = datetime.combine(target_date, time(), tz)
    completed = sorted((row for row in decoded if row["status"] == "完成" and row["updated_at"]
                        and cutoff - timedelta(days=1) <= row["updated_at"] < cutoff + (
                            timedelta(days=1) if edition == "morning" else timedelta())),
                       key=lambda row: (row["updated_at"], row["record_id"], row["title"]), reverse=True)
    calendar, intervals = _calendar(events, target_date, tz)
    capacity = _capacity(config, target_date, tz, intervals, pending)
    confirmations = [{"record_id": row["record_id"], "title": row["title"], "fields": row["missing"]}
                     for row in pending if row["missing"]]
    label = "今日早报" if edition == "morning" else "明日计划"
    lines = [f"【{label}｜{target_date.isoformat()} 周{'一二三四五六日'[target_date.weekday()]}】", "", "日程"]
    if not any(calendar[key] for key in ("accepted", "tentative", "all_day", "warnings")):
        lines.append("当前日历未查到该日日程。")
    for event in calendar["accepted"]:
        lines.append(f"• {event['time']} {event['title']}" + (f"\n  来源：{event['source_url']}" if event["source_url"] else ""))
    for event in calendar["tentative"]:
        lines.append(f"? {event['time']} {event['title']}｜参会待确认，未计入已安排工时" +
                     (f"\n  来源：{event['source_url']}" if event["source_url"] else ""))
    for event in calendar["all_day"]:
        lines.append(f"• 全天提醒：{event['title']}（不按会议扣时" +
                     ("；参会待确认" if not event["confirmed"] else "") + "）" +
                     (f"\n  来源：{event['source_url']}" if event["source_url"] else ""))
    for event in calendar["warnings"]:
        lines.append(f"? {event['title']}｜{event['reason']}，未计入工时" +
                     (f"\n  来源：{event['source_url']}" if event["source_url"] else ""))
    lines.extend(_review_lines(review, limit=3 if edition == "morning" else None,
                               summary_limit=160 if edition == "morning" else 600))
    lines.extend(["", ("今日重点" if edition == "morning" else "明日重点") + "（最多 3 项本人可执行事项）",
                  "顺序依据：逾期 → 当日到期 → P0 → 3 天内到期 → 其余。"])
    if not pending:
        lines.append("当前事项表无待办；这不代表没有其他工作。")
    elif not focus:
        lines.append("当前没有已明确归属、可直接安排的本人事项。")
    remaining = [row for row in pending if row not in focus and row["status"] != "草稿已备"]
    displayed = focus if edition == "morning" else [*focus, *remaining]
    for number, row in enumerate(displayed, 1):
        if edition == "evening" and number == len(focus) + 1:
            lines.extend(["", "其余未完成事项（保留状态与协作归属）"])
        facts = [row["reason"], row["status"] or "状态待确认"]
        if row["direction"]:
            facts.append(row["direction"])
        if row["priority"] and row["priority"] != row["reason"]:
            facts.append(row["priority"])
        facts.append("截止 " + (row["due_date"] or "待确认"))
        facts.append(f"预计 {_fmt(row['minutes'])} 分钟" if row["minutes"] is not None else "耗时未知")
        lines.append(f"□ {number}. {row['title']}｜" + "｜".join(facts))
        if row["next_action"]:
            lines.append("  下一步：" + row["next_action"])
        if row["source_url"]:
            lines.append("  来源：" + row["source_url"])
    if edition == "morning" and remaining:
        counts = {}
        for row in remaining:
            status = row["status"] or "状态待确认"
            counts[status] = counts.get(status, 0) + 1
        lines.extend(["", f"其余未完成 {len(remaining)} 项未展开：" + "、".join(
            f"{status} {count} 项" for status, count in sorted(counts.items())) + "。"])
        base_url = _link(getattr(config, "base_url", ""))
        lines.append("完整事项表：" + base_url if base_url else "可在 Cindy 对话里回复“展开简报”查看其余事项。")
    lines.extend(["", "时间安排"])
    lines.append(f"{capacity['scope']}：已知耗时 {_fmt(capacity['known_task_minutes'])} 分钟，"
                 f"{capacity['unknown_task_count']} 项耗时未知。")
    if not capacity["configured"]:
        lines.append("工作时段未设置，暂不判断是否排得下。")
    else:
        lines.append(f"工作时段 {config.work_start}–{config.work_end}，扣除休息 {_fmt(capacity['break_minutes'])} 分钟、"
                     f"已确认会议 {_fmt(capacity['meeting_minutes_excluding_breaks'])} 分钟（重叠只算一次），"
                     f"剩余 {_fmt(capacity['available_minutes'])} 分钟。")
        lines.append(f"按 70% 留余量，建议计划上限 {_fmt(capacity['planning_limit_minutes'])} 分钟。")
        if capacity["exceeds_planning_limit"]:
            lines.append("⚠ 已知事项耗时超过建议上限，需要确认取舍；截止日保持原值。")
        elif capacity["unknown_task_count"]:
            lines.append("还有事项未估时，暂不能确认是否排得下。")
        else:
            lines.append("上述已知事项耗时未超过建议上限；其余事项尚未纳入该日容量。")
        if calendar["tentative"] or calendar["warnings"]:
            lines.append("待确认日程未占用上述工时，确认后可用时间可能减少。")
    if completed:
        lines.extend(["", "近期完成（按更新时间筛选，不等同于实际完成时刻）"])
        for row in (completed[:3] if edition == "morning" else completed):
            lines.append("✓ " + row["title"] + (f"\n  来源：{row['source_url']}" if row["source_url"] else ""))
        if edition == "morning" and len(completed) > 3:
            lines.append(f"另有 {len(completed) - 3} 项近期完成未展开。")
    if confirmations:
        lines.extend(["", f"待确认 {len(confirmations)} 项"])
        for row in (confirmations[:3] if edition == "morning" else confirmations):
            lines.append(f"? {row['title']}：" + "、".join(row["fields"]))
        if edition == "morning" and len(confirmations) > 3:
            lines.append(f"另有 {len(confirmations) - 3} 项待确认未展开。")
    lines.extend(["", "可在 Cindy 对话里回复事项名称和变化。"])
    for row in [*pending, *completed]:
        if row["updated_at"]:
            row["updated_at"] = row["updated_at"].isoformat()
    result = {"edition": edition, "target_date": target_date.isoformat(),
            "pending": pending, "focus": focus, "recent_completed": completed, "confirmations": confirmations,
            "calendar": calendar, "capacity": capacity, "awaiting_review": review}
    result["text"], result["condensed"] = _compact("\n".join(lines), result, config)
    return result


def _today(config):
    return datetime.now(ZoneInfo(config.timezone)).date()


def _read_rows(config, client, state):
    """Older bases keep working; project the new summary only after schema readback."""
    from ..bitable import items
    from ..records import read_records

    rows = read_records(client, state, "事项表", FIELDS)
    if not any(_text(row.get("fields", row).get("状态")) == "草稿已备" for row in rows):
        return rows
    table_id = config.tables.get("事项表")
    if not table_id:
        raise MatrixError("事项表尚未绑定，不能核对预做摘要字段。")
    actual = items(client.call("+field-list", "--base-token", config.base_token, "--table-id", table_id))
    if not all(isinstance(field, dict) for field in actual):
        raise MatrixError("事项表字段清单异常，不能确认预做结果摘要。")
    names = {field.get("name", field.get("field_name")) for field in actual}
    if SUMMARY_FIELD in names:
        return read_records(client, state, "事项表", [*FIELDS, SUMMARY_FIELD])
    return rows


def run(config, client, state, *, edition="morning", target_date=None, preview=False) -> dict:
    """Read actual records and calendar; send at most once per edition/target date."""

    if edition not in {"morning", "evening"}:
        raise MatrixError("简报版本仅支持 morning 或 evening。")
    target_date = target_date or (_today(config) + timedelta(days=edition == "evening"))
    if isinstance(target_date, str):
        try:
            target_date = date.fromisoformat(target_date)
        except ValueError:
            raise MatrixError("目标日期需使用 YYYY-MM-DD 格式。") from None
    if not isinstance(target_date, date) or isinstance(target_date, datetime):
        raise MatrixError("简报目标日期须为日期。")
    key = f"brief:{edition}:{target_date.isoformat()}"
    previous = state.operation(key) if not preview else None
    if previous and previous["status"] == "done":
        message_id = find_value(previous["receipt"], "message_id")
        if not message_id:
            raise MatrixError("已有简报发送记录缺少 message_id，需先核对回执。")
        return {"sent": False, "already_sent": True, "message_id": message_id,
                "edition": edition, "target_date": target_date.isoformat(), "operation_key": key}
    rows = _read_rows(config, client, state)
    start = datetime.combine(target_date, time(), ZoneInfo(config.timezone))
    events = client._exec(["calendar", "+agenda", "--start", start.isoformat(),
                           "--end", (start + timedelta(days=1)).isoformat(), "--as", "user", "--format", "json"])
    result = build_brief(config, rows, events, target_date, edition)
    result.update(preview=preview, sent=False, already_sent=False, operation_key=key)
    if preview:
        return result

    def deliver():
        receipt = client.send_self(result["text"], key)
        if not find_value(receipt, "message_id"):
            # Leave pending: an unacknowledged send must never be blindly retried.
            raise MatrixError("简报发送缺少 message_id 回执，结果待核对，已阻止自动重发。")
        return receipt

    receipt = state.once(key, deliver)
    message_id = find_value(receipt, "message_id")
    if not message_id:
        raise MatrixError("简报发送结果未确认。")
    result.update(sent=True, message_id=message_id)
    return result
