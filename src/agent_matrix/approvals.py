"""开发模式: the background AI waits for the owner before a risky step (or before every step).

One JSON file per request under state/approvals/, so the worker (asks, polls) and the app server (lists,
decides) share it without a lock: each side only ever replaces a whole file."""
import json
import os
import re
import shlex
import uuid
from datetime import datetime
from pathlib import Path

from .timeutil import TZ

MODES = ("off", "risky", "every")
# Asked about in "risky" mode (Claude permission rules, prefix match per sub-command): only what reaches beyond this
# computer — pushing and merging, publishing, cloud, servers and databases. Local work (rm, git reset, kill…) runs
# without asking: the owner chose speed there.
# ponytail: prefix list, add a pattern here when a new outward command shows up.
RISKY = [f"Bash({c}:*)" for c in (
    "git push", "npm publish", "pnpm publish", "yarn publish",
    # gh: only what writes outside (PRs, issues, repos, releases, secrets, CI); gh auth / pr view / pr checks read
    "gh api", "gh pr create", "gh pr merge", "gh pr close", "gh pr edit", "gh pr comment", "gh pr review", "gh pr ready",
    "gh issue create", "gh issue close", "gh issue edit", "gh issue comment", "gh issue delete",
    "gh repo create", "gh repo delete", "gh repo edit", "gh repo rename", "gh repo archive", "gh repo fork",
    "gh release", "gh secret", "gh variable", "gh workflow", "gh run", "gh gist", "gh ssh-key", "gh gpg-key", "kubectl", "helm", "terraform", "aws", "gcloud", "docker",
    "ssh", "scp", "rsync", "mysql", "psql", "redis-cli", "mongosh")]
# MCP tools with these words are banned for the worker (engines.MCP_DENY); one the owner let through
# (worker_mcp_send) is asked in 开发模式
MCP_DENY_WORDS = ("send", "publish", "deploy", "merge", "delete", "schedule_message", "post_", "reply",
                  "create", "update", "move", "restore", "resolve", "comment", "duplicate", "spawn", "cancel", "upload")
# a sub-command (after optional VAR=x prefixes) that starts with one of the RISKY commands as whole words
_RISKY_START = re.compile(r"\s*(\w+=\S*\s+)*(%s)(\s|$)" % "|".join(re.escape(r[5:-3]) for r in RISKY))


def needs_owner(root, tool, tool_input):
    """Asked again when a request arrives, against the mode as it is NOW: a claude started under "every" keeps
    prompting for its whole turn, so switching to "risky"/"off" mid-item has to be honoured here."""
    current = mode(root)
    if current == "every":
        return True
    if current == "off":
        return False
    if str(tool or "").startswith("mcp__"):
        return any(w in tool for w in MCP_DENY_WORDS)
    if tool != "Bash":
        return False
    # ponytail: naive split on shell separators, quotes are not parsed; a risky word inside a quoted string asks once too many
    return any((hit := _RISKY_START.match(part)) and not _read_only(hit.group(2), part)
               for part in _parts(str((tool_input or {}).get("command") or "")))


# Words that start a command without being it: `for …; do rm x; done`, `if …; then kubectl delete …`, `xargs rm`.
_LEAD = re.compile(r"^\s*(?:(?:do|then|else|elif|if|while|until|time|exec|nohup|env|command|builtin|!|\{|\(|xargs(?:\s+-\S+)*)\s+)+")


def _parts(command, depth=0):
    """Every simple command in a shell line, the ones inside $(…), `…` and bash/sh -c "…" included, each without the
    keywords in front of it. A risky command hidden in any of them is seen."""
    out = []
    inner = re.findall(r"\$\(([^()]*(?:\([^()]*\)[^()]*)*)\)|`([^`]*)`", command)
    for piece in re.split(r"&&|\|\||[;|\n]", command):
        out.append(_LEAD.sub("", re.sub(r"^\s*\w+=\$\(", "", piece)))   # x=$(aws …: the command after "x=$("
        c = re.search(r"\b(?:ba|z)?sh\s+-c\s+(\"[^\"]*\"|'[^']*')", piece)
        if c and depth < 3:
            out += _parts(c.group(1)[1:-1], depth + 1)
    if depth < 3:
        for a, b in inner:
            out += _parts(a or b, depth + 1)
    return out


