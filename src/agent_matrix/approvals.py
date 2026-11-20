"""开发模式: the background AI waits for the owner before a risky step (or before every step).

One JSON file per request under state/approvals/, so the worker (asks, polls) and the app server (lists,
decides) share it without a lock: each side only ever replaces a whole file."""
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from .timeutil import TZ

MODES = ("off", "risky", "every")
# Asked about in "risky" mode (Claude permission rules, prefix match per sub-command). Everything else runs as before.
# ponytail: prefix list, add a pattern here when a new dangerous command shows up.
RISKY = [f"Bash({c}:*)" for c in (
    "rm", "rmdir", "sudo", "chmod", "chown", "dd", "mkfs", "kill", "pkill", "killall", "launchctl", "crontab",
    "git push", "git reset", "git clean", "git rebase", "git branch -D", "git checkout --", "git restore", "git stash drop",
    "npm publish", "pnpm publish", "yarn publish", "gh", "kubectl", "helm", "terraform", "aws", "gcloud", "docker",
    "ssh", "scp", "rsync", "mysql", "psql", "redis-cli", "mongosh")]


def mode(root):
    from . import thread
    value = thread.setting(Path(root), "dev_mode", "off")
    return value if value in MODES else "off"


def _dir(root):
    path = Path(root) / "state/approvals"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write(root, entry):
    path = _dir(root) / f"{entry['id']}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def get(root, approval_id):
    if not approval_id or not all(c.isalnum() for c in approval_id):
        return None
    try:
        return json.loads((_dir(root) / f"{approval_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def summary(tool, tool_input):
    """What the owner reads before saying yes: the command itself for Bash, the path for file tools."""
    tool_input = tool_input or {}
    text = tool_input.get("command") or tool_input.get("file_path") or tool_input.get("url") or tool_input.get("pattern")
    return str(text if text else json.dumps(tool_input, ensure_ascii=False))[:1500]


def ask(root, record_id, tool, tool_input, reason=""):
    entry = {"id": uuid.uuid4().hex[:12], "record_id": record_id, "tool": tool, "what": summary(tool, tool_input),
             "why": str(reason or (tool_input or {}).get("description") or "")[:300],
             "status": "pending", "note": "", "at": datetime.now(TZ).isoformat(timespec="seconds")}
    _write(root, entry)
    return entry["id"]


def decide(root, approval_id, allow, note=""):
    entry = get(root, approval_id)
    if not entry or entry["status"] != "pending":
        return False
    entry.update(status="allowed" if allow else "denied", note=str(note or "")[:500],
                 decided_at=datetime.now(TZ).isoformat(timespec="seconds"))
    _write(root, entry)
    return True


def expire(root, approval_id):
    """The run ended (timeout, stopped) before an answer: the request is no longer waiting on anyone."""
    entry = get(root, approval_id)
    if entry and entry["status"] == "pending":
        entry["status"] = "expired"
        _write(root, entry)


def pending(root):
    out = []
    for path in sorted(_dir(root).glob("*.json")):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if entry.get("status") == "pending":
            out.append(entry)
    return sorted(out, key=lambda e: e["at"])
