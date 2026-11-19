"""Feishu tasks assigned to the owner → the task table. Pure code, no model.

- New open task assigned to the owner → one row (executor 我, source 飞书任务, link back).
- First run imports only tasks created in the last 14 days; later every new one comes in.
- Owner ticks it done in Feishu → the row becomes 完成 (that is the owner's own action).
- Nothing is ever written back to Feishu tasks.
"""
from datetime import datetime, timedelta

from .. import activity
from ..brief_migration import number, single
from ..models import MatrixError

FIRST_IMPORT_DAYS = 14


def _list(client, *, complete, since):
    data = client._exec(["task", "+get-my-tasks", "--complete=" + ("true" if complete else "false"),
                         "--created_at", since.strftime("%Y-%m-%d"), "--page-all", "--as", "user", "--format", "json"])
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise MatrixError("飞书任务列表结构异常，本轮不同步。")
    return items


def _assigned_to(task, owner):
    return any(m.get("id") == owner and m.get("role") == "assignee" for m in task.get("members") or [])


def _due(task, tz):
    due = task.get("due") or {}
    stamp = due.get("timestamp") if isinstance(due, dict) else None
    if not stamp:
        return None
    return datetime.fromtimestamp(int(stamp) / 1000, tz).strftime("%Y-%m-%d 00:00")


def fields_for(task, config, tz):
    title = " ".join(str(task.get("summary") or "").split())[:60]
    fields = {"标题": title, "执行方": ["我"], "状态": ["未开始"], "进度": "0/1 未开始",
              "负责人": [{"id": config.owner_open_id}], "来源": ["飞书任务"], "类型": ["临时"],
              "关联任务链接": task.get("url")}
    due = _due(task, tz)
    if due:
        fields["截止日"] = due
    return fields


def sync(config, client, state, now, *, notify=True):
    from ..workflow import Gateway, ensure_options
    if not state.get("v1:source-options"):
        ensure_options(client, state, "来源", ["飞书任务", "聊天"])
        state.set("v1:source-options", True)
    started = state.get("feishu-tasks:started")
    if not started:
        started = now.isoformat()
        state.set("feishu-tasks:started", started)
        first = True
    else:
        first = False
    since = now - timedelta(days=FIRST_IMPORT_DAYS)
    owner = config.owner_open_id
    open_tasks = [t for t in _list(client, complete=False, since=since)
                  if _assigned_to(t, owner) and str(t.get("summary") or "").strip()]
    done_tasks = {t["guid"]: t for t in _list(client, complete=True, since=since - timedelta(days=16))}
    gateway = Gateway(client, state)
    rows = {str(r.get("去重键") or ""): r for r in gateway.rows()}
    created, completed = [], []
    from ..merge import find_same, merge_into, task_guid
    all_rows = gateway.rows()
    linked = {task_guid(r.get("关联任务链接")) for r in all_rows if task_guid(r.get("关联任务链接"))}
    merged = []
    for task in open_tasks:
        key = "feishu-task:" + task["guid"]
        if key in rows or task["guid"] in linked:
            continue
        target, _, _ = find_same(all_rows, task.get("summary"), task_link=task.get("url"))
        if target is not None:
            merge_into(gateway, target, title=task.get("summary"), source="飞书任务", task_link=task.get("url"),
                       key="feishu-task-merge:" + task["guid"], now=now)
            merged.append(target.get("编号"))
            continue
        fields = fields_for(task, config, now.tzinfo)
        waits = getattr(config, "intake_mode", "all") == "accepted"
        if waits:  # 全自动: a task someone assigned waits for the owner's yes before it counts
            fields.update({"状态": ["待确认"], "进度": "0/1 待确认"})
        receipt = gateway.create(fields, key, "feishu-task")
        created.append(receipt["record_id_list"][0])
        if waits:
            from ..thread import judge
            judge(client.root, created[-1], "ask", "别人在飞书任务里分给你的，等你确认接不接")
    # Completion follows the Feishu task, whether the item was created from it or merged with it.
    by_link = {}
    for r in all_rows:
        g = task_guid(r.get("关联任务链接")) or (str(r.get("去重键") or "").split(":", 1)[1] if str(r.get("去重键") or "").startswith("feishu-task:") else None)
        if g:
            by_link[g] = r
    for guid, row in by_link.items():
        task = done_tasks.get(guid)
        if task and single(row.get("状态")) not in {"完成", "取消"} and single(row.get("执行方")) == "我":
            gateway.update(row, {"状态": ["完成"], "进度": "完成", "完成时间": task.get("completed_at") or now.isoformat()},
                           f"feishu-task-done:{row['record_id']}", "feishu-task")
            completed.append(row["record_id"])
            try:
                from ..thread import closed
                closed(client.root, row["record_id"], "你", "在飞书任务里勾了完成")
            except Exception as exc:
                activity.swallowed(client.root, "sources.feishu_tasks.sync", exc)
    if merged:
        from .. import activity
        activity.log(client.root, "detect", "飞书任务：" + "、".join(f"#{n}" for n in merged) + " 是已有的事，已关联，不重复建")
    message_id = None
    if created and notify:
        numbers = {r["record_id"]: number(r["编号"]) for r in gateway.rows() if r["record_id"] in created}
        titles = {r["record_id"]: str(r.get("标题"))[:14] for r in gateway.rows() if r["record_id"] in created}
        listed = " / ".join(f"#{numbers[x]} {titles[x]}" for x in created[:4]) + (f" 等 {len(created)} 件" if len(created) > 4 else "")
        head = "飞书任务 · 首次导入近两周的 " if first else "飞书任务 · 新增 "
        text = f"{head}{len(created)} 件：{listed}\n不是你的回「编号 给某人」，不做回「编号 不做」"
        from ..delivery import send_once
        from hashlib import sha256
        key = "feishu-tasks-new:" + sha256(",".join(sorted(created)).encode()).hexdigest()[:24]
        message_id = send_once(state, key, lambda: client.send_self(text, key.replace(":", "-"))).get("message_id")
    if created or completed:
        from .. import activity
        activity.log(client.root, "detect", "飞书任务：" + "，".join(x for x in [f"新增 {len(created)} 件" if created else "",
                                                                            f"你勾完成 {len(completed)} 件" if completed else ""] if x))
    state.set("feishu-tasks:last-sync", now.isoformat())
    return {"created": len(created), "completed": len(completed), "message_id": message_id}
