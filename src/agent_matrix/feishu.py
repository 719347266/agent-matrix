import json
import hashlib
import os
import re
from pathlib import Path
import subprocess
from .models import Config, MatrixError
from .delivery import require_message_receipt


READS = {"+base-get", "+table-list", "+table-get", "+field-list", "+view-list", "+view-get-filter"}
WRITES = {"+base-create", "+table-create", "+field-create", "+view-create", "+view-set-filter", "+record-batch-create", "+record-batch-update"}


def permission_scope(args):
    # Only stable operation/resource IDs, never message text, dates or secrets.
    flags = ("--as", "--base-token", "--table-id", "--calendar-id", "--user-id", "--chat-id", "--meeting-id", "--task-guid")
    resources = {flag: args[args.index(flag) + 1] for flag in flags
                 if flag in args and args.index(flag) + 1 < len(args)}
    digest = hashlib.sha256(json.dumps(resources, sort_keys=True).encode()).hexdigest()[:24]
    return ":".join(args[:2]) + ":" + digest


class FeishuError(MatrixError):
    def __init__(self, code="unknown", authorization=False, *, scope=None):
        self.authorization = authorization
        self.code = str(code)
        self.not_sent = False
        self.scope = scope
        super().__init__(f"飞书调用失败（{self.code}）；已停止，不自动重放写操作。")


class Feishu:
    def __init__(self, config: Config, root: Path, runner=None):
        self.config, self.root = config, root
        self.runner = runner or subprocess.run
        self.can_message_self = False
        self.authorized_scopes = set()

    def _exec(self, args, *, auth=False, manifest=False):
        scope = permission_scope(args)
        # A later failure must supersede any earlier success in this same run.
        self.authorized_scopes.discard(scope)
        env = dict(os.environ, LARKSUITE_CLI_NO_UPDATE_NOTIFIER="1", LARKSUITE_CLI_NO_SKILLS_NOTIFIER="1")
        try:
            r = self.runner([self.config.lark_binary, *args], cwd=self.root, env=env,
                            capture_output=True, text=True, timeout=self.config.timeout_seconds, check=False)
        except subprocess.TimeoutExpired as exc:
            raise FeishuError("timeout") from exc
        except OSError as exc:
            raise FeishuError("cli_unavailable") from exc
        try:
            d = json.loads(r.stdout if r.returncode == 0 else r.stderr or r.stdout)
        except (ValueError, TypeError) as exc:
            raise FeishuError("invalid_json") from exc
        if not isinstance(d, dict):
            raise FeishuError("invalid_envelope")
        if r.returncode != 0 or d.get("ok") is False:
            e = d.get("error", {})
            code = "high-risk-write-refused" if r.returncode == 10 else e.get("code", e.get("subtype", r.returncode))
            raise FeishuError(code, e.get("type") in {"authorization", "authentication"}, scope=scope)
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
        argv = ["base", command, *args, "--as", "user", "--format", "json"]
        if write:
            try:
                self._exec([*argv, "--dry-run"])
            except FeishuError as exc:
                exc.not_sent = True
                raise
        return self._exec(argv)

    def send_card(self, card, key):
        """Send an interactive card (display + open-url buttons only) to the owner."""
        if not self.can_message_self:
            raise MatrixError("尚未核验当前机器人与账号绑定，不发送消息。")
        if not self.config.owner_open_id.startswith("ou_"):
            raise MatrixError("尚未绑定本人 open_id，不发送消息。")
        argv = ["im", "+messages-send", "--user-id", self.config.owner_open_id, "--msg-type", "interactive",
                "--content", json.dumps(card, ensure_ascii=False), "--idempotency-key", key,
                "--as", "bot", "--format", "json"]
        return self._send_logged(argv, card, key)

    def _send_logged(self, argv, content, key):
        """Every message to the owner goes through here, so the local monitor sees all of them."""
        from . import activity
        title = activity.preview(content)
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
        return receipt

    def send_self(self, text, key):
        if not self.can_message_self:
            raise MatrixError("尚未核验当前机器人与账号绑定，不发送消息。")
        if not self.config.owner_open_id.startswith("ou_"):
            raise MatrixError("尚未绑定本人 open_id，不发送消息。")
        # No recipient argument is exposed to agents or CLI callers.
        # Text links like [打开](url) only render in a post message; plain text stays text.
        kind = "--markdown" if re.search(r"\]\(https?://", text) else "--text"
        argv = ["im", "+messages-send", "--user-id", self.config.owner_open_id,
                kind, text, "--idempotency-key", key, "--as", "bot", "--format", "json"]
        return self._send_logged(argv, text, key)
