"""Cards update in place: a tapped「✓ 通过」turns into "✓ 已通过 14:52" on the card itself.

Every card sent to the owner is kept locally by message id (state/cards/). When a button is tapped,
the card callback carries a one-time token; we swap that button for a short result line and send
the whole card back through the delayed-update API. If anything is missing, the caller falls back to
a chat message, so the owner always sees what happened.
"""
import json
from pathlib import Path
import re

KEEP = 300  # cards kept on disk


def _path(root, message_id):
    if not re.fullmatch(r"om_[A-Za-z0-9_-]{4,80}", message_id or ""):
        return None
    return Path(root) / "state/cards" / f"{message_id}.json"


def remember(root, message_id, card):
    path = _path(root, message_id)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")
        old = sorted(path.parent.glob("om_*.json"), key=lambda x: x.stat().st_mtime)[:-KEEP]
        for x in old:
            x.unlink(missing_ok=True)
    except OSError:
        pass


def load(root, message_id, card_content=""):
    path = _path(root, message_id)
    if path is not None and path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    try:
        card = json.loads(card_content or "")
        return card if isinstance(card, dict) and isinstance(card.get("body"), dict) else None
    except ValueError:
        return None


def _is_target(element, cmd):
    if not isinstance(element, dict) or element.get("tag") != "button":
        return False
    for b in element.get("behaviors") or []:
        if b.get("type") == "callback" and (b.get("value") or {}).get("cmd") == cmd:
            return True
    return (element.get("value") or {}).get("cmd") == cmd


def mark(card, cmd, text, ok=True):
    """Return (new card, replaced?) with the tapped button swapped for a result line."""
    color = "green" if ok else "red"
    done = {"tag": "markdown", "content": f"<font color='{color}'>{text}</font>", "text_size": "notation"}
    replaced = False

    def walk(node):
        nonlocal replaced
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            out = []
            for x in node:
                if _is_target(x, cmd):
                    out.append(done)
                    replaced = True
                else:
                    out.append(walk(x))
            return out
        return node

    return walk(card), replaced


def short_result(reply, now):
    """「#15 周报 → 完成 ✓」→「✓ 已完成 14:52」; anything else → the first line, shortened."""
    first = (reply or "").strip().splitlines()[0] if (reply or "").strip() else ""
    if "✓" in first:
        verb = "已批准" if "已批准" in first else "已拒绝" if "已拒绝" in first else "已完成" if "完成" in first else "收到，按钮可用" if "按钮可用" in first else "已处理"
        return f"✓ {verb} {now:%H:%M}", True
    return (first.split("→")[-1].strip() or "没改成")[:20], False


def update_in_place(client, root, event, cmd, reply, now):
    """True when the card on the owner's screen now shows the result."""
    token = event.get("token")
    card = load(root, event.get("message_id"), event.get("card_content"))
    if not token or not card:
        return False
    text, ok = short_result(reply, now)
    new, replaced = mark(card, cmd, text, ok)
    if not replaced:
        return False
    try:
        client._exec(["api", "POST", "/open-apis/interactive/v1/card/update", "--as", "bot",
                      "--data", json.dumps({"token": token, "card": new}, ensure_ascii=False)])
    except Exception:
        return False
    remember(root, event.get("message_id"), new)
    return True
