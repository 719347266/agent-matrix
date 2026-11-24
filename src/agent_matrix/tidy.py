"""整理记忆：once a day the AI that drives 心响 reads the whole memory and proposes changes; nothing changes until the owner
says 用 (Letta's sleep-time tidying; Zep's newer fact replaces the older one).

    due(root)              is there anything new since the last tidy, and has a day passed? (cheap, no model)
    prepare(root, folder)  the files the AI reads: rules with their record, 30 days of moments, projects and versions,
                           关于你, recent verdicts, items not yet in a project or version
    take(root, plan)       check tidy.json and keep each proposal as a 记忆更新 waiting for the owner
    pending / use / drop   what waits; 用 applies it with the existing memory functions, 不要 changes nothing

Kept in state/xinxiang.db (memory_updates). Each proposal: its type, what it says, why, and its state (待定/已用/不要).
"""
from datetime import datetime, timedelta
import json
import re

from . import db as database
from .timeutil import TZ

EVERY_HOURS = 24
DAYS = 30
MAX_CHANGES = 30
MAX_PROFILE = 2
LABEL = {"merge_rules": "合并规矩", "retire_rule": "退休规矩", "rule_trigger": "规矩归到动作", "stale_moment": "经历过时", "place_item": "归类事项",
         "version_status": "版本状态", "profile": "关于你"}
STATE = {"pending": "待定", "used": "已用", "dropped": "不要", "superseded": "已有更新"}


def _db(root):
    return database.open(root, "xinxiang")


def _now():
    return datetime.now(TZ).isoformat(timespec="seconds")


def _rows(root):
    from .snapshot import _snapshot
    return [r for r in _snapshot(root).get("rows") or [] if r.get("编号")]


def _max_n(rows):
    return max((int(r["编号"]) for r in rows if str(r.get("编号")).isdigit()), default=0)


# ---- when ---------------------------------------------------------------------------------------------------

def _new_records(root, db, since):
    from .thread import setting
    cursor = setting(root, "tidy_cursor", {})
    result = {}
    for table, field in (("outcomes", "at"), ("moments", "created")):
        if cursor.get("at") == since and table in cursor:
            clause, value = "id>?", cursor[table]
        else:
            clause, value = field + ">?", since
        result[table] = [r[0] for r in db.execute("SELECT id FROM " + table + " WHERE " + clause + " ORDER BY id", (value,))]
    return result


def due(root, now=None):
    """None, or what is new since the last tidy. At most once a day, and only when there is something new."""
    from .thread import setting
    now = now or datetime.now(TZ)
    tried = setting(root, "tidy_tried_at", "")
    if tried and now - datetime.fromisoformat(tried) < timedelta(hours=EVERY_HOURS):
        return None
    since = setting(root, "tidied_at", "")
    db = _db(root)
    try:
        new = _new_records(root, db, since)
        verdicts, moments = len(new["outcomes"]), len(new["moments"])
    finally:
        db.close()
    items = max(0, _max_n(_rows(root)) - int(setting(root, "tidy_max_n", 0) or 0))
    if not (verdicts or moments or items):
        return None
    return {"verdicts": verdicts, "moments": moments, "items": items}


def tried(root):
    from .thread import set_setting
    set_setting(root, "tidy_tried_at", _now())


def done(root, cursor=None):
    from .thread import set_setting
    if cursor:
        set_setting(root, "tidy_cursor", cursor)
        set_setting(root, "tidied_at", cursor["at"])
        set_setting(root, "tidy_max_n", cursor["max_n"])
    else:  # a job prepared by a previous version
        set_setting(root, "tidied_at", _now())
        set_setting(root, "tidy_max_n", _max_n(_rows(root)))


# ---- what the AI reads ------------------------------------------------------------------------------------------

