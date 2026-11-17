import argparse
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from pydantic import ValidationError
from . import bitable
from .alerts import alert_key, clear_recovered
from .delivery import message_id, send_once
from .config import load
from .feishu import Feishu, FeishuError
from .models import MatrixError
from .state import State, exclusive_lock

LATER = {"followup": 2, "prep": 3, "review": 3, "prework": 4}


def ping_text(config):
    return ("【会议与待办中心｜第0阶段测试】\n"
            "四张基础表已建立。周期规则示例全部关闭，定时运行与 AI 处理尚未启用。\n"
            "请先验收表格，确认后再进入下一阶段。\n" + config.base_url)


def health(config, client, state, run_id):
    status = client.auth()
    checks = {"authorization": status, "setup_complete": state.get("setup_complete", False),
              "last_run": state.last_run(run_id), "scheduling_enabled": config.scheduling_enabled,
              "llm_enabled": config.llm_backend != "disabled", "meeting_watch_enabled": config.meeting_watch_enabled}
    client.verify_owner(status)
    bitable.require_owned(config, state)
    client.call("+base-get", "--base-token", config.base_token)
    existing = bitable.items(client.call("+table-list", "--base-token", config.base_token))
    ids = {t.get("table_id", t.get("id")) for t in existing}
    checks["tables"] = {name: token in ids for name, token in config.tables.items()}
    checks["unconfirmed_operations"] = state.pending()
    last = checks["last_run"]
    checks["ok"] = (not checks["unconfirmed_operations"] and checks["setup_complete"] and len(checks["tables"]) == 4
                    and all(checks["tables"].values()) and (not last or last["status"] == "ok"))
    return checks


def main(argv=None):
    parser = argparse.ArgumentParser(description="会议与待办矩阵")
    parser.add_argument("command", choices=["setup", "ping", "health", "recurring", "brief", *LATER])
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--dry-run", action="store_true", help="离线预览；不调用飞书、不写配置/日志/状态")
    parser.add_argument("--preview", action="store_true", help="读取真实表格和日历预览；不改飞书数据、不发送消息")
    editions = parser.add_mutually_exclusive_group()
    editions.add_argument("--evening", action="store_true")
    editions.add_argument("--morning", action="store_true")
    args = parser.parse_args(argv)
    if (args.evening or args.morning) and args.command != "brief":
        parser.error("--evening/--morning 仅用于 brief")
    if args.preview and (args.command not in {"recurring", "brief"} or args.dry_run):
        parser.error("--preview 仅用于 recurring/brief，且不能与 --dry-run 合用")
    state = None
    run_id = None
    client = None
    locks = ExitStack()
    try:
        path = args.config.resolve()
        root = path.parent
        config = load(path)
        if args.command in LATER:
            raise MatrixError(f"{args.command} 属于第 {LATER[args.command]} 阶段，尚未实现或启用；请先验收第 0 阶段。")
        if args.dry_run:
            if args.command == "setup":
                result = bitable.setup_preview(config)
            elif args.command == "ping":
                result = {"dry_run": True, "identity": "bot", "recipient": config.owner_open_id or "待核验的本人 open_id",
                          "text": ping_text(config), "send": False}
            elif args.command in {"recurring", "brief"}:
                result = {"dry_run": True, "executed": False, "command": args.command,
                          "edition": "evening" if args.evening else "morning" if args.command == "brief" else None,
                          "sources": ["周期规则表", "事项表"] if args.command == "recurring" else ["事项表", "本人日历"],
                          "note": "离线模式不读取真实资料；用 --preview 生成真实只读预览。"}
            else:
                result = {"dry_run": True, "checks": ["user/bot授权", "本人及应用绑定", "本系统Base可访问", "四张表存在", "最近运行状态"], "executed": False}
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        locks.enter_context(exclusive_lock(root / "state"))
        state = State(root / "state/state.db")
        run_id = state.begin(args.command)
        client = Feishu(config, root)
        if args.command == "setup":
            result = bitable.setup(config, path, client, state)
        elif args.command == "health":
            result = health(config, client, state, run_id)
        elif args.command in {"recurring", "brief"}:
            client.verify_owner(client.auth())
            bitable.require_owned(config, state)
            if not state.get("setup_complete"):
                raise MatrixError("基础表尚未完整建立，不能运行事项和简报。")
            if args.command == "recurring":
                from .agents.recurring import run
                result = run(config, client, state, preview=args.preview)
            elif state.get("brief-v1:migration-stage") == "v1_active":
                from . import brief_v1
                result = brief_v1.run(config, client, state, edition="evening" if args.evening else "morning", preview=args.preview)
                result = {k: v for k, v in result.items() if k not in {"rows", "events"}}
            else:
                from .agents.daily_brief import run
                result = run(config, client, state, edition="evening" if args.evening else "morning", preview=args.preview)
        else:
            client.verify_owner(client.auth())
            bitable.require_owned(config, state)
            if not state.get("setup_complete"):
                raise MatrixError("四张表尚未完整建好，不发送完成测试消息。")
            key = "stage0-ping-" + hashlib.sha256(config.base_token.encode()).hexdigest()[:24]
            receipt = send_once(state, "ping", lambda: client.send_self(ping_text(config), key))
            result = {"ok": True, "receipt": receipt, "recipient": config.owner_open_id, "identity": "bot"}
        clear_recovered(state, client)
        state.finish(run_id, result.get("ok", True))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok", True) else 1
    except (MatrixError, ValidationError, OSError, sqlite3.Error) as exc:
        if state and run_id:
            state.finish(run_id, False, type(exc).__name__)
            if client:
                clear_recovered(state, client)
        # Never log transcript, raw exception data, environment, tokens or raw CLI output.
        message = str(exc) if isinstance(exc, MatrixError) else f"配置或本地文件异常（{type(exc).__name__}）"
        result = {"ok": False, "error": message}
        if isinstance(exc, FeishuError) and exc.authorization and state and config.owner_open_id and not args.preview:
            try:
                key = alert_key(state, exc, args.command)
                receipt = send_once(state, key, lambda: client.send_self(
                    "会议与待办中心：当前操作遇到飞书授权或权限问题，已停止；请核对账号授权和对应资源权限。同一故障仅提醒一次。", key))
                result["owner_alert"] = "已有有效消息回执"
                result["owner_alert_message_id"] = message_id(receipt)
            except MatrixError:
                result["owner_alert"] = "通知未确认送达；请查看运行记录，结果不明时不会自动重发"
        print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    finally:
        if state:
            state.close()
        locks.close()


if __name__ == "__main__":
    raise SystemExit(main())
