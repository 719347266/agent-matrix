"""Whichever AI the owner hands 心响 to, drives it.

Nothing is chosen in advance. The owner pastes one message to an AI (Antigravity, Gemini, Claude Code, Cursor,
Cindy … any that can run a command). That AI says who it is (`xinxiang hello`), which makes it the one that does
the work, and then keeps asking for the next thing to do. 心响 learns the list of AIs on this computer from who
came and said hello; an AI that stops asking is shown as away, and sending it the message again brings it back.
Sending the message to another AI hands the work over to that one.
"""
from datetime import datetime, timedelta
import re
from pathlib import Path

from . import thread as conversation
from .timeutil import TZ

KEY = "ai:drivers"
AWAY_AFTER = timedelta(minutes=12)   # a waiting `next` asks again at least every ten minutes


def _now():
    return datetime.now(TZ)


def _all(root):
    return dict(conversation.setting(Path(root), KEY, None) or {})


def clean(name):
    return " ".join(re.sub(r"""['"\\<>&`]""", "", str(name or "")).split())[:40]


def setting_of(name):
    return "driver:" + name


def name_in(value):
    """The AI named by an engine setting ("driver:Antigravity"), or None for the built-in ones and Cindy's schedule."""
    value = str(value or "")
    return value[len("driver:"):] or None if value.startswith("driver:") else None


def hello(root, name, *, app="", model="", take=True):
    """An AI says who it is. With `take` (the usual case) it becomes the one that does the items and the sorting."""
    name = clean(name)
    if not name:
        raise ValueError("要说你叫什么：--name")
    root, known, now = Path(root), _all(root), _now().isoformat(timespec="seconds")
    was = known.get(name) or {}
    known[name] = {"name": name, "app": clean(app) or was.get("app", ""), "model": clean(model) or was.get("model", ""),
                   "hello": was.get("hello") or now, "seen": now}
    conversation.set_setting(root, KEY, known)
    if take:
        conversation.set_setting(root, "engine", setting_of(name))
        conversation.set_setting(root, "engine_sort", setting_of(name))
    return known[name]


def is_known(root, name):
    return clean(name) in _all(root)


def seen(root, name):
    """It asked for work or reported on a job: it is here. Written at most twice a minute."""
    name, known = clean(name), _all(root)
    entry = known.get(name)
    if not entry:
        return
    try:
        if _now() - datetime.fromisoformat(entry.get("seen", "")) < timedelta(seconds=30):
            return
    except (TypeError, ValueError):
        pass
    entry["seen"] = _now().isoformat(timespec="seconds")
    conversation.set_setting(Path(root), KEY, known)


def known(root):
    """Every AI that has said hello, the most recent first: name, where it runs, its model, and whether it is here."""
    out = []
    for entry in _all(root).values():
        try:
            here = _now() - datetime.fromisoformat(entry.get("seen", "")) < AWAY_AFTER
        except (TypeError, ValueError):
            here = False
        out.append({"name": entry["name"], "app": entry.get("app", ""), "model": entry.get("model", ""),
                    "seen": str(entry.get("seen", ""))[5:16].replace("T", " "), "online": here})
    return sorted(out, key=lambda d: d["seen"], reverse=True)


def forget(root, name):
    known = _all(root)
    if known.pop(clean(name), None) is not None:
        conversation.set_setting(Path(root), KEY, known)


# ---- one item handed to one AI ---------------------------------------------------------------------------
# The AI chosen in settings does everything by default. One item can be handed to another AI instead: by the owner
# (in the item), or by the AI working on it (`xinxiang handoff`). Then only that AI takes the item, the others skip it.

FOR = "ai:for"
HANDOFF = "handoff"   # waiting for whichever AI the message is sent to: nobody else takes it meanwhile
LOCAL = {"codex": "本机 GPT", "claude": "本机 Claude", "gemini": "本机 Gemini"}


def _for(root):
    return dict(conversation.setting(Path(root), FOR, None) or {})


def assigned(root, record_id):
    """Who this one item was handed to ("driver:<name>", "cindy", "codex" …, or HANDOFF); None = the usual one."""
    return _for(root).get(record_id)


def assignments(root):
    return _for(root)


def assign(root, record_id, value):
    """Hand one item to one AI; an empty value gives it back to the AI chosen in settings."""
    table = _for(root)
    if value:
        table[record_id] = value
    else:
        table.pop(record_id, None)
    conversation.set_setting(Path(root), FOR, table)


def owner_of(root, record_id):
    """The AI that does this item: the one it was handed to, else the one chosen in settings."""
    return assigned(root, record_id) or conversation.setting(Path(root), "engine", "codex")


def label(value):
    value = str(value or "")
    if value == HANDOFF:
        return "等另一个 AI 来接"
    if value == "cindy":
        return "Cindy"
    return name_in(value) or LOCAL.get(value) or value


def resolve(root, text):
    """What an AI or the owner typed as "give it to …" → a setting value. A name not seen yet is a new AI's."""
    raw = clean(text)
    low = raw.lower().replace(" ", "")
    if not raw:
        return HANDOFF
    for entry in _all(root):
        if entry.lower().replace(" ", "") == low:
            return setting_of(entry)
    if low == "cindy":
        return "cindy"
    for key, name in LOCAL.items():   # the built-in ones by their 心响 name ("本机 Claude"); a bare "Claude" is an outside AI
        if low == name.lower().replace(" ", "") or (key == "codex" and low == "codex"):
            return key
    return setting_of(raw)


def is_mine(value, driver, *, builtin=False):
    """Does the AI asking for work take an item whose owner is `value`?"""
    value = str(value or "")
    if builtin:
        return value in LOCAL
    return value == setting_of(clean(driver)) or (value == "cindy" and "cindy" in str(driver).lower())
