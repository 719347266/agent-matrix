"""Pure prework eligibility and input versioning; never executes an action."""
from collections import Counter
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..models import MatrixError


FIELDS = ["标题", "负责人", "方向", "截止日", "状态", "下一步动作", "来源", "来源链接",
          "原话引用", "预做策略", "预做指令"]
# v1 inputs: a rejection comment or added material makes a new version to rework.
V1_FIELDS = ["执行方", "完成标准", "退回意见", "补充材料", "排除本人提醒", "编号"]
VERSION_FIELDS = [field for field in FIELDS if field != "状态"]
V1_VERSION_FIELDS = [*VERSION_FIELDS, "完成标准", "退回意见", "补充材料"]
POLICIES = {"自动判断", "仅备草稿", "不代做"}
ACTIVE = {"待办", "进行中", "未开始"}


def _selection(value):
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value):
        return [item.strip() for item in value]
    return None


def _canonical(field, value):
    if field in {"方向", "来源", "预做策略"}:
        selection = _selection(value)
        if selection is not None:
            if field == "预做策略" and not selection:
                selection = ["自动判断"]
            return sorted(selection)
    if field == "负责人" and isinstance(value, list):
        if all(isinstance(person, dict) and isinstance(person.get("id"), str) and person["id"] for person in value):
            # Display-name refreshes do not change who owns the task.
            return sorted(person["id"] for person in value)
    if field == "截止日" and isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return value
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).isoformat()
    return "" if value is None else value


def fingerprint(row) -> str:
    """Hash only task inputs; status, draft output and update timestamps never count."""
    if not isinstance(row, dict):
        raise MatrixError("预做版本需要事项记录。")
    fields = V1_VERSION_FIELDS if "执行方" in row else VERSION_FIELDS
    payload = {field: _canonical(field, row.get(field)) for field in fields}
    try:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MatrixError("事项字段格式无效，无法生成预做输入版本。") from exc
    return sha256(encoded).hexdigest()


def _due(value, tz):
    if not isinstance(value, str):
        raise ValueError("deadline must be an ISO date")
    if len(value) == 10:
        return date.fromisoformat(value)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("datetime requires an explicit timezone")
    return parsed.astimezone(tz).date()


