"""Jira issues assigned to the owner → the task table. Pure code, no model.

- Every open issue (statusCategory != Done) assigned to the owner → one row (executor 我, source Jira, link back).
- The issue moves to a Done status in Jira → the row becomes 完成.
- Read only: nothing is ever written back to Jira.
Auth: Jira Cloud site + account email + API token (id.atlassian.com/manage-profile/security/api-tokens).
"""
import base64
from hashlib import sha1
import json
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .. import activity
from ..models import MatrixError

OPEN_JQL = "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC"
FIELDS = ["summary", "duedate", "status"]


class JiraHTTPError(MatrixError):
    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


def site_url(site):
    site = (site or "").strip().rstrip("/")
    if site and "://" not in site:
        site = "https://" + site
    host = urlparse(site).hostname or ""
    if not host:
        raise MatrixError("Jira 地址不对，填成 https://你的公司.atlassian.net 这样。")
    return "https://" + host


def call(config, method, path, body=None):
    auth = base64.b64encode(f"{config.jira_email}:{config.jira_token}".encode()).decode()
    req = Request(site_url(config.jira_site) + path, method=method,
                  data=json.dumps(body).encode() if body is not None else None,
                  headers={"Authorization": "Basic " + auth, "Accept": "application/json", "Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except HTTPError as exc:   # status only, never the token
        why = {401: "邮箱或 API Token 不对", 403: "这个账号没有权限",
               404: "没找到（站点地址不对，或这个单子不在了）"}.get(exc.code, f"HTTP {exc.code}")
        raise JiraHTTPError(f"Jira {path.split('?')[0]} 失败：{why}", exc.code) from None


def search(config, jql, *, api=call):
    issues, token = [], None
    while True:
        body = {"jql": jql, "fields": FIELDS, "maxResults": 100, **({"nextPageToken": token} if token else {})}
        data = api(config, "POST", "/rest/api/3/search/jql", body)
        issues += data.get("issues") or []
        token = data.get("nextPageToken")
        if data.get("isLast", True) or not token:
            return issues


def connected(config):
    return bool(config.jira_site and config.jira_email and config.jira_token)


def fields_for(issue, config):
    f = issue.get("fields") or {}
    title = " ".join(f"{issue['key']} {f.get('summary') or ''}".split())[:60]
    out = {"标题": title, "执行方": ["我"], "状态": ["未开始"], "进度": "0/1 未开始",
           "负责人": [{"id": config.owner_open_id}], "来源": ["Jira"], "类型": ["临时"],
           "来源链接": f"{site_url(config.jira_site)}/browse/{issue['key']}"}
    if f.get("duedate"):
        out["截止日"] = f["duedate"] + " 00:00"
    return out


def sync(config, client, state, now, *, api=call):
    if not connected(config):
        return "no_token"
    from ..brief_migration import single
    from ..itemstore import feishu_free
    from ..merge import find_same, merge_into
    from ..workflow import Gateway, ensure_options
    if not state.get("jira:source-option") and not feishu_free(config):
        ensure_options(client, state, "来源", ["Jira"])
        state.set("jira:source-option", True)
    gateway = Gateway(client, state)
    rows = gateway.rows()
    by_key = {str(r.get("去重键") or "")[5:]: r for r in rows if str(r.get("去重键") or "").startswith("jira:")}
    others = [r for r in rows if not str(r.get("去重键") or "").startswith("jira:")]   # two issues are never one thing
    created, merged, completed, created_ids = [], [], [], []
    open_issues = {i["key"]: i for i in search(config, OPEN_JQL, api=api)}
    for issue in open_issues.values():
        key = issue["key"]
        if key in by_key or state.get("jira:merged:" + key):
            continue
        summary = (issue.get("fields") or {}).get("summary") or ""
        target, _, _ = find_same(others, summary)
        if target is not None:   # a meeting or chat already brought in the same thing
            merge_into(gateway, target, title=f"{key} {summary}", source="Jira", key="jira-merge:" + key, now=now)
            state.set("jira:merged:" + key, target["record_id"])
            merged.append(key)
            continue
        fields = fields_for(issue, config)
        waits = getattr(config, "intake_mode", "all") == "accepted"
        if waits:   # 全自动: an issue someone assigned waits for the owner's yes
            fields.update({"状态": ["待确认"], "进度": "0/1 待确认"})
        rid = gateway.create(fields, "jira:" + key, "jira")["record_id_list"][0]
        created.append(key)
        created_ids.append(rid)
        if waits:
            from ..thread import judge
            judge(client.root, rid, "ask", "别人在 Jira 里指派给你的，等你确认接不接")
    # Rows this sync made and still open. Merged rows keep the owner's own judgment.
    tracked = {k: r for k, r in by_key.items()
               if single(r.get("状态")) not in {"完成", "取消"} and single(r.get("执行方")) == "我"}
    closed_rows, edited = [], []
    for key, row in tracked.items():
        if key in open_issues:   # still open and still the owner's: follow a new title / due date
            want = fields_for(open_issues[key], config)
            patch = {k: want[k] for k in ("标题", "截止日") if want.get(k) and want[k] != row.get(k)}
            if patch:
                gateway.update(row, patch, f"jira-edit:{row['record_id']}:{sha1(json.dumps(patch, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:10]}", "jira")
                edited.append(key)
            continue
        # dropped out of "open and mine": look at this one issue; a failure skips only it, the next round tries again
        try:
            how = _left(config, state, key, api)
        except MatrixError as exc:
            activity.swallowed(client.root, f"sources.jira.sync {key}", exc)
            continue
        if not how:
            continue
        status, by, why = how
        patch = {"状态": [status], "进度": status}
        if status == "完成":
            patch["完成时间"] = now.isoformat()
        gateway.update(row, patch, f"jira-{'done' if status == '完成' else 'gone'}:{row['record_id']}", "jira")
        (completed if status == "完成" else closed_rows).append(key)
        try:
            from ..thread import closed
            closed(client.root, row["record_id"], "Jira", why)
        except Exception as exc:
            activity.swallowed(client.root, "sources.jira.sync", exc)
    if created:
        _announce(client, state, gateway, created_ids)
    if created or merged or completed or closed_rows or edited:
        activity.log(client.root, "detect", "Jira：" + "，".join(x for x in [
            f"新增 {len(created)} 件（{'、'.join(created[:5])}）" if created else "",
            f"{len(merged)} 件是已有的事，已关联" if merged else "",
            f"完成 {len(completed)} 件" if completed else "",
            f"{len(closed_rows)} 件不再是你的（改派或删除），已关掉" if closed_rows else "",
            f"{len(edited)} 件标题或截止日在 Jira 里改了，已跟上" if edited else ""] if x))
    state.set("jira:last-sync", now.isoformat())
    return {"created": len(created), "merged": len(merged), "completed": len(completed),
            "closed": len(closed_rows), "edited": len(edited)}


def _announce(client, state, gateway, record_ids):
    """The same 「新事项」 notice a chat round sends: 「Jira · 新增 2 件：你 #12 #13」, once per batch of issues.
    A notice that cannot go out never undoes the sync."""
    from hashlib import sha256
    from ..brief_migration import number
    from ..delivery import send_once
    try:
        made = [r for r in gateway.rows() if r["record_id"] in set(record_ids)]
        nums = " ".join(f"#{number(r['编号'])}" for r in made if r.get("编号") is not None)
        if not nums:
            return
        waiting = "，等你确认接不接" if any("待确认" in (r.get("状态") or []) for r in made) else ""
        text = f"Jira · 新增 {len(made)} 件：你 {nums}{waiting}\n回复：展开 {nums.split()[0].lstrip('#')} 看单子"
        key = "jira-new-" + sha256(",".join(sorted(record_ids)).encode()).hexdigest()[:24]
        send_once(state, key, lambda: client.send_self(text, key))
    except Exception as exc:
        activity.swallowed(client.root, "sources.jira.announce", exc)


def _me(config, state, api):
    """The owner's Jira accountId (saved by connect; fetched once for a connection made before it was saved)."""
    me = state.get("jira:account")
    if not me:
        me = api(config, "GET", "/rest/api/3/myself").get("accountId")
        state.set("jira:account", me)
    return me


def _left(config, state, key, api):
    """Why an issue is no longer open-and-mine: ("完成"|"取消", by, why), or None when it still is (a race)."""
    try:
        issue = api(config, "GET", f"/rest/api/3/issue/{key}?fields=status,assignee")
    except JiraHTTPError as exc:
        if exc.status in (404, 403):
            return "取消", "Jira", "Jira 里这个单子删了，或你已经看不到它"
        raise
    f = issue.get("fields") or {}
    if ((f.get("status") or {}).get("statusCategory") or {}).get("key") == "done":
        return "完成", "Jira", "在 Jira 里改成了完成"
    me = _me(config, state, api)
    if not me:
        return None   # without the owner's id a reassignment cannot be told apart: close nothing
    if ((f.get("assignee") or {}).get("accountId")) != me:
        who = (f.get("assignee") or {}).get("displayName") or "没人"
        return "取消", "Jira", f"在 Jira 里改派给了{who}"
    return None


def connect(root, site, email, token, *, api=call):
    """Check the credentials with Jira, then save them to config.yaml. Never echoes the token back."""
    from ..config import load, save
    from ..state import State
    path = root / "config.yaml"
    config = load(path)
    config.jira_site, config.jira_email, config.jira_token = site_url(site), (email or "").strip(), (token or "").strip()
    if not config.jira_email or not config.jira_token:
        raise MatrixError("邮箱和 API Token 都要填。")
    me = api(config, "GET", "/rest/api/3/myself")
    save(path, config)
    who = {"site": config.jira_site, "user": me.get("displayName"), "tail": config.jira_token[-4:]}
    state = State(root / "state/state.db")
    try:
        state.set("jira:identity", who)
        state.set("jira:account", me.get("accountId"))
    finally:
        state.close()
    return who


def disconnect(root):
    from ..config import load, save
    from ..state import State
    path = root / "config.yaml"
    config = load(path)
    config.jira_token = ""
    save(path, config)
    state = State(root / "state/state.db")
    try:
        state.set("jira:identity", None)
        state.set("jira:account", None)
    finally:
        state.close()
