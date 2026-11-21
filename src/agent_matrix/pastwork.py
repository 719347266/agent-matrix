"""Earlier work an item should build on, read from this computer only (no network, no model).

  - 前置事项的成果: what the items strung before this one delivered (title, one-line summary, link or local copy),
    so the AI keeps their wording and conclusions;
  - 范例: deliverables the owner passed, of the same kind (or, when the kind can't be told, of the same
    project), with the opening of each; and the reasons the owner gave when sending the same kind back.
Used by background.build.
"""
import json
from pathlib import Path
import re

from . import itemlinks, memory, projects, thread

EXAMPLE_CAP = 1500
KIND_WORDS = [("周报", r"周报|周总结|周汇报"), ("会议材料", r"会议材料|议程|会前|开会|会上"),
              ("回复草稿", r"回复|回信|答复|草稿"), ("数据汇总", r"数据|汇总|统计|报表|盘点|指标"),
              ("方案", r"方案|规划|计划|策略|思路|设计")]


def _one(value):
    """A table cell as plain text (same reading as background._one)."""
    if isinstance(value, list):
        return "、".join(str(_one(x)) for x in value) if value else ""
    if isinstance(value, dict):
        return value.get("text") or value.get("name") or value.get("url") or value.get("id") or ""
    return "" if value is None else str(value)


def _local(root, record_id):
    return Path(root) / "state/work" / str(record_id) / "交付.md"


def kind_of(root, row):
    """The kind of deliverable this item makes: what an earlier round said in 结果.json, else guessed from its words."""
    try:
        kind = json.loads((_local(root, row.get("record_id")).parent / "结果.json").read_text(encoding="utf-8")).get("template")
        if kind and kind != "其他":
            return str(kind)
    except (OSError, ValueError, AttributeError):
        pass
    text = " ".join(_one(row.get(k)) for k in ("标题", "下一步动作", "完成标准"))
    return next((kind for kind, words in KIND_WORDS if re.search(words, text)), None)


def _opening(text, cap=EXAMPLE_CAP):
    """The start of a deliverable, cut at a paragraph (or line) end when it is too long."""
    text = text.strip()
    if len(text) <= cap:
        return text
    head = text[:cap]
    for mark in ("\n\n", "\n"):
        at = head.rfind(mark)
        if at >= cap // 2:
            return head[:at].rstrip() + "\n……"
    return head.rstrip() + "……"


def _number_title(by_id, rid):
    r = by_id.get(rid) or {}
    n = str(r.get("编号") or "").strip()
    return (f"#{n} " if n else "") + (_one(r.get("标题")) or "（标题不详）")


def upstream_results(root, rows, row, limit=4):
    """Lines for the items before this one that already delivered, nearest first."""
    by_id = {r.get("record_id"): r for r in rows}
    ids = itemlinks.upstream(root, row.get("record_id"))
    if not ids:
        return []
    history = thread.history(root, ids)
    out = []
    for rid in ids:
        r = by_id.get(rid) or {}
        versions = (history.get(rid) or {}).get("deliverables") or []
        last = versions[-1] if versions else {}
        local = _local(root, rid)
        url = last.get("url") or _one(r.get("交付物链接"))
        if not versions and not url and not local.is_file():
            continue  # nothing delivered yet
        summary = " ".join(str(last.get("summary") or _one(r.get("预做结果摘要")) or "").split())[:120]
        line = f"- {_number_title(by_id, rid)}" + (f"（第 {last['v']} 版）" if last.get("v") else "")
        line += f"：{summary}" if summary else "：没写摘要，读原文"
        if url:
            line += f"\n  链接：{url}"
        if local.is_file():
            line += f"\n  本机副本：`{local}`"
        out.append(line)
        if len(out) >= limit:
            break
    return out


def _project_name(root, rows, record_id):
    try:
        pid = (projects.place(root, rows).get(record_id) or (None,))[0]
        return next((p["name"] for p in projects.all(root) if p["id"] == pid), "") if pid else ""
    except Exception:
        return ""


def examples(root, rows, row, limit=2, rejected_limit=3):
    """(basis, [(title, rid, opening)], [reason lines]): passed deliverables to copy the shape of, and why the
    same kind came back. Each item counts once, by its latest verdict."""
    me = row.get("record_id")
    latest = {}
    for o in memory.outcomes(root):  # oldest first, so the last one per item wins
        if o["record_id"] != me:
            latest[o["record_id"]] = o
    if not latest:
        return None, [], []
    recent = sorted(latest.values(), key=lambda o: o["at"], reverse=True)
    kind, project = kind_of(root, row), _project_name(root, rows, me)
    choices = []
    if kind:
        choices.append((f"同为「{kind}」", lambda o: o["template"] == kind))
    if project:
        choices.append((f"同在项目「{project}」", lambda o: o["project"] == project))
    by_id = {r.get("record_id"): r for r in rows}
    for basis, same in choices:
        passed = []
        for o in recent:
            if o["verdict"] == "通过" and same(o) and _local(root, o["record_id"]).is_file():
                text = _local(root, o["record_id"]).read_text(encoding="utf-8", errors="ignore")
                if text.strip():
                    passed.append((_number_title(by_id, o["record_id"]), o["record_id"], _opening(text)))
            if len(passed) >= limit:
                break
        reasons, seen = [], set()
        for o in recent:
            reason = " ".join(o["reason"].split())[:200]
            if o["verdict"] in ("退回", "要改") and same(o) and reason and reason not in seen:
                seen.add(reason)
                reasons.append(f"- {reason}（{_number_title(by_id, o['record_id'])}，{o['verdict']}）")
            if len(reasons) >= rejected_limit:
                break
        if passed or reasons:
            return basis, passed, reasons
    return None, [], []


def example_lines(root, rows, row):
    """The 范例 section body for 背景.md, or [] when there is nothing to show."""
    basis, passed, reasons = examples(root, rows, row)
    lines = []
    if passed:
        lines.append(f"{basis}、本人通过过的交付。这是本人通过过的写法，照它的结构和语气，不要照抄内容。")
        for title, rid, opening in passed:
            lines += ["", f"### {title}", f"全文：`{_local(root, rid)}`", "",
                      *[f"> {x}".rstrip() for x in opening.splitlines()]]
    if reasons:
        lines += ["", f"{basis}的交付最近被退回的原因（避开这些）：", *reasons]
    return lines
