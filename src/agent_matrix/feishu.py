import json
import hashlib
import os
import re
from pathlib import Path
import subprocess
import time
from . import activity
from .models import Config, MatrixError
from .delivery import require_message_receipt


READS = {"+base-get", "+table-list", "+table-get", "+field-list", "+view-list", "+view-get-filter"}
WRITES = {"+base-create", "+table-create", "+field-create", "+view-create", "+view-set-filter", "+record-batch-create", "+record-batch-update"}


def resolve_lark(binary):
    """On Windows the npm shim is lark-cli.cmd; run the native lark-cli.exe it wraps instead,
    so JSON arguments never pass through cmd.exe quoting. Elsewhere the name is used as is."""
    if os.name != "nt":
        return binary
    import shutil
    found = shutil.which(binary)
    if not found:
        return binary
    if found.lower().endswith((".cmd", ".bat", ".ps1")):
        exe = os.path.join(os.path.dirname(found), "node_modules", "@larksuite", "cli", "bin", "lark-cli.exe")
        if os.path.isfile(exe):
            return exe
    return found


def permission_scope(args):
    # Only stable operation/resource IDs, never message text, dates or secrets.
    flags = ("--as", "--base-token", "--table-id", "--calendar-id", "--user-id", "--chat-id", "--meeting-id", "--task-guid")
    resources = {flag: args[args.index(flag) + 1] for flag in flags
                 if flag in args and args.index(flag) + 1 < len(args)}
    digest = hashlib.sha256(json.dumps(resources, sort_keys=True).encode()).hexdigest()[:24]
    return ":".join(args[:2]) + ":" + digest


class FeishuError(MatrixError):
    def __init__(self, code="unknown", authorization=False, *, scope=None, detail=""):
        self.authorization = authorization
        self.code = str(code)
        self.not_sent = False
        self.scope = scope
        # What the command itself said stays on the error (it may quote content); the message only carries what is
        # safe to show: which permissions are missing, or which local proxy is down.
        self.detail = " ".join(str(detail or "").split())[:400]
        proxy, scopes = proxy_down(self.detail), missing_scopes(self)
        hint = f"（代理 {proxy} 没开）" if proxy else f"（少了权限：{'、'.join(scopes)}）" if self.code == "missing_scope" and scopes else ""
        super().__init__(f"飞书调用失败（{self.code}）{hint}；已停止，不自动重放写操作。")


SCOPE = re.compile(r"\b[a-z][a-z_]*(?::[a-z_.]+){1,3}\b")


def missing_scopes(exc):
    """The permissions a missing_scope error names (e.g. vc:meeting:readonly), as far as the command said them."""
    text = f"{getattr(exc, 'detail', '')}"
    return sorted({s for s in SCOPE.findall(text) if not s.startswith(("http", "https"))})[:8]


def proxy_down(text):
    """This computer sends traffic through a local proxy (127.0.0.1:7890 and the like) that is not running."""
    m = re.search(r"proxyconnect tcp: dial tcp ((?:127\.0\.0\.1|localhost):\d+)", str(text or ""))
    return m.group(1) if m else None


def why_not(exc):
    """Why Feishu cannot be used right now, in words the owner can act on."""
    code = getattr(exc, "code", None)
    if code is None:
        return str(exc)[:160]   # already a sentence: bot not available, another app, another account
    proxy = proxy_down(getattr(exc, "detail", ""))
    if proxy:
        return f"这台电脑设了网络代理（{proxy}），但代理软件现在没开，所以连不上飞书"
    if str(code) == "missing_scope":
        scopes = missing_scopes(exc)
        return "飞书应用少了权限" + (f"：{'、'.join(scopes)}" if scopes else "")
    return {"user_needs_refresh": "飞书登录过期了，要重新登录一次",
            "cli_unavailable": "找不到飞书命令行（lark-cli）",
            "timeout": "飞书命令行太久没有响应",
            "network": "连不上飞书（网络不通）",
            "invalid_json": "飞书命令行返回的内容读不懂（可能版本太旧或没登录）"}.get(str(code), f"飞书命令行报错（{code}）")


def note_health(state, exc=None):
    """Remember whether the sign-in check passed: the page says "connected" only while Feishu can really be read."""
    from datetime import datetime
    from .timeutil import TZ
    now = datetime.now(TZ)
    if exc is None:
        if (state.get("feishu:health") or {}).get("ok") is not True:
            state.set("feishu:health", {"ok": True, "at": now.strftime("%m-%d %H:%M")})
        return
    was = state.get("feishu:health") or {}
    state.set("feishu:health", {"ok": False, "why": why_not(exc), "at": now.strftime("%m-%d %H:%M"),
                                "since": was.get("since") if was.get("ok") is False else now.strftime("%m-%d %H:%M"),
                                "count": (was.get("count", 0) if was.get("ok") is False else 0) + 1})