# ---- 只读放行: a risky command whose sub-command only reads runs without asking ----------------------------------
# ponytail: per-tool lists of read verbs; a verb missing here just asks once too often, add it when it shows up.
# Options that take a value, so the value is not mistaken for the sub-command.
_VALUED = {
    "aws": {"--region", "--profile", "--output", "--endpoint-url", "--query", "--color", "--ca-bundle",
            "--cli-read-timeout", "--cli-connect-timeout"},
    "kubectl": {"-n", "--namespace", "--context", "--kubeconfig", "--cluster", "--user", "-s", "--server", "--token"},
    "docker": {"-H", "--host", "--context", "-c", "--config", "--log-level", "-l"},
    "helm": {"-n", "--namespace", "--kube-context", "--kubeconfig", "--repository-config"},
    "gcloud": {"--project", "--account", "--configuration", "--format", "--verbosity", "--region", "--zone"},
    "terraform": {"-chdir"},
    "redis-cli": {"-h", "-p", "-a", "-n", "-u", "--user", "--pass", "-s"},
}
_READ = {
    "kubectl": ({"get", "describe", "logs", "top", "explain", "events", "version", "cluster-info", "api-resources",
                 "api-versions"},
                {"config": {"view", "get-contexts", "current-context", "get-clusters"}, "auth": {"can-i", "whoami"},
                 "rollout": {"status", "history"}}),
    "docker": ({"ps", "images", "inspect", "logs", "version", "info", "stats", "diff", "port", "top", "search", "history"},
               {**{g: {"ls", "list", "inspect", "history", "logs", "ps"} for g in
                   ("image", "container", "network", "volume", "context", "buildx", "manifest")},
                "compose": {"ps", "logs", "config", "ls", "images", "top", "version"}, "system": {"df", "info"}}),
    "helm": ({"list", "ls", "status", "get", "history", "show", "search", "template", "lint", "version", "env", "verify"},
             {"repo": {"list", "ls"}, "dependency": {"list", "ls"}, "dep": {"list", "ls"}, "plugin": {"list", "ls"}}),
    "terraform": ({"plan", "show", "validate", "output", "providers", "version", "graph"}, {"state": {"list", "show"}}),
}
_AWS_READ_OP = re.compile(r"(describe|get|list|batch-get|head|lookup|filter)-|(ls|tail|list|get-caller-identity)$")   # configure list too; configure get reads keys
_SQL_READ = re.compile(r"\s*(select|show|desc|describe|explain)\b", re.I)
_REDIS_READ = {"get", "mget", "hget", "hmget", "hgetall", "hkeys", "hvals", "hlen", "hexists", "keys", "scan", "sscan",
               "hscan", "zscan", "ttl", "pttl", "type", "exists", "strlen", "info", "dbsize", "lrange", "llen", "lindex",
               "smembers", "scard", "sismember", "zrange", "zrevrange", "zrangebyscore", "zcard", "zscore", "ping",
               "memory", "object", "xrange", "xlen", "getrange", "bitcount", "pfcount"}
_SECRET = re.compile(r"get-secret-value|--with-decryption|print-access-token|print-identity-token|secrets\s+versions\s+access"
                     r"|\bget\s+secrets?\b")


def _words(part, name):
    """The words after the command with options (and their values) taken out: ["codebuild", "batch-get-builds", …]."""
    try:
        words = shlex.split(part)
    except ValueError:
        return None
    while words and re.match(r"\w+=", words[0]):
        words = words[1:]
    out, i, valued = [], 1, _VALUED.get(name, set())
    while i < len(words):
        w = words[i]
        if w.startswith("-"):
            i += 2 if w in valued else 1
            continue
        out.append(w)
        i += 1
    return out


def _read_only(name, part):
    """Does this risky command only read? Reading secrets, and anything unclear, still asks."""
    if _SECRET.search(part):
        return False
    if name in ("mysql", "psql"):
        if re.search(r"(?:^|\s)(?:-f|--file)\b|<", part):
            return False   # runs a file or stdin: what it does is not on the command line
        # a closed quote only: the outer split cuts at ";" inside quotes, and an unclosed half must not pass as read
        q = re.search(r"(?:^|\s)(?:-e|--execute|-c|--command)(?:\s+|=)(\"[^\"]*\"|'[^']*'|[^\s\"']\S*)", part)
        statements = [s for s in q.group(1).strip("\"'").split(";") if s.strip()] if q else []
        return bool(statements) and all(_SQL_READ.match(s) for s in statements)
    words = _words(part, name)
    if not words:
        return False
    if name == "aws":
        return len(words) >= 2 and bool(_AWS_READ_OP.match(words[1]))
    if name == "gcloud":
        return any(w in ("list", "describe", "info", "version") for w in words)
    if name == "redis-cli":
        return words[0].lower() in _REDIS_READ
    if name == "terraform" and words[0] == "fmt":
        return "-check" in part.split()
    if name in _READ:
        verbs, groups = _READ[name]
        return words[0] in verbs or (len(words) > 1 and words[1] in groups.get(words[0], ()))
    return False


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


STATUS = {"pending": "等你批准", "allowed": "已批准", "denied": "已拒绝", "expired": "已过期"}
# How long a run waits for the owner (the worker's own timeout is paused meanwhile): then the step is refused.
# ponytail: one fixed limit; the waiting run holds a worker slot, so make it a setting only if 2 h proves wrong.
OWNER_WAIT_HOURS = 2
_COMMAND = re.compile(r"\s*(批准|拒绝)\s*([0-9a-f]{12})\s*(.*)", re.S)


def command(root, text):
    """「批准 <id>」/「拒绝 <id> 理由」— a Feishu card button, a typed Feishu reply or a Slack button.
    None when the text is not one of these."""
    m = _COMMAND.fullmatch(text or "")
    if not m:
        return None
    entry = get(root, m[2])
    if not entry:
        return f"找不到这条待确认：{m[2]}"
    allow = m[1] == "批准"
    if not decide(root, m[2], allow, m[3].strip()):
        return f"这条已经处理过了（{STATUS.get(entry['status'], entry['status'])}）"
    return ("✓ 已批准：" if allow else "✓ 已拒绝：") + entry["what"][:80]


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
