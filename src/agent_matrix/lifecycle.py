"""The one table of item statuses and which changes between them are allowed.

    canonical("草稿已备")            -> "待验收"   (older wording is still read)
    problem("完成", "待验收")         -> a sentence saying why this change is not in the table, else None

Every status write goes through workflow.Gateway.update, which asks `problem` first. With ENFORCE off
(today) an unlisted change is still written and recorded in the activity log as kind "lifecycle", so
the table can be checked against real use before it starts refusing anything. To allow a new change,
add it to ALLOWED here; nowhere else decides this.
"""
STATUSES = ["待确认", "未开始", "进行中", "卡住", "待验收", "完成", "取消"]

# Wording used before v1, still present in old rows and old code paths.
LEGACY = {"待办": "未开始", "阻塞": "卡住", "草稿已备": "待验收"}

OPEN = {"待确认", "未开始", "进行中", "卡住", "待验收"}
CLOSED = {"完成", "取消"}

ALLOWED = {
    "待确认": {"未开始", "进行中", "卡住", "待验收", "完成", "取消"},
    "未开始": {"待确认", "进行中", "卡住", "待验收", "完成", "取消"},
    "进行中": {"待确认", "未开始", "卡住", "待验收", "完成", "取消"},
    "卡住": {"待确认", "未开始", "进行中", "待验收", "完成", "取消"},
    # Waiting for the owner: approve, send back, block on a question, or drop. Never silently back to the start.
    "待验收": {"进行中", "卡住", "完成", "取消"},
    # Finished work reopens only on purpose: the owner asks for a change (进行中) or restores it (待确认).
    "完成": {"进行中", "待确认"},
    # A dropped item comes back only through 恢复; it never jumps straight to delivered or done.
    "取消": {"待确认", "未开始"},
}

ENFORCE = False


def canonical(status):
    """The v1 word for this status; None when it is empty or unknown."""
    if isinstance(status, list):
        status = status[0] if len(status) == 1 else None
    if not isinstance(status, str):
        return None
    status = LEGACY.get(status, status)
    return status if status in ALLOWED else None


def problem(old, new):
    """Why changing `old` to `new` is outside the table, or None when it is fine (or not a change)."""
    before, after = canonical(old), canonical(new)
    if new in (None, "", []):
        return None  # the patch does not set a status
    if after is None:
        return f"状态「{new}」不在状态表里"
    if before is None or before == after:
        return None  # a new or unreadable row has nothing to compare with
    if after not in ALLOWED[before]:
        return f"「{before}」不能直接变成「{after}」"
    return None