def prepare(root, folder):
    from . import memory, projects, versions
    from .thread import setting
    rows = _rows(root)
    last_n = setting(root, "tidy_max_n", 0) or 0
    db = _db(root)
    try:
        db.execute("BEGIN")
        cursor = {"at": _now(), "max_n": _max_n(rows)}
        new = _new_records(root, db, setting(root, "tidied_at", ""))
        for table in ("outcomes", "moments"):
            cursor[table] = db.execute("SELECT coalesce(max(id),0) FROM " + table).fetchone()[0]
    finally:
        db.close()
    since = (datetime.now(TZ) - timedelta(days=DAYS)).isoformat(timespec="seconds")
    write = lambda name, data: (folder / name).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    write("rules.json", [{k: r[k] for k in ("id", "rule", "why", "source", "scope", "scope_name", "wins", "losses")}
                         for r in memory.rules(root)])
    write("moments.json", [{"id": m["id"], "kind": memory.KIND_LABEL.get(m["kind"], m["kind"]), "text": m["text"],
                            "meeting": m["source_title"], "at": str(m["at"])[:16], "status": m["status"],
                            **({"wait_for": m["wait_for"]} if m.get("wait_for") else {})}
                           for m in memory.moments(root, limit=400) if str(m["at"]) >= since[:10] and m["status"] != "stale"])
    n_of = {r["record_id"]: r.get("编号") for r in rows}
    write("versions.json", {"projects": [p["name"] for p in projects.all(root)],
                            "versions": [{"project": v["project"], "version": v["name"], "status": v["status_label"], "period": v["period"],
                                          "note": v["note"], "items": [n_of[x["rid"]] for x in v["items"] if x["rid"] in n_of],
                                          "expected": [{k: e[k] for k in ("title", "why", "status")} for e in v["expected"]]}
                                         for v in versions.all(root)]})
    (folder / "profile.md").write_text(memory.profile(root)["text"] or "（还没有）", encoding="utf-8")
    by_rid = {r["record_id"]: r for r in rows}
    write("outcomes.json", [{**{k: v for k, v in o.items() if k != "record_id"}, "n": (by_rid.get(o["record_id"]) or {}).get("编号"),
                             "item": str((by_rid.get(o["record_id"]) or {}).get("标题") or "")} for o in memory.outcomes(root, since=since)][-80:])
    names = {p["id"]: p["name"] for p in projects.all(root)}
    chosen = projects.chosen(root)
    one = lambda v: (v[0] if isinstance(v, list) and v else v) or ""
    write("unplaced.json", [{"n": r["编号"], "title": r.get("标题"), "status": one(r.get("状态")), "project": names.get(chosen.get(r["record_id"]), "")}
                            for r in rows if not versions.of(root, r["record_id"]) and one(r.get("状态")) != "取消"][-200:])
    evidence = [f"moment:{i}" for i in new["moments"]] + [f"outcome:{i}" for i in new["outcomes"]]
    evidence += [f"item:{r['编号']}" for r in rows if str(r["编号"]).isdigit() and int(r["编号"]) > last_n]
    write("new-evidence.json", evidence)
    return {"cursor": cursor, "profile_evidence": evidence}


# ---- checking tidy.json ------------------------------------------------------------------------------------------

def _clean(text, n):
    return " ".join(str(text or "").split())[:n]


def _profile_change(text, old, new):
    """Return the edited text, or the original for an already-known fact."""
    compact = lambda value: " ".join(value.split()).rstrip("。.")
    if not new or len(new) > 2000:
        raise ValueError("new 应是一处完整的改动，最多 2000 字")
    if compact(old) == compact(new) or (not old and compact(new) in compact(text)):
        return text
    if old and text.count(old) != 1:
        raise ValueError("原句对不上或出现多次，请选能唯一定位的一处原文")
    headings = re.findall(r"^#{1,6} +.+$", new, re.M)
    if len(headings) > 1 or len(re.findall(r"^#{1,6} +.+$", old, re.M)) > 1:
        raise ValueError("每处更新只能改一个小节")
    if not old and text and not headings:
        raise ValueError("请摘录要补充的原文，或给全新的小节写标题")
    if not old and headings and headings[0] in text.splitlines():
        raise ValueError("已有同名小节，请在原段落补充")
    edited = text.replace(old, new, 1) if old else (text + "\n\n" + new).strip()
    from .memory import PROFILE_MAX
    if len(edited.strip()) > PROFILE_MAX:
        raise ValueError("修改后超出「关于你」的长度，请缩小这处改动")
    return edited


