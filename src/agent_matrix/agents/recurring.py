"""Deterministic, date-only occurrences from explicitly enabled recurring rules."""
from datetime import date, datetime, time, timedelta
import math
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr

from ..bitable import js, require_owned
from ..models import MatrixError
from ..timeutil import TZ


MAX_LEAD_DAYS = 366
MAX_CREATE_RECORDS = 2000
RULE_FIELDS = ["名称", "频率", "提前天数", "默认负责人", "预计耗时(分钟)",
               "检查清单", "能否AI预做", "启用"]
PREWORK_RULE_FIELDS = [*RULE_FIELDS, "预做指令"]
TASK_FIELDS = ["标题", "类型", "负责人", "截止日", "首次截止日", "延期次数",
               "下一步动作", "状态", "预计耗时(分钟)", "能否AI预做", "来源", "去重键"]
WEEKDAYS = {"MO", "TU", "WE", "TH", "FR", "SA", "SU"}
ALLOWED_PARTS = {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYSETPOS", "BYMONTH",
                 "BYMONTHDAY", "BYDAY", "BYYEARDAY", "BYWEEKNO", "WKST"}


def _error(rule_id, message):
    return MatrixError(f"周期规则 {rule_id}：{message}")


def _start(line, rule_id):
    """Parse only explicit iCalendar dates/times, never parser 'today' defaults."""
    header, separator, value = line.partition(":")
    if not separator:
        raise _error(rule_id, "DTSTART 缺少日期。")
    parameters = {}
    for parameter in header.split(";")[1:]:
        key, equals, item = parameter.partition("=")
        key = key.upper()
        if not equals or key not in {"TZID", "VALUE"} or key in parameters or not item:
            raise _error(rule_id, "DTSTART 参数无效。")
        parameters[key] = item
    kind = parameters.get("VALUE", "DATE-TIME").upper()
    if kind not in {"DATE", "DATE-TIME"}:
        raise _error(rule_id, "DTSTART 仅支持日期或日期时间。")
    try:
        if re.fullmatch(r"\d{8}", value):
            result = datetime.strptime(value, "%Y%m%d")
        elif kind != "DATE" and re.fullmatch(r"\d{8}T\d{6}Z?", value.upper()):
            result = datetime.strptime(value.upper().rstrip("Z"), "%Y%m%dT%H%M%S")
        else:
            raise ValueError("unsupported date syntax")
        if value.upper().endswith("Z"):
            if "TZID" in parameters:
                raise ValueError("TZID and UTC suffix conflict")
            return result.replace(tzinfo=ZoneInfo("UTC"))
        if "TZID" in parameters:
            return result.replace(tzinfo=ZoneInfo(parameters["TZID"]))
        # Floating dates/times are interpreted in the project's Asia/Shanghai zone.
        return result
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise _error(rule_id, "DTSTART 日期或时区无效。") from exc


def _positive(value, name, rule_id):
    if not re.fullmatch(r"[0-9]+", value) or not 1 <= int(value) <= 1_000_000:
        raise _error(rule_id, f"{name} 必须是 1 到 1000000 的整数。")
    return int(value)


