"""Whichever AI the owner hands 心响 to, drives it.

Nothing is chosen in advance. The owner pastes one message to an AI (Antigravity, Gemini, Claude Code, Cursor,
Cindy … any that can run a command). That AI says who it is (`xinxiang hello`), which makes it the one that does
the work, and then comes by on its own schedule (a pre-run `gate` decides whether to wake it) or is woken by 心响 through
the address it gave (`wake-hook`). 心响 learns the list of AIs on this computer from who came and said hello; one that
does not come is woken, and when that fails the built-in driver stands in for it (wakeup.py).
Sending the message to another AI hands the work over to that one.
"""
import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta
import re
from pathlib import Path

from . import thread as conversation
from .timeutil import TZ

KEY = "ai:drivers"
AWAY_AFTER = timedelta(minutes=12)   # an AI on a 5-minute schedule calls `gate` (or `next`) at least that often


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
    """An AI says who it is. With `take` (the usual case) it becomes the one that does the items and the sorting;
    take="work" or "sort" hands it only that part; take=False: it only comes for what is handed to it one by one."""
    name = clean(name)
    if not name:
        raise ValueError("要说你叫什么：--name")
    root, known, now = Path(root), _all(root), _now().isoformat(timespec="seconds")
    was = known.get(name) or {}
    known[name] = {"name": name, "app": clean(app) or was.get("app", ""), "model": clean(model) or was.get("model", ""),
                   "hello": was.get("hello") or now, "seen": now}
    conversation.set_setting(root, KEY, known)
    if take in (True, "work"):
        conversation.set_setting(root, "engine", setting_of(name))
    if take in (True, "sort"):
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


STANDIN, HOOKS = "ai:standin", "ai:wake-hooks"


def _hooks(root):
    return dict(conversation.setting(Path(root), HOOKS, None) or {})


def new_secret():
    return "whsec_" + base64.b64encode(secrets.token_bytes(24)).decode()


def hook_of(root, value):
    return _hooks(root).get(value)


def set_hook(root, value, url, secret=None):
    """An AI (or the owner) tells 心响 how to wake it: an https address that starts one round of its schedule."""
    url = str(url or "").strip()
    if url and not url.startswith("https://"):
        raise ValueError("叫醒地址要以 https:// 开头。")
    hooks = _hooks(root)
    if not url:
        hooks.pop(value, None)
    else:
        was = hooks.get(value) or {}
        hooks[value] = {"url": url[:500], "secret": (secret or was.get("secret") or new_secret())[:200],
                        "set": _now().isoformat(timespec="seconds")}
    conversation.set_setting(Path(root), HOOKS, hooks)
    return hooks.get(value)


def signed(secret, msg_id, stamp, body):
    """Standard Webhooks: base64 HMAC-SHA256 of "<id>.<timestamp>.<body>" with the secret (whsec_ + base64 key)."""
    key = base64.b64decode(secret.split("_", 1)[1]) if secret.startswith("whsec_") else secret.encode()
    return "v1," + base64.b64encode(hmac.new(key, f"{msg_id}.{stamp}.{body}".encode(), hashlib.sha256).digest()).decode()


WAKE_SAID = {401: "叫醒密钥对不上（两边的签名密钥不一样）", 404: "叫醒地址失效了（那边的 Webhook 被删了或换了）",
             429: "叫醒次数今天用完了（那边的 Webhook 有每日上限）", 503: "那边暂时收不了叫醒（云端接收不可用）"}


def send_wake(hook, work, opener=urllib.request.urlopen):
    """One signed request that starts a round on the AI's side. Says nothing but the kind of work waiting."""
    if not hook or not hook.get("url"):
        return {"ok": False, "why": "它没给心响叫醒的入口，只能等它自己的定时任务来"}
    msg_id, stamp = "xinxiang-" + uuid.uuid4().hex, str(int(time.time()))
    body = json.dumps({"type": "xinxiang.work", "work": work}, ensure_ascii=False)
    request = urllib.request.Request(hook["url"], data=body.encode(), method="POST", headers={
        "content-type": "application/json", "webhook-id": msg_id, "webhook-timestamp": stamp,
        "webhook-signature": signed(hook.get("secret") or "", msg_id, stamp, body), "user-agent": "xinxiang"})
    try:
        with opener(request, timeout=15) as answer:
            code = answer.status
    except urllib.error.HTTPError as exc:
        code = exc.code
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"ok": False, "why": f"叫醒请求发不出去（{type(exc).__name__}：网络不通或地址不对）"}
    if 200 <= code < 300:
        return {"ok": True, "status": code}
    return {"ok": False, "status": code, "why": WAKE_SAID.get(code, f"叫醒被拒绝（HTTP {code}）")}



def pending_secret(root, value):
    """The signing secret to put on the AI's side before its address is known (kept until the address comes)."""
    hooks = _hooks(root)
    entry = hooks.setdefault(value, {})
    if not entry.get("secret"):
        entry["secret"] = new_secret()
        conversation.set_setting(Path(root), HOOKS, hooks)
    return entry["secret"]




def standing_in(root):
    """The AIs in other apps the built-in driver stands in for right now (wakeup.py decides): {value: {...}}."""
    return dict(conversation.setting(Path(root), STANDIN, None) or {})


def forget(root, name):
    known = _all(root)
    if known.pop(clean(name), None) is not None:
        conversation.set_setting(Path(root), KEY, known)


# ---- one item handed to one AI ---------------------------------------------------------------------------
# The AI chosen in settings does everything by default. One item can be handed to another AI instead: by the owner
# (in the item), or by the AI working on it (`xinxiang handoff`). Then only that AI takes the item, the others skip it.

FOR = "ai:for"
HANDOFF = "handoff"   # waiting for whichever AI the message is sent to: nobody else takes it meanwhile
LOCAL = {"codex": "本机 GPT", "claude": "本机 Claude", "gemini": "本机 Gemini", "api": "模型接口"}


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