def check(root, plan, rows=None, *, profile_evidence=None):
    """(proposals, problems): each proposal is (type, content, why, data); problems say what to fix, one line each."""
    from . import memory, versions
    if not isinstance(plan, dict) or not isinstance(plan.get("changes"), list):
        return [], ["tidy.json 要写成 {\"changes\": [...]}"]
    kinds = {c["type"] for c in plan["changes"] if isinstance(c, dict) and isinstance(c.get("type"), str)}
    if rows is None:
        rows = _rows(root) if "place_item" in kinds else []
    rules = {r["id"]: r for r in memory.rules(root)} if kinds & {"merge_rules", "retire_rule", "rule_trigger"} else {}
    moments = {m["id"]: m for m in memory.moments(root, limit=2000)} if "stale_moment" in kinds else {}
    rid_of = {str(r.get("编号")): r["record_id"] for r in rows}
    title_of = {str(r.get("编号")): str(r.get("标题") or "") for r in rows}
    have = {(v["project"], v["name"]): v for v in versions.all(root)} if "version_status" in kinds else {}
    profile = memory.profile(root)["text"].strip() if "profile" in kinds else ""
    out, problems, profiles = [], [], 0
    for i, c in enumerate(plan["changes"][:MAX_CHANGES], 1):
        kind = (c or {}).get("type") if isinstance(c, dict) else None
        why = _clean((c or {}).get("why") if isinstance(c, dict) else "", 200)
        bad = lambda text: problems.append(f"第 {i} 条（{kind or '?'}）：{text}")
        if not isinstance(kind, str) or kind not in LABEL:
            bad("type 只能是 " + " / ".join(LABEL)); continue
        if not why:
            bad("要写一句 why（理由）"); continue
        if kind == "merge_rules":
            ids = [x for x in c.get("ids") or [] if isinstance(x, int)]
            rule = _clean(c.get("rule"), 120)
            if len(set(ids)) < 2 or any(x not in rules for x in ids) or not rule:
                bad("ids 要是 rules.json 里至少两条规矩的编号，rule 写合并后的一句"); continue
            ids = sorted(set(ids))
            scopes, triggers = {rules[x]["scope"] for x in ids}, {rules[x]["trigger"] for x in ids}
            out.append((kind, "「" + "」「".join(rules[x]["rule"] for x in ids) + f"」合并成「{rule}」", why,
                        {"ids": ids, "rule": rule, "scope": scopes.pop() if len(scopes) == 1 else "",
                         "trigger": triggers.pop() if len(triggers) == 1 else ""}))
        elif kind == "retire_rule":
            r = rules.get(c.get("id"))
            if not r or r["source"] != "AI 学的":
                bad("id 要是 rules.json 里来源为「AI 学的」的规矩"); continue
            out.append((kind, f"「{r['rule']}」（通过 {r['wins']} · 退回 {r['losses']}）不再用", why, {"id": r["id"]}))
        elif kind == "rule_trigger":
            r, trigger = rules.get(c.get("id")), memory.clean_trigger(c.get("trigger"))
            if not r or trigger == r["trigger"]:
                bad("id 要是 rules.json 里的规矩，trigger 写一个和现在不同的动作（每件事都要守就写空串）"); continue
            out.append((kind, f"「{r['rule']}」→ " + (f"做「{trigger}」时才读" if trigger else "每件事都读"), why,
                        {"id": r["id"], "trigger": trigger}))
        elif kind == "stale_moment":
            m, newer = moments.get(c.get("id")), moments.get(c.get("by"))
            if not m or m["status"] == "stale":
                bad("id 要是 moments.json 里的经历"); continue
            out.append((kind, f"「{m['text']}」（{m['source_title']} {str(m['at'])[5:10]}）已经不算数"
                        + (f"，新的是「{newer['text']}」" if newer else ""), why, {"id": m["id"], "by": newer["id"] if newer else None}))
        elif kind == "place_item":
            n, project, version = str(c.get("n") or ""), _clean(c.get("project"), 20), _clean(c.get("version"), 40)
            if n not in rid_of or not project:
                bad("n 要是 unplaced.json 里的编号，project 写项目名"); continue
            out.append((kind, f"#{n} {title_of[n]} → {project}" + (f" · {version}" if version else ""), why,
                        {"n": int(n), "rid": rid_of[n], "project": project, "version": version}))
        elif kind == "version_status":
            key, status = (_clean(c.get("project"), 20), _clean(c.get("version"), 40)), str(c.get("status") or "")
            if key not in have or status not in (*versions.STATUS.values(), *versions.STATUS):
                bad("project + version 要是 versions.json 里有的，status 只用 当前 / 下一版 / 规划中 / 已完成"); continue
            new = versions.status_of(status)
            if new == have[key]["status"]:
                bad("状态没变，不用写"); continue
            out.append((kind, f"{key[0]} · {key[1]}：{have[key]['status_label']} → {versions.STATUS[new]}", why,
                        {"project": key[0], "version": key[1], "status": new}))
        else:
            old, new = str(c.get("old") or "").strip(), str(c.get("new") or "").strip()
            try:
                if _profile_change(profile, old, new) == profile:
                    continue
            except ValueError as exc:
                bad(str(exc)); continue
            if profile_evidence is not None and c.get("evidence") not in profile_evidence:
                bad("关于你的变化要引用 new-evidence.json 里的本轮依据"); continue
            if profiles >= MAX_PROFILE:
                bad("每轮最多两处局部更新"); continue
            profiles += 1
            out.append((kind, new, why, {"old": old, "new": new}))
    return out, problems


