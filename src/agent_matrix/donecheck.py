"""看起来办完了: the owner's own open items, checked now and then against what happened since (the chat after the
promise, documents the AI finds), so the ones already done can be closed with one tap instead of piling up.

    due(root, rows, now)   which of the owner's items are due a check: open, the owner's, not checked for a while
    accept(...)            the AI's verdict, kept only with words that are really there (or a link it found)
    hints / dismiss        what the page shows; 「没办完」 drops it and the same words are not suggested again

The AI only suggests. Closing is the owner's tap (the ordinary 「完成」 command).
"""
from datetime import datetime, timedelta
from pathlib import Path
import re

OPEN = ("未开始", "进行中", "卡住")
EVERY = timedelta(hours=6)       # one item is looked at again after this long
SETTLE = timedelta(minutes=30)   # a brand-new item is not checked: nothing can have happened yet
PER_ROUND = 8
CHECKED, HINTS = "donecheck:checked", "donecheck:hints"


def _one(value):
    return (value[0] if value else "") if isinstance(value, list) else (value or "")


def _when(value):
    try:
        return datetime.fromisoformat(str(value).replace(" ", "T"))
    except (TypeError, ValueError):
        return None


def _get(root, key):
    from .thread import setting
    value = setting(Path(root), key, {}) or {}
    return value if isinstance(value, dict) else {}


def _put(root, key, value):
    from .thread import set_setting
    set_setting(Path(root), key, value)


def due(root, rows, now):
    """The owner's open items worth a look now, oldest check first, at most PER_ROUND."""
    checked, hints = _get(root, CHECKED), _get(root, HINTS)
    out = []
    for r in rows:
        rid = r.get("record_id")
        if not rid or r.get("排除本人提醒") or _one(r.get("执行方")) != "我" or _one(r.get("状态")) not in OPEN:
            continue
        if (h := hints.get(rid)) and not h.get("dismissed"):
            continue   # already suggested: waiting on the owner's tap
        made = _when(r.get("创建时间"))
        if made and made.tzinfo and now - made < SETTLE:
            continue
        last = _when(checked.get(rid))
        if last and now - last < EVERY:
            continue
        out.append((checked.get(rid) or "", r))
    return [r for _, r in sorted(out, key=lambda x: x[0])][:PER_ROUND]


def mark_checked(root, rids, now):
    checked = _get(root, CHECKED)
    for rid in rids:
        checked[rid] = now.isoformat(timespec="seconds")
    _put(root, CHECKED, checked)


def _squash(text):
    return re.sub(r"\s+", "", str(text or ""))


def accept(root, item, verdict, now):
    """Keep one verdict if it holds up: done, with words copied from what the AI was given (or a link to where it
    found them), and not the words the owner already said were wrong. Returns the hint kept, or None."""
    if not isinstance(verdict, dict) or verdict.get("done") is not True:
        return None
    evidence = " ".join(str(verdict.get("evidence") or "").split())[:300]
    url = str(verdict.get("url") or "")
    url = url if url.startswith("https://") else ""
    if len(_squash(evidence)) < 4:
        return None
    seen = _squash("\n".join(item.get("after") or []))
    if _squash(evidence) not in seen and not url:
        return None   # words it did not see and cannot point to: a guess
    hints = _get(root, HINTS)
    old = hints.get(item["record_id"]) or {}
    if old.get("dismissed") and _squash(old.get("evidence")) == _squash(evidence):
        return None
    hint = {"evidence": evidence, "where": str(verdict.get("where") or "")[:80], "url": url, "at": now.isoformat(timespec="seconds")}
    hints[item["record_id"]] = hint
    _put(root, HINTS, hints)
    return hint


def hints(root):
    return _get(root, HINTS)


def dismiss(root, rid):
    """The owner says it is not done. Kept as dismissed so the same words are not offered again."""
    value = _get(root, HINTS)
    if not rid or rid not in value or value[rid].get("dismissed"):
        return False
    value[rid] = {**value[rid], "dismissed": True}
    _put(root, HINTS, value)
    return True