AUTH_KEEP_SECONDS = 45
_AUTH = {}   # (command, project) → (when, answer): the last good sign-in check


class Feishu:
    def __init__(self, config: Config, root: Path, runner=None):
        self.config, self.root = config, root
        self.runner = runner or subprocess.run
        self.can_message_self = False
        self.authorized_scopes = set()
        self.use_local_items = True  # the backup sets False to write the Feishu table itself

    def local_items(self, args):
        from . import itemstore
        return self.use_local_items and itemstore.enabled(self.config) and itemstore.is_items_table(self.config, args)

    def feishu_free(self):
        from . import itemstore
        return itemstore.feishu_free(self.config)

    def _exec(self, args, *, auth=False, manifest=False):
        from . import itemstore
        if not itemstore.reads_feishu(self.config):
            raise MatrixError("没接飞书：事项记在 Notion 或本机，这一步要用飞书，已跳过。")
        scope = permission_scope(args)
        # A later failure must supersede any earlier success in this same run.
        self.authorized_scopes.discard(scope)
        env = dict(os.environ, LARKSUITE_CLI_NO_UPDATE_NOTIFIER="1", LARKSUITE_CLI_NO_SKILLS_NOTIFIER="1")
        try:
            r = self.runner([resolve_lark(self.config.lark_binary), *args], cwd=self.root, env=env,
                            capture_output=True, text=True, timeout=self.config.timeout_seconds, check=False)
        except subprocess.TimeoutExpired as exc:
            raise FeishuError("timeout") from exc
        except OSError as exc:
            raise FeishuError("cli_unavailable") from exc
        try:
            d = json.loads(r.stdout if r.returncode == 0 else r.stderr or r.stdout)
        except (ValueError, TypeError) as exc:
            said = (r.stderr or "") + " " + (r.stdout or "")
            raise FeishuError("network" if proxy_down(said) else "invalid_json", detail=said[:400]) from exc
        if not isinstance(d, dict):
            raise FeishuError("invalid_envelope")
        if r.returncode != 0 or d.get("ok") is False:
            e = d.get("error", {}) if isinstance(d.get("error"), dict) else {}
            code = "high-risk-write-refused" if r.returncode == 10 else e.get("code", e.get("subtype", r.returncode))
            said = " ".join(str(e.get(k) or "") for k in ("message", "hint", "detail", "permission_violations", "required_scopes", "missing_scopes"))
            raise FeishuError(code, e.get("type") in {"authorization", "authentication"}, scope=scope, detail=said)
        if manifest:
            if d.get("manifest_version") != "v1" or d.get("format") != "ndjson":
                raise FeishuError("invalid_manifest")
            if "--dry-run" not in args:
                self.authorized_scopes.add(scope)
            return d
        if not auth and d.get("ok") is not True:
            raise FeishuError("missing_ok")
        if not auth and "--dry-run" not in args:
            self.authorized_scopes.add(scope)
        return d if auth else d.get("data", {})

    def auth(self, _retry=True):
        if self.feishu_free():  # nothing to verify: no Feishu account is used
            ok = {"status": "ready", "available": True, "verified": True}
            return {"app_id": self.config.app_id, "user": dict(ok), "bot": dict(ok)}
        # Every timer checks who is signed in before it works; several fire in the same second. Ask the command
        # once and let the others within the next moments use that answer (only a good answer is kept).
        key = (resolve_lark(self.config.lark_binary), str(self.root))
        kept = _AUTH.get(key)
        if self.runner is subprocess.run and kept and time.monotonic() - kept[0] < AUTH_KEEP_SECONDS:
            return json.loads(kept[1])
        raw = self._exec(["auth", "status", "--json", "--verify"], auth=True)
        raw = raw.get("data", raw)
        user = raw.get("identities", {}).get("user", {})
        if _retry and user.get("status") == "needs_refresh":
            # The access token was at its refresh boundary; one re-check lets the CLI refresh it.
            return self.auth(_retry=False)
        ids = raw.get("identities", {})
        result = {"app_id": raw.get("appId"), "user": {}, "bot": {}}
        for kind in ("user", "bot"):
            v = ids.get(kind, {})
            result[kind] = {k: v.get(k) for k in ("status", "available", "verified", "openId", "userName", "tokenStatus")}
        if self.runner is subprocess.run and all(result[k].get("verified") and result[k].get("status") == "ready" for k in ("user", "bot")):
            _AUTH[key] = (time.monotonic(), json.dumps(result))
        else:
            _AUTH.pop(key, None)
        return result

    def verify_owner(self, status, *, require_user=True):
        self.authorized_scopes.discard("identity:user")
        user, bot = status["user"], status["bot"]
        if not bot.get("verified") or not bot.get("available"):
            raise MatrixError("飞书机器人授权不可用。")
        if self.config.app_id and self.config.app_id != status.get("app_id"):
            raise MatrixError("飞书应用与已绑定应用不同，已阻止操作。")
        if self.config.owner_open_id and user.get("openId") and self.config.owner_open_id != user.get("openId"):
            raise MatrixError("飞书登录账号与事项中心所有者不同，已阻止操作。")
        self.can_message_self = True
        if require_user and (not user.get("verified") or user.get("status") != "ready"):
            raise FeishuError("user_needs_refresh", authorization=True, scope="identity:user")
        if user.get("verified") and user.get("status") == "ready":
            self.authorized_scopes.add("identity:user")
            self.authorized_scopes.add(permission_scope(["auth", "status"]))

    def call(self, command, *args, write=False):
        allowed = WRITES if write else READS
        if command not in allowed or "--yes" in args:
            raise MatrixError("命令不在当前框架允许范围内。")
        if command in {"+record-batch-update", "+record-batch-create"} and self.local_items(args):
            from . import itemstore, records
            records.ensure_local(self)  # first use: bring the Feishu table home before writing locally
            return itemstore.apply_call(self, command, args)  # 心响 local store; Feishu gets it via backup
        argv = ["base", command, *args, "--as", "user", "--format", "json"]
        if write:
            try:
                self._exec([*argv, "--dry-run"])
            except FeishuError as exc:
                exc.not_sent = True
                raise
        return self._exec(argv)

    def send_card(self, card, key, channel=None):
        """Send an interactive card (display + open-url buttons only) to the owner."""
        if not self.can_message_self:
            raise MatrixError("尚未核验当前机器人与账号绑定，不发送消息。")
        if not self.config.owner_open_id.startswith("ou_") and (channel or self.config.notify_channel) != "local":
            raise MatrixError("尚未绑定本人 open_id，不发送消息。")
        argv = ["im", "+messages-send", "--user-id", self.config.owner_open_id, "--msg-type", "interactive",
                "--content", json.dumps(card, ensure_ascii=False), "--idempotency-key", key,
                "--as", "bot", "--format", "json"]
        return self._send_logged(argv, card, key, channel)

    def _send_logged(self, argv, content, key, channel=None):
        """Every message to the owner goes through here, so the local monitor sees all of them."""
        from . import activity
        title = activity.preview(content)
        from . import inbox
        if (channel or getattr(self.config, "notify_channel", "feishu")) == "local":
            # Nothing goes to Feishu: the message lands in the local client's inbox.
            receipt = inbox.add(self.root, key, content)
            activity.log(self.root, "send", receipt["title"] or title, status="ok", ref=receipt["message_id"])
            return receipt
        try:  # Feishu messages (the 08:30 / 21:00 briefs) keep a copy in the local inbox too
            inbox.add(self.root, "feishu:" + key, content, kind="brief" if str(key).startswith("brief") else None)
        except Exception as exc:
            activity.swallowed(self.root, "feishu._send_logged", exc)
        try:
            try:
                self._exec([*argv, "--dry-run"])
            except FeishuError as exc:
                exc.not_sent = True
                raise
            receipt = require_message_receipt(self._exec(argv))
        except Exception as exc:
            activity.log(self.root, "send", title, detail=str(exc), status="failed", ref=key)
            raise
        from .delivery import message_id
        activity.log(self.root, "send", title, detail=str(content)[:400] if not isinstance(content, dict) else "",
                     status="ok", ref=message_id(receipt) or key)
        if isinstance(content, dict) and message_id(receipt):
            from .cards import remember
            remember(self.root, message_id(receipt), content)
        return receipt

    def send_self(self, text, key, channel=None):
        if not self.can_message_self:
            raise MatrixError("尚未核验当前机器人与账号绑定，不发送消息。")
        if not self.config.owner_open_id.startswith("ou_") and (channel or self.config.notify_channel) != "local":
            raise MatrixError("尚未绑定本人 open_id，不发送消息。")
        # No recipient argument is exposed to agents or CLI callers.
        # Text links like [打开](url) only render in a post message; plain text stays text.
        kind = "--markdown" if re.search(r"\]\(https?://", text) else "--text"
        argv = ["im", "+messages-send", "--user-id", self.config.owner_open_id,
                kind, text, "--idempotency-key", key, "--as", "bot", "--format", "json"]
        return self._send_logged(argv, text, key, channel)