def _recurrence(raw, rule_id):
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 4096:
        raise _error(rule_id, "启用前必须填写有效的 RRULE。")
    start = None
    rule_text = None
    for line in (line.strip() for line in raw.splitlines() if line.strip()):
        if line.upper().startswith(("DTSTART:", "DTSTART;")):
            if start is not None:
                raise _error(rule_id, "只能指定一个 DTSTART。")
            start = _start(line, rule_id)
        elif rule_text is None and (line.upper().startswith("RRULE:") or "=" in line and ":" not in line):
            rule_text = line.partition(":")[2] if line.upper().startswith("RRULE:") else line
        else:
            raise _error(rule_id, "仅支持一条 RRULE 和可选的 DTSTART，不支持额外日期集合。")
    if not rule_text:
        raise _error(rule_id, "缺少 RRULE。")
    parts = {}
    for part in rule_text.upper().split(";"):
        key, equals, value = part.partition("=")
        if not equals or key not in ALLOWED_PARTS or key in parts or not value:
            raise _error(rule_id, "RRULE 含未知、重复或无效参数；不支持小时、分钟、秒级规则。")
        parts[key] = value
    frequency = parts.get("FREQ")
    if frequency not in {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"}:
        raise _error(rule_id, "FREQ 只支持 DAILY、WEEKLY、MONTHLY、YEARLY。")
    interval = _positive(parts.get("INTERVAL", "1"), "INTERVAL", rule_id)
    if "COUNT" in parts:
        _positive(parts["COUNT"], "COUNT", rule_id)
    if "COUNT" in parts and "UNTIL" in parts:
        raise _error(rule_id, "COUNT 和 UNTIL 不能同时使用。")
    if "UNTIL" in parts and not re.fullmatch(r"[0-9]{8}(?:T[0-9]{6}Z?)?", parts["UNTIL"]):
        raise _error(rule_id, "UNTIL 必须是完整的 YYYYMMDD 或 YYYYMMDDTHHMMSS 日期时间，不能省略月日。")
    if "WKST" in parts and parts["WKST"] not in WEEKDAYS:
        raise _error(rule_id, "WKST 必须是明确的星期。")
    if "BYDAY" in parts:
        for value in parts["BYDAY"].split(","):
            match = re.fullmatch(r"([+-]?[0-9]+)?(MO|TU|WE|TH|FR|SA|SU)", value)
            if not match or match[1] is not None and not 1 <= abs(int(match[1])) <= 53:
                raise _error(rule_id, "BYDAY 星期或序号无效。")
    for name, maximum, negative in (("BYMONTH", 12, False), ("BYMONTHDAY", 31, True),
                                     ("BYYEARDAY", 366, True), ("BYWEEKNO", 53, True),
                                     ("BYSETPOS", 366, True)):
        if name in parts:
            for value in parts[name].split(","):
                if not re.fullmatch(r"[+-]?[0-9]+", value):
                    raise _error(rule_id, f"{name} 必须是整数列表。")
                number = int(value)
                if not 1 <= abs(number) <= maximum or not negative and number < 0:
                    raise _error(rule_id, f"{name} 超出有效范围。")
    if start is None:
        if interval > 1 or "COUNT" in parts:
            raise _error(rule_id, "INTERVAL 大于 1 或使用 COUNT 时必须明确填写 DTSTART。")
        if frequency == "WEEKLY" and "BYDAY" not in parts:
            raise _error(rule_id, "无 DTSTART 的每周规则必须填写 BYDAY，不能猜星期。")
        if frequency == "MONTHLY" and not {"BYMONTHDAY", "BYDAY"} & parts.keys():
            raise _error(rule_id, "无 DTSTART 的每月规则必须填写 BYMONTHDAY 或 BYDAY。")
        if frequency == "YEARLY" and not ("BYMONTH" in parts and {"BYMONTHDAY", "BYDAY"} & parts.keys()):
            raise _error(rule_id, "无 DTSTART 的每年规则必须明确 BYMONTH 及 BYMONTHDAY 或 BYDAY。")
        # With no interval/count and explicit date components, this invariant anchor
        # never invents a weekday/month/day or consults the current clock.
        start = datetime(1970, 1, 1)
        if parts.get("UNTIL", "").endswith("Z"):
            start = start.replace(tzinfo=TZ)
    parts.setdefault("WKST", "MO")
    try:
        recurrence = rrulestr("RRULE:" + ";".join(f"{key}={value}" for key, value in parts.items()), dtstart=start)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _error(rule_id, "RRULE 无法解析；请检查日期、参数及 DTSTART/UNTIL 的时区。") from exc
    return recurrence, start


def _template(rule, rule_id, *, include_prework_instructions=False):
    name = rule.get("名称")
    if not isinstance(name, str) or not name.strip():
        raise _error(rule_id, "启用前必须填写名称。")
    lead = rule.get("提前天数")
    if (isinstance(lead, bool) or not isinstance(lead, (int, float)) or not math.isfinite(lead)
            or not 0 <= lead <= MAX_LEAD_DAYS or int(lead) != lead):
        raise _error(rule_id, f"提前天数必须明确填写 0 到 {MAX_LEAD_DAYS} 的整数。")
    owners = rule.get("默认负责人")
    if owners is None:
        owners = []
    if not isinstance(owners, list) or len(owners) > 1:
        raise _error(rule_id, "默认负责人必须为空或一位飞书用户。")
    copied_owners = []
    for owner in owners:
        if not isinstance(owner, dict) or not isinstance(owner.get("id"), str) or not owner["id"].strip():
            raise _error(rule_id, "默认负责人缺少有效用户 ID，不能按名字猜测。")
        copied = {"id": owner["id"]}
        if owner.get("name") is not None:
            if not isinstance(owner["name"], str):
                raise _error(rule_id, "默认负责人名称无效。")
            copied["name"] = owner["name"]
        copied_owners.append(copied)
    allow_prework = rule.get("能否AI预做")
    if allow_prework is not None and not isinstance(allow_prework, bool):
        raise _error(rule_id, "能否AI预做必须是复选框布尔值。")
    result = {"标题": name.strip(), "类型": ["周期事项"], "来源": ["周期规则"],
              "状态": ["待办" if copied_owners else "待确认"], "延期次数": 0,
              "能否AI预做": allow_prework is True}
    if copied_owners:
        result["负责人"] = copied_owners
    checklist = rule.get("检查清单")
    if checklist is not None:
        if not isinstance(checklist, str):
            raise _error(rule_id, "检查清单必须是文字。")
        if checklist.strip():
            result["下一步动作"] = checklist
    estimate = rule.get("预计耗时(分钟)")
    if estimate is not None:
        if isinstance(estimate, bool) or not isinstance(estimate, (int, float)) or not math.isfinite(estimate) or estimate < 0:
            raise _error(rule_id, "预计耗时必须为空或非负分钟数。")
        result["预计耗时(分钟)"] = estimate
    if include_prework_instructions:
        instructions = rule.get("预做指令")
        if instructions is not None:
            if not isinstance(instructions, str):
                raise _error(rule_id, "预做指令必须为空或文字。")
            if instructions.strip():
                result["预做指令"] = instructions
    return result, int(lead)


def plan(rules: list[dict], existing: list[dict], today: date, *, minimum_lead_days=0,
         include_prework_instructions=False) -> dict:
    """Validate all rules and return prospective records, without I/O or mutation."""
    if type(today) is not date:
        raise MatrixError("周期事项需要明确的日期，不接受无时区的日期时间。")
    if not isinstance(rules, list) or not isinstance(existing, list):
        raise MatrixError("周期规则和现有事项必须是完整记录列表。")
    if (isinstance(minimum_lead_days, bool) or not isinstance(minimum_lead_days, int)
            or not 0 <= minimum_lead_days <= MAX_LEAD_DAYS):
        raise MatrixError(f"最小提前创建天数必须是 0 到 {MAX_LEAD_DAYS} 的整数。")
    existing_keys = set()
    for row in existing:
        if not isinstance(row, dict):
            raise MatrixError("现有事项记录格式无效，无法可靠去重。")
        key = row.get("去重键")
        if key is not None and not isinstance(key, str):
            raise MatrixError("现有事项的去重键不是文字，停止创建以避免重复。")
        if key:
            existing_keys.add(key.strip())
    prepared = []
    disabled = 0
    ids = set()
    for rule in rules:
        if not isinstance(rule, dict):
            raise MatrixError("周期规则记录格式无效。")
        if rule.get("启用") is not True:
            disabled += 1
            continue
        rule_id = rule.get("record_id")
        if not isinstance(rule_id, str) or not rule_id.strip() or rule_id in ids:
            raise MatrixError("启用的周期规则缺少唯一记录 ID。")
        ids.add(rule_id)
        template, lead = _template(rule, rule_id, include_prework_instructions=include_prework_instructions)
        recurrence, start = _recurrence(rule.get("频率"), rule_id)
        # Validate the original rule before expanding the creation window: a floor
        # must never turn a missing/invalid lead time or recurrence into a valid rule.
        prepared.append((rule_id, template, max(lead, minimum_lead_days), recurrence, start))
    records = []
    candidate_count = skipped = 0
    for rule_id, template, lead, recurrence, start in prepared:
        try:
            after = datetime.combine(today, time.min)
            before = datetime.combine(today + timedelta(days=lead), time.max)
            if start.tzinfo is not None:
                after = after.replace(tzinfo=TZ).astimezone(start.tzinfo)
                before = before.replace(tzinfo=TZ).astimezone(start.tzinfo)
            dates = sorted({(occurrence.astimezone(TZ) if occurrence.tzinfo else occurrence).date()
                            for occurrence in recurrence.between(after, before, inc=True)})
        except (TypeError, ValueError, OverflowError) as exc:
            raise _error(rule_id, "无法计算日期范围；请检查规则及日期边界。") from exc
        for due in dates:
            candidate_count += 1
            key = f"rule:{rule_id}:{due.isoformat()}"
            if key in existing_keys:
                skipped += 1
                continue
            records.append({**template, "截止日": due.isoformat(), "首次截止日": due.isoformat(), "去重键": key})
            if len(records) > MAX_CREATE_RECORDS:
                raise MatrixError(f"周期事项单轮计划超过 {MAX_CREATE_RECORDS} 条；请缩短提前范围后重试。")
    records.sort(key=lambda row: (row["截止日"], row["去重键"]))
    return {"ok": True, "today": today.isoformat(), "rule_count": len(rules),
            "enabled_count": len(prepared), "disabled_count": disabled,
            "candidate_count": candidate_count, "existing_count": len(existing),
            "skipped": skipped, "skipped_count": skipped,
            "planned_count": len(records), "create_records": records}


def run(config, client, state, *, today=None, preview=False) -> dict:
    from ..records import ensure_fields, read_records

    require_owned(config, state)
    for table in ("周期规则表", "事项表"):
        if table not in config.tables:
            raise MatrixError("尚未绑定表格：" + table)
    prework_enabled = getattr(config, "prework_enabled", False) is True
    rule_fields = PREWORK_RULE_FIELDS if prework_enabled else RULE_FIELDS
    ensure_fields(client, "周期规则表", rule_fields)
    rules = read_records(client, state, "周期规则表", rule_fields)
    existing = read_records(client, state, "事项表", ["去重键", "状态"])
    result = plan(rules, existing, today if today is not None else datetime.now(TZ).date(),
                  minimum_lead_days=getattr(config, "prework_lead_days", 2) if prework_enabled else 0,
                  include_prework_instructions=prework_enabled)
    # Field checks, rule parsing and all uncertain-operation checks precede any writes.
    # Disabled/empty-rule installations retain the old table contract. Require the
    # migrated instruction field only when this batch actually has text to write.
    target_fields = [*TASK_FIELDS]
    if any("预做指令" in row for row in result["create_records"]):
        target_fields.append("预做指令")
    ensure_fields(client, "事项表", target_fields)
    candidates = []
    # A remote row may already exist after an uncertain/partial write. Its key stops
    # duplicates but does not prove that the original write's other fields succeeded.
    pending = [key for key in state.pending() if key.startswith("rule:")
               and state.operation(key)["status"] != "not_sent"]
    cached = 0
    for row in result["create_records"]:
        key = row["去重键"]
        operation = state.operation(key)
        if operation and operation["status"] == "done":
            cached += 1
            continue
        if operation and operation["status"] != "not_sent" and key not in pending:
            pending.append(key)
        candidates.append(row)
    result.update({"ok": not pending, "preview": bool(preview), "created_count": 0,
                   "created_ids": [], "cached_count": cached, "pending_keys": pending,
                   "planned_count": len(candidates), "create_records": candidates,
                   "skipped": result["skipped"] + cached, "skipped_count": result["skipped_count"] + cached})
    if preview:
        return result
    if pending:
        raise MatrixError("周期事项存在上次结果未确认的操作；本轮未创建，请先核对远端结果：" + "、".join(pending))

    def create(row):
        receipt = client.call("+record-batch-create", "--base-token", config.base_token,
                              "--table-id", config.tables["事项表"],
                              "--json", js({"create_records": [row]}), write=True)
        ids = receipt.get("record_id_list") if isinstance(receipt, dict) else None
        if not isinstance(ids, list) or len(ids) != 1 or not isinstance(ids[0], str) or not ids[0]:
            raise MatrixError("周期事项创建回执缺少唯一记录 ID；结果待核对，不自动重试。")
        if receipt.get("ignored_fields"):
            raise MatrixError("周期事项写入有字段被忽略；结果待核对，不自动重试。")
        fresh = read_records(client, state, "事项表", list(row))
        matches = [item for item in fresh if item.get("record_id") == ids[0]]
        if len(matches) != 1 or not _matches(matches[0], row):
            raise MatrixError("周期事项写后读回不完整或字段不一致；结果待核对，不自动重试。")
        return {**receipt, "verified_by_readback": True}

    for row in candidates:
        receipt = state.once(row["去重键"], lambda row=row: create(row))
        result["created_ids"].extend(receipt["record_id_list"])
        result["created_count"] += 1
    return result


def _matches(actual, expected):
    """Compare meaningful field values, allowing Feishu's normalized date/user data."""
    for field, value in expected.items():
        current = actual.get(field)
        if field in {"截止日", "首次截止日"}:
            if not isinstance(current, str):
                return False
            try:
                parsed = datetime.fromisoformat(current)
            except ValueError:
                return False
            if parsed.tzinfo is None and len(current) != 10:
                return False
            current = (parsed.astimezone(TZ) if parsed.tzinfo else parsed).date().isoformat()
        elif field == "负责人":
            if not isinstance(current, list) or not all(isinstance(person, dict) for person in current):
                return False
            current = [person.get("id") for person in current]
            value = [person["id"] for person in value]
        if current != value:
            return False
    return True