def assess(config, rows, *, today=None) -> dict:
    """Select model-assessment candidates, without claiming any task is executable."""
    if not isinstance(rows, list):
        raise MatrixError("预做评估需要完整的事项列表。")
    try:
        tz = ZoneInfo(getattr(config, "timezone", "Asia/Shanghai"))
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise MatrixError("预做评估的时区配置无效。") from exc
    if today is None:
        today = datetime.now(tz).date()
    elif isinstance(today, str):
        try:
            today = date.fromisoformat(today)
        except ValueError as exc:
            raise MatrixError("预做评估日期须为 YYYY-MM-DD。") from exc
    if type(today) is not date:
        raise MatrixError("预做评估需要明确的日期。")
    lead = getattr(config, "prework_lead_days", 2)
    if isinstance(lead, bool) or not isinstance(lead, int) or not 0 <= lead <= 366:
        raise MatrixError("预做提前天数必须是 0 到 366 的整数。")
    try:
        cutoff = today + timedelta(days=lead)
    except OverflowError as exc:
        raise MatrixError("预做评估日期范围超出有效边界。") from exc
    enabled = getattr(config, "prework_enabled", False) is True
    owner_id = getattr(config, "owner_open_id", "")
    configured_owner = isinstance(owner_id, str) and bool(owner_id.strip())
    counts = Counter(row.get("record_id") for row in rows if isinstance(row, dict) and isinstance(row.get("record_id"), str))
    candidates, skipped = [], []

    def skip(row, code, reason):
        record_id = row.get("record_id") if isinstance(row, dict) else None
        title = row.get("标题") if isinstance(row, dict) else None
        skipped.append({"record_id": record_id if isinstance(record_id, str) else None,
                        "title": title if isinstance(title, str) else "未命名事项",
                        "code": code, "reason": reason})

    for row in rows:
        if not enabled:
            skip(row, "prework_disabled", "预做功能尚未启用。")
            continue
        if not configured_owner:
            skip(row, "owner_unconfigured", "本人用户 ID 尚未配置，不能判断负责人。")
            continue
        if not isinstance(row, dict):
            skip(row, "invalid_row", "事项记录格式无效。")
            continue
        record_id = row.get("record_id")
        if not isinstance(record_id, str) or not record_id.strip():
            skip(row, "missing_record_id", "缺少事项记录 ID，无法可靠追踪。")
            continue
        if counts[record_id] > 1:
            skip(row, "duplicate_record_id", "事项记录 ID 重复，需重新读取完整记录。")
            continue
        title = row.get("标题")
        if not isinstance(title, str) or not title.strip():
            skip(row, "missing_title", "事项标题待确认。")
            continue
        policy_values = _selection(row.get("预做策略"))
        if policy_values is None or len(policy_values) > 1:
            skip(row, "policy_unknown", "预做策略格式不明确，需确认后评估。")
            continue
        policy = policy_values[0] if policy_values else "自动判断"
        if policy not in POLICIES:
            skip(row, "policy_unknown", "预做策略未知，需确认后评估。")
            continue
        if policy == "不代做":
            skip(row, "policy_declined", "该事项明确设为不代做。")
            continue
        owners = row.get("负责人")
        if owners is None or owners == []:
            skip(row, "owner_missing", "负责人待确认，不能默认由本人负责。")
            continue
        if (not isinstance(owners, list) or len(owners) != 1 or not isinstance(owners[0], dict)
                or not isinstance(owners[0].get("id"), str) or not owners[0]["id"].strip()):
            skip(row, "owner_ambiguous", "负责人必须是唯一且用户 ID 明确的人员。")
            continue
        if owners[0]["id"] != owner_id:
            skip(row, "owner_other", "该事项由他人负责。")
            continue
        v1 = "执行方" in row
        if v1:
            executor = _selection(row.get("执行方"))
            if executor != ["AI"]:
                skip(row, "executor_not_ai", "执行方不是 AI，AI 不动这件事。")
                continue
            if row.get("排除本人提醒"):
                skip(row, "excluded", "本人说先不做或不属于本人工作。")
                continue
        statuses = _selection(row.get("状态"))
        if statuses is None or len(statuses) != 1:
            skip(row, "state_unknown", "事项状态待确认。")
            continue
        status = statuses[0]
        raw_due = row.get("截止日")
        from_meeting = _selection(row.get("来源")) == ["会议"]
        # An explicit draft-only policy can prepare an owned meeting action
        # while its missing deadline stays unconfirmed. No other pending state
        # (including an existing or malformed deadline) gains this exception.
        pending_draft = (not v1 and status == "待确认" and policy == "仅备草稿" and from_meeting
                         and (raw_due is None or raw_due == ""))
        if status not in ACTIVE and not pending_draft:
            reason = {"完成": "事项已完成。", "取消": "事项已取消。", "草稿已备": "已有草稿待确认，不重复预做。",
                      "待验收": "已交付，等本人验收。", "卡住": "卡住了，等补材料。",
                      "待确认": "负责人或日期还没定，AI 先不接。"}.get(status,
                      "当前状态不属于待办或进行中，暂不评估预做。")
            skip(row, "state_ineligible", reason)
            continue
        due = None
        if raw_due is not None and raw_due != "":
            try:
                due = _due(raw_due, tz)
            except (TypeError, ValueError, OverflowError):
                skip(row, "due_invalid", "截止日格式或时区无效，需确认后评估。")
                continue
        # Only an unambiguous meeting source bypasses the lead-time window.
        # Ownership, policy, state and date validation still apply above.
        if not from_meeting and due is not None and due > cutoff:
            skip(row, "too_early", f"尚未进入提前 {lead} 天的评估范围。")
            continue
        if from_meeting:
            reason = "来源为会议，直接评估可代做性。"
            if due is None:
                reason += "截止日待确认，不推定日期。"
            elif due < today:
                reason += "该事项已逾期。"
        else:
            reason = ("截止日待确认，可先评估可代做性，不推定日期。" if due is None else
                      "已逾期，保留在可代做性评估范围。" if due < today else
                      "已进入近期到期范围，评估可代做性。")
        if policy == "仅备草稿":
            reason += "用户策略限定仅备草稿。"
        if pending_draft:
            reason += "保留事项待确认状态，仅评估草稿准备，不视为日期已确认。"
        try:
            version = fingerprint(row)
        except MatrixError:
            skip(row, "invalid_fields", "任务内容字段格式无效，无法生成稳定输入版本。")
            continue
        candidates.append({"record_id": record_id, "input_version": version, "title": title.strip(),
                           "due_date": due.isoformat() if due else None, "reason": reason,
                           "fields": {field: deepcopy(row.get(field)) for field in [*FIELDS, *(V1_FIELDS if v1 else [])]},
                           "policy": policy, "rework": bool(v1 and row.get("退回意见") and status == "进行中")})
    candidates.sort(key=lambda item: (item["due_date"] is None, item["due_date"] or "", item["record_id"]))
    skipped.sort(key=lambda item: (item["record_id"] or "", item["code"], item["title"]))
    return {"ok": not enabled or configured_owner, "enabled": enabled, "today": today.isoformat(),
            "cutoff_date": cutoff.isoformat(), "candidates": candidates, "skipped": skipped,
            "candidate_count": len(candidates), "skipped_count": len(skipped)}
