"""Cindy script-only entrypoint; stdout contains cindy-script/1 frames only.

The installed host accepts a complete frame with optional resultText. No host
broker capabilities are called: Feishu operations use the verified local CLI.
"""
import argparse
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import sys
from .cli import main as run_cli
from .config import load
from .models import MatrixError


def complete(text):
    print(json.dumps({"protocol": "cindy-script/1", "type": "complete", "resultText": text}, ensure_ascii=False), flush=True)


def main(argv=None):
    p = argparse.ArgumentParser(description="Cindy 定时事项与简报")
    p.add_argument("job", choices=["recurring", "morning", "evening"])
    p.add_argument("--config", type=Path, default=Path("config.yaml"))
    p.add_argument("--preview", action="store_true")
    args = p.parse_args(argv)
    try:
        config = load(args.config.resolve())
        if not config.scheduling_enabled:
            complete("定时运行总开关已关闭，本轮未执行。")
            return 0
        command = ["recurring"] if args.job == "recurring" else ["brief", "--" + args.job]
        command += ["--config", str(args.config.resolve())]
        if args.preview:
            command.append("--preview")
        import os
        # Fixed-time jobs (06:00 / 08:30 / 21:00) queue behind a running check instead of failing.
        previous = os.environ.get("AGENT_MATRIX_LOCK_WAIT")
        os.environ["AGENT_MATRIX_LOCK_WAIT"] = previous or "180"
        output, errors = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(output), redirect_stderr(errors):
                code = run_cli(command)
        finally:
            if previous is None:
                os.environ.pop("AGENT_MATRIX_LOCK_WAIT", None)
        if code:
            # CLI errors are already sanitized; never expose raw subprocess data.
            try:
                error = json.loads(errors.getvalue()).get("error", "执行失败")
            except ValueError:
                error = "执行失败；请查看本地运行记录。"
            print(error, file=sys.stderr)
            return 1
        result = json.loads(output.getvalue())
        if result.get("ok") is False:
            raise MatrixError("操作结果未确认，已停止。")
        if args.preview:
            message = "只读预览通过；未修改事项或发送消息。"
        elif args.job == "recurring":
            message = (f"周期事项检查完成：{result.get('enabled_count', 0)} 条启用规则，"
                       f"新增 {result.get('created_count', 0)} 项。")
        else:
            message = "早间简报已处理，送达回执已记录。" if args.job == "morning" else "明日计划已处理，送达回执已记录。"
        from . import activity
        activity.log(args.config.resolve().parent, "run", {"recurring": "周期事项", "morning": "早报", "evening": "明日计划"}[args.job], detail=message)
        complete(message)
        return 0
    except Exception as exc:
        # Fail the host run rather than marking an unknown delivery successful.
        message = str(exc) if isinstance(exc, MatrixError) else f"定时运行失败（{type(exc).__name__}）"
        from . import activity
        activity.log(args.config.resolve().parent, "error", f"定时任务 {args.job} 出错", detail=message, status="failed")
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