# ---- the list the owner decides on --------------------------------------------------------------------------------

def save(root, proposals, job=""):
    """Keep proposals as 待定; the same one already waiting is not added twice. Returns how many are new."""
    db = _db(root)
    added = 0
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            for kind, content, why, data in proposals:
                anchor = data.get("old")
                data = json.dumps(data, ensure_ascii=False, sort_keys=True)
                if kind == "profile":
                    if db.execute("SELECT 1 FROM memory_updates WHERE kind='profile' AND data=? "
                                  "AND status IN ('pending','dropped')", (data,)).fetchone():
                        continue
                    if anchor:
                        db.execute("UPDATE memory_updates SET status='superseded',decided=? WHERE kind='profile' "
                                   "AND status='pending' AND json_extract(data,'$.old')=?", (_now(), anchor))
                if db.execute("SELECT 1 FROM memory_updates WHERE kind=? AND data=? AND status='pending'", (kind, data)).fetchone():
                    continue
                db.execute("INSERT INTO memory_updates (job, kind, content, why, data, status, created) VALUES (?,?,?,?,?,'pending',?)",
                           (job, kind, content[:2000], why, data, _now()))
                added += 1
    finally:
        db.close()
    return added


def _settled(root, rows):
    """A proposal to place an item that has been placed since (by the owner, or by 整理项目和版本) asks for nothing
    any more: it is closed quietly instead of waiting for a click."""
    from . import projects
    placed = projects.chosen(root)
    done = [i for i, k, c, w, d, s, at in rows if s == "pending" and k == "place_item" and json.loads(d).get("rid") in placed]
    for uid in done:
        _mark(root, uid, "dropped")
    return [r for r in rows if r[0] not in set(done)]


