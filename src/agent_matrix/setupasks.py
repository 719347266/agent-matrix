"""What the owner asked the AI that drives 心响 to set up: one click on 接入飞书 / Slack / 微信 (or 剩下没配好的).

The ask waits here until that AI takes it as a job (drive: kind "setup"), works through `xinxiang-setup doctor --only
<what>`, and hands it in; 心响 itself checks whether it is really connected before the ask goes away.
"""
from datetime import datetime
from pathlib import Path

from . import thread as conversation
from .timeutil import TZ

KEY = "setup:asks"
WHAT = {"feishu": "飞书", "slack": "Slack", "wechat": "微信", "fix": "剩下没配好的"}


def _all(root):
    return dict(conversation.setting(Path(root), KEY, None) or {})


def ask(root, what):
    if what not in WHAT:
        raise ValueError(what)
    asks = _all(root)
    asks.setdefault(what, {"at": datetime.now(TZ).isoformat(timespec="seconds")})
    conversation.set_setting(Path(root), KEY, asks)


def pending(root):
    """{what: {"at", "last"?}} in the order they were asked."""
    return dict(sorted(_all(root).items(), key=lambda kv: kv[1].get("at", "")))


def note(root, what, **fields):
    asks = _all(root)
    if what in asks:
        asks[what].update(fields)
        conversation.set_setting(Path(root), KEY, asks)


def done(root, what):
    asks = _all(root)
    if asks.pop(what, None) is not None:
        conversation.set_setting(Path(root), KEY, asks)
