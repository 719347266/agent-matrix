"""Jira issues assigned to the owner (and to the people the owner follows) → the task table. Pure code, no model.

- Every open issue (statusCategory != Done) assigned to the owner → one row (executor 我, source Jira, link back).
- One assigned to a followed person (config.jira_others) → a row with executor 别人, the name in front of the title.
- One nobody has taken yet in a watched project (config.jira_unassigned_projects), still in a 待办 status → a row
  waiting for the owner's yes. "Taken" is 经办人, or the field set in config.jira_owner_field (e.g. Issue Owner).
- The issue moves to a Done status in Jira → the row becomes 完成.
- The issue's description, translated into Chinese (translator passed in by the caller), goes into 原话引用 (shown under 来源).
- Read only: nothing is ever written back to Jira.
Auth: Jira Cloud site + account email + API token (id.atlassian.com/manage-profile/security/api-tokens).
"""
import base64
from hashlib import sha1
import json
from urllib.error import HTTPError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from .. import activity
from ..models import MatrixError

OPEN_JQL = "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC"
FIELDS = ["summary", "duedate", "status", "assignee", "project", "description"]


def _owner_field(config):
    return (config.jira_owner_field or {}).get("id") or ""


def _extra(config):
    """Fields read on top of FIELDS: the owner field and the ones shown in the title."""
    ids = [_owner_field(config)] + [x["id"] for x in config.jira_title_fields]
    return list(dict.fromkeys(i for i in ids if i))


BLANK = {"", "n/a", "na", "tbd", "none", "-"}


def _labels(issue, config):
    """Values of the title fields, one per field name (same-named fields: the first filled one)."""
    f, out = issue.get("fields") or {}, {}
    for x in config.jira_title_fields:
        v = f.get(x["id"])
        v = v.get("value") or v.get("name") if isinstance(v, dict) else v
        if x["name"] not in out and isinstance(v, (str, int, float)) and str(v).strip().lower() not in BLANK:
            out[x["name"]] = str(v).strip()
    return list(out.values())


def open_jql(config):
    ids = [o["id"] for o in config.jira_others if o.get("id")]
    projects, owner = config.jira_unassigned_projects, _owner_field(config)
    if not ids and not projects and not owner:
        return OPEN_JQL
    people = ", ".join(["currentUser()"] + [json.dumps(i) for i in ids])
    cf = f"cf[{owner.rsplit('_', 1)[-1]}]" if owner else ""
    who = [f"assignee in ({people})"] + ([f"{cf} in ({people})"] if cf else [])
    if projects:
        nobody = f"{cf} is EMPTY" if cf else "assignee is EMPTY"
        who.append(f'(project in ({", ".join(json.dumps(p) for p in projects)}) AND {nobody} AND statusCategory = "To Do")')
    return f"({' OR '.join(who)}) AND statusCategory != Done ORDER BY updated DESC"


def _handler(issue, config):
    """Who took the issue: the owner field when it is set and filled; else, in a watched project with that field,
    nobody; else 经办人. A user dict, or None."""
    f = issue.get("fields") or {}
    owner = _owner_field(config)
    if owner and isinstance(f.get(owner), dict):
        return f[owner]
    if owner and (f.get("project") or {}).get("key") in set(config.jira_unassigned_projects):
        return None
    return f.get("assignee") or None


def _nobody(issue, config):
    """Nobody has taken it yet, in a project the owner watches for that."""
    f = issue.get("fields") or {}
    return _handler(issue, config) is None and (f.get("project") or {}).get("key") in set(config.jira_unassigned_projects)


def _other(issue, config):
    """The followed person who took this issue, or None when it is the owner's (or nobody's)."""
    aid = (_handler(issue, config) or {}).get("accountId")
    return next((o for o in config.jira_others if aid and o.get("id") == aid), None)


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
        body = {"jql": jql, "fields": FIELDS + _extra(config), "maxResults": 100, **({"nextPageToken": token} if token else {})}
        data = api(config, "POST", "/rest/api/3/search/jql", body)
        issues += data.get("issues") or []
        token = data.get("nextPageToken")
        if data.get("isLast", True) or not token:
            return issues


def connected(config):
    return bool(config.jira_site and config.jira_email and config.jira_token)


def fields_for(issue, config):
    f = issue.get("fields") or {}
    other = _other(issue, config)
    tag = other["name"] + " · " if other else "没人领 · " if _nobody(issue, config) else ""
    labels = "".join(f"[{v}]" for v in _labels(issue, config))
    title = " ".join(f"{tag}{labels} {issue['key']} {f.get('summary') or ''}".split())[:60]
    out = {"标题": title, "执行方": ["别人" if other else "我"], "状态": ["未开始"], "进度": "0/1 未开始",
           "负责人": [{"id": config.owner_open_id}], "来源": ["Jira"], "类型": ["临时"],
           "来源链接": f"{site_url(config.jira_site)}/browse/{issue['key']}"}
    if f.get("duedate"):
        out["截止日"] = f["duedate"] + " 00:00"
    return out