def updates(root, status="pending"):
    db = _db(root)
    try:
        q = "SELECT id, kind, content, why, data, status, created FROM memory_updates" + (" WHERE status=?" if status else "") + " ORDER BY id"
        rows = db.execute(q, (status,) if status else ()).fetchall()
    finally:
        db.close()
    rows = _settled(root, rows)
    return [{"id": i, "type": k, "label": LABEL.get(k, k), "content": c, "why": w or "", "data": json.loads(d), "status": s,
             "state": STATE.get(s, s), "created": str(at)[5:16].replace("T", " ")} for i, k, c, w, d, s, at in rows]


def _mark(root, uid, status):
    db = _db(root)
    try:
        with db:
            db.execute("UPDATE memory_updates SET status=?, decided=? WHERE id=? AND status='pending'", (status, _now(), int(uid)))
    finally:
        db.close()


def _apply(root, kind, d):
    """Make the change with the memory's own functions. Returns a problem in plain words, or ""."""
    from . import memory, projects, versions
    if kind == "merge_rules":
        rules = {r["id"]: r for r in memory.rules(root)}
        olds = [rules[x] for x in d["ids"] if x in rules]
        if len(olds) < 2:
            return "要合并的规矩已经不在了"
        mine = any(r["source"] in memory.OWNER_SOURCES for r in olds)
        memory.add_rule(root, d["rule"], "合并自：" + "；".join(r["rule"] for r in olds), source="本人" if mine else "AI 学的",
                        scope=d.get("scope", ""), trigger=d.get("trigger", ""))
        kept = next((r for r in memory.rules(root) if r["rule"] == d["rule"]), None)
        if kept:
            memory.confirm_rule(root, kept["id"])
        for r in olds:
            if not kept or r["id"] != kept["id"]:
                memory.remove_rule(root, r["id"])
    elif kind == "retire_rule":
        memory.remove_rule(root, d["id"])
    elif kind == "rule_trigger":
        if not any(r["id"] == d["id"] for r in memory.rules(root)):
            return "这条规矩已经不在了"
        memory.set_trigger(root, d["id"], d["trigger"])
    elif kind == "stale_moment":
        memory.set_status(root, d["id"], "stale")
    elif kind == "place_item":
        known = projects.find(root, d["project"])   # "示例" when the project is called "示例 App": the same one, not a new one
        name = known["name"] if known else d["project"]
        if d.get("version"):
            have = next((v for v in versions.all(root) if v["project"] == name and v["name"] == d["version"]), None)
            vid = have["id"] if have else versions.upsert(root, name, d["version"])
            versions.place(root, d["rid"], vid, by=versions.OWNER)
        else:
            projects.assign(root, [d["rid"]], known["id"] if known else projects.create(root, name))
    elif kind == "version_status":
        if not any(v["project"] == d["project"] and v["name"] == d["version"] for v in versions.all(root)):
            return "这个版本已经不在了"
        versions.upsert(root, d["project"], d["version"], d["status"])
    elif kind == "profile":
        text = memory.profile(root)["text"].strip()
        try:
            edited = _profile_change(text, d["old"], d["new"])
            if edited != text:
                memory.set_profile(root, edited, expected=text)
        except ValueError as exc:
            return str(exc)
    return ""


def use(root, ids):
    """The owner said 用: apply each one. Returns (used ids, {id: problem})."""
    waiting = {u["id"]: u for u in updates(root)}
    used, failed = [], {}
    for uid in ids:
        u = waiting.get(int(uid))
        if not u:
            continue
        problem = _apply(root, u["type"], u["data"])
        if problem:
            failed[u["id"]] = problem
            continue
        _mark(root, u["id"], "used")
        used.append(u["id"])
    return used, failed


def drop(root, ids):
    """The owner said 不要: the memory stays as it is."""
    for uid in ids:
        _mark(root, uid, "dropped")
