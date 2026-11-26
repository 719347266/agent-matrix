"""One thing, one item: new input that is the same thing as an open item is merged, not re-created.

Layers, cheapest first:
  1. Same source key (already deduplicated where items are created).
  2. Same Feishu task link.
  3. Code similarity on the subject words of the titles (CJK character pairs):
       >= STRONG  → merged automatically,
       >= POSSIBLE → suggested to the owner as "可能重复" (they reply "31 合并到 3" or "31 不重复").
  4. For meeting and chat extraction the model also sees the open items and may set `same_as`.
Parents and their AI preparation children belong together and are never treated as duplicates.
Merging never overwrites the target's owner, date or status: new facts go into 补充材料.
"""
import re

from .textmatch import STRONG, POSSIBLE, VERBS, NOISE, subject, pairs, similarity  # noqa: F401









def _value(row, key):
    v = row.get(key)
    return v[0] if isinstance(v, list) and len(v) == 1 and isinstance(v[0], str) else v


def _number(row):
    try:
        return int(str(row.get("编号")).strip())
    except (TypeError, ValueError):
        return None


def _family(row):
    """Record ids this row is linked to as parent/child."""
    out = set()
    for x in row.get("父事项") or []:
        if isinstance(x, dict):
            out.add(x.get("id") or x.get("record_id"))
        elif isinstance(x, str):
            out.add(x)
    return out


def open_items(rows):
    return [r for r in rows if _value(r, "状态") not in {"完成", "取消"} and _number(r) is not None]


def task_guid(link):
    m = re.search(r"guid=([0-9a-f-]{36})", str(link or ""))
    return m.group(1) if m else None


def find_same(rows, title, *, task_link=None, exclude=()):
    """Best open item that is the same thing: (row, score, how) or (None, 0, None)."""
    guid = task_guid(task_link)
    best, score = None, 0.0
    for r in open_items(rows):
        if r.get("record_id") in exclude:
            continue
        if guid and task_guid(r.get("关联任务链接")) == guid:
            return r, 1.0, "同一个飞书任务"
        s = similarity(title, r.get("标题"))
        if s > score:
            best, score = r, s
    return (best, score, "标题相同的事") if best is not None and score >= STRONG else (None, score, None)


def duplicate_pairs(rows, distinct=()):
    """Open, visible items that look like the same thing, for the owner to confirm."""
    items = [r for r in open_items(rows) if not r.get("排除本人提醒")]
    skip = set(distinct)
    out = []
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            na, nb = _number(a), _number(b)
            key = f"{min(na, nb)}-{max(na, nb)}"
            if key in skip or b.get("record_id") in _family(a) or a.get("record_id") in _family(b):
                continue
            s = similarity(a.get("标题"), b.get("标题"))
            if s >= POSSIBLE:
                # keep the older item; the newer one is suggested to merge into it
                keep, drop = (a, b) if na < nb else (b, a)
                out.append({"keep": _number(keep), "keep_title": str(keep.get("标题"))[:26],
                            "drop": _number(drop), "drop_title": str(drop.get("标题"))[:26], "score": round(s, 2), "key": key})
    out.sort(key=lambda x: -x["score"])
    return out


def merge_into(gateway, target, *, title, source, detail="", task_link=None, deliverable=None, key, now):
    """Fold new information into an existing item instead of creating a second one."""
    line = f"{now:%m-%d} 并入（{source}）：{title}" + (f" — {detail[:120]}" if detail else "")
    patch = {"补充材料": ((target.get("补充材料") or "").rstrip() + "\n" + line).strip()}
    if task_link and not target.get("关联任务链接"):
        patch["关联任务链接"] = task_link
    if deliverable and not target.get("交付物链接"):
        patch["交付物链接"] = deliverable
    receipt = gateway.update(target, patch, key, "merge:" + source)
    try:
        from .prework import add_note
        add_note(gateway.client.root, target["record_id"], line)
    except Exception:
        pass
    try:
        from . import activity
        activity.log(gateway.client.root, "detect", f"同一件事：「{str(title)[:20]}」并入 #{_number(target)}")
    except Exception:
        pass
    return receipt