def plain(node):
    """Jira's description (Atlassian document format, or plain text on old issues) → plain text."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    if kind == "text":
        return node.get("text") or ""
    if kind == "hardBreak":
        return "\n"
    if kind in ("mention", "emoji", "status"):
        return (node.get("attrs") or {}).get("text") or ""
    if kind in ("inlineCard", "blockCard"):
        return (node.get("attrs") or {}).get("url") or ""
    inner = "".join(plain(c) for c in node.get("content") or [])
    return inner + "\n" if kind in ("paragraph", "heading", "listItem", "codeBlock", "blockquote", "tableRow") else inner


def _original(issue):
    f = issue.get("fields") or {}
    body = "\n".join(line.rstrip() for line in plain(f.get("description")).splitlines() if line.strip())
    return f"{f.get('summary') or ''}\n\n{body}"[:3000] if body else ""


TRANSLATE_PER_ROUND = 10
TRANSLATE_RETRY_MINUTES = 60


def _translations(root, state, now, issues, *, translate):
    """Translations for these issues (newest first, a handful per round); {} without a translator or while a failed
    try is cooling down. The translator ({key: text} → {key: 中文}) comes from the caller: this layer runs no AI."""
    if translate is None:
        return {}
    texts = {i["key"]: t for i in issues if (t := _original(i))}
    texts = dict(list(texts.items())[:TRANSLATE_PER_ROUND])
    failed = state.get("jira:translate-failed")
    if not texts or failed and now.isoformat() < failed:
        return {}
    try:
        return translate(root, texts)
    except Exception as exc:   # no AI set up, a timeout …: the sync itself goes on
        from datetime import timedelta
        state.set("jira:translate-failed", (now + timedelta(minutes=TRANSLATE_RETRY_MINUTES)).isoformat())
        activity.swallowed(root, "sources.jira.translate", exc)
        return {}


def sync(config, client, state, now, *, api=call, translate=None):
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
    open_issues = {i["key"]: i for i in search(config, open_jql(config), api=api)}
    tracked = {k: r for k, r in by_key.items()
               if single(r.get("状态")) not in {"完成", "取消"} and single(r.get("执行方")) in {"我", "别人"}}
    fresh = [i for k, i in open_issues.items() if k not in by_key and not state.get("jira:merged:" + k)]
    untranslated = [open_issues[k] for k, r in tracked.items() if k in open_issues and not r.get("原话引用")]
    chinese = _translations(client.root, state, now, fresh + untranslated, translate=translate)
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
        if chinese.get(key):
            fields["原话引用"] = chinese[key]
        nobody = _nobody(issue, config)
        waits = nobody or (getattr(config, "intake_mode", "all") == "accepted" and fields["执行方"] == ["我"])
        if waits:   # 全自动 (or nobody has it yet): waits for the owner's yes
            fields.update({"状态": ["待确认"], "进度": "0/1 待确认"})
        rid = gateway.create(fields, "jira:" + key, "jira")["record_id_list"][0]
        created.append(key)
        created_ids.append(rid)
        if waits:
            from ..thread import judge
            judge(client.root, rid, "ask", "Jira 里还没人领的单子，待确认是否接下" if nobody
                  else "他人在 Jira 里指派给你")
    # Rows this sync made and still open (``tracked`` above). Merged rows keep the owner's own judgment.
    closed_rows, edited = [], []
    for key, row in tracked.items():
        if key in open_issues:   # still open and still followed: follow a new title / due date / assignee
            want = fields_for(open_issues[key], config)
            patch = {k: want[k] for k in ("标题", "截止日", "执行方") if want.get(k) and want[k] != row.get(k)}
            if chinese.get(key) and not row.get("原话引用"):   # rows made before translations, or a missed round
                patch["原话引用"] = chinese[key]
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
        waiting = "，待确认是否接下" if any("待确认" in (r.get("状态") or []) for r in made) else ""
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
    """Why an issue is no longer open-and-followed: ("完成"|"取消", by, why), or None when it still is (a race)."""
    try:
        issue = api(config, "GET", f"/rest/api/3/issue/{key}?fields=" + ",".join(["status", "assignee", "project"] + ([_owner_field(config)] if _owner_field(config) else [])))
    except JiraHTTPError as exc:
        if exc.status in (404, 403):
            return "取消", "Jira", "Jira 里这个单子删了，或你已经看不到它"
        raise
    f = issue.get("fields") or {}
    if ((f.get("status") or {}).get("statusCategory") or {}).get("key") == "done":
        return "完成", "Jira", "在 Jira 里改成了完成"
    taken = _handler(issue, config)
    if not taken:
        if (f.get("project") or {}).get("key") in set(config.jira_unassigned_projects):
            return None
        return "取消", "Jira", "没人领，且不再看这个项目没人领的单子"
    me = _me(config, state, api)
    if not me:
        return None   # without the owner's id a reassignment cannot be told apart: close nothing
    if taken.get("accountId") not in {me} | {o.get("id") for o in config.jira_others}:
        return "取消", "Jira", f"在 Jira 里给了{taken.get('displayName') or '别人'}（不在你跟的人里）"
    return None


def _find_user(config, name, api):
    """One Jira user for a name or email the owner typed; several or none → a plain message to type it fully."""
    name = (name or "").strip()
    if not name:
        raise MatrixError("填一个名字或邮箱。")
    found = [u for u in api(config, "GET", "/rest/api/3/user/search?maxResults=20&query=" + quote(name)) or []
             if u.get("accountId") and u.get("accountType", "atlassian") == "atlassian" and u.get("active", True)]
    exact = [u for u in found if name.lower() in {(u.get("displayName") or "").lower(), (u.get("emailAddress") or "").lower()}]
    pick = exact if exact else found
    if len(pick) == 1:
        return pick[0]
    if not pick:
        raise MatrixError(f"Jira 里没找到「{name}」，换成全名或邮箱试试。")
    raise MatrixError(f"「{name}」对上了好几个人：{'、'.join(u.get('displayName') or '?' for u in pick[:5])}，写全名或邮箱。")


def sync_now(root, *, api=call, translate=None):
    """The 「立即同步」 button: one sync right away, under the same project lock as the 10-minute timer
    (waits up to 20 s behind a running round)."""
    from datetime import datetime
    from ..config import load
    from ..feishu import Feishu
    from ..state import State, exclusive_lock
    from ..timeutil import TZ
    config = load(root / "config.yaml")
    if not connected(config):
        raise MatrixError("先连接 Jira。")
    with exclusive_lock(root / "state", wait=20):
        state = State(root / "state/state.db")
        try:
            client = Feishu(config, root)
            client.verify_owner(client.auth())   # same check as the timer before it touches the table
            return sync(config, client, state, datetime.now(TZ), api=api, translate=translate)
        finally:
            state.close()


def watch_projects(root, keys, field=None, *, api=call):
    """The projects whose not-yet-taken issues come in (each key checked with Jira first), and optionally the field
    that says who took one (by its name; "" or 经办人 = assignee)."""
    from ..config import load, save
    path = root / "config.yaml"
    config = load(path)
    if not connected(config):
        raise MatrixError("先连接 Jira。")
    wanted = list(dict.fromkeys(k.strip().upper() for k in keys if k and k.strip()))
    for k in wanted:
        try:
            api(config, "GET", f"/rest/api/3/project/{quote(k)}")
        except JiraHTTPError as exc:
            if exc.status in (404, 403):
                raise MatrixError(f"Jira 里没有项目 {k}，或你看不到它。填项目 key（单号前面那段，比如 LGS-123 的 LGS）。") from None
            raise
    config.jira_unassigned_projects = wanted
    if field is not None:
        name = field.strip()
        if name.lower() in {"", "经办人", "assignee"}:
            config.jira_owner_field = {}
        else:
            hit = [x for x in api(config, "GET", "/rest/api/3/field") or [] if (x.get("name") or "").lower() == name.lower()]
            if not hit:
                raise MatrixError(f"Jira 里没有叫「{name}」的字段，照单子页面上显示的名字填。")
            config.jira_owner_field = {"id": hit[0]["id"], "name": hit[0]["name"]}
    save(path, config)
    return {"projects": wanted, "field": config.jira_owner_field.get("name", "")}


def title_fields(root, names, *, api=call):
    """The fields shown in front of item titles, by the names Jira shows (every field with that name)."""
    from ..config import load, save
    path = root / "config.yaml"
    config = load(path)
    if not connected(config):
        raise MatrixError("先连接 Jira。")
    wanted = list(dict.fromkeys(n.strip() for n in names if n and n.strip()))
    all_fields = api(config, "GET", "/rest/api/3/field") or [] if wanted else []
    picked = []
    for name in wanted:
        hit = [x for x in all_fields if (x.get("name") or "").lower() == name.lower()]
        if not hit:
            raise MatrixError(f"Jira 里没有叫「{name}」的字段，照单子页面上显示的名字填。")
        picked += [{"id": x["id"], "name": hit[0]["name"]} for x in hit]
    config.jira_title_fields = picked
    save(path, config)
    return list(dict.fromkeys(x["name"] for x in picked))


def follow(root, name=None, remove=None, *, api=call):
    """Add a person (looked up in Jira by name or email) or remove one (by accountId); returns the list."""
    from ..config import load, save
    path = root / "config.yaml"
    config = load(path)
    if not connected(config):
        raise MatrixError("先连接 Jira。")
    from ..state import State
    others = [o for o in config.jira_others if o.get("id") != remove]
    if name:
        u = _find_user(config, name, api)
        state = State(root / "state/state.db")
        try:
            me = _me(config, state, api)
        finally:
            state.close()
        if u["accountId"] == me:
            raise MatrixError("这是你自己，指派给你的单子本来就会收进来。")
        if all(o["id"] != u["accountId"] for o in others):
            others.append({"id": u["accountId"], "name": u.get("displayName") or name})
    config.jira_others = others
    save(path, config)
    return others



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
