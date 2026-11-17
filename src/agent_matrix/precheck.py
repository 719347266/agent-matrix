"""Host-neutral pre-check for the AI routine (works the same on macOS, Windows and Linux).

    python -m agent_matrix.precheck        # run every ~2 minutes from any AI host / scheduler

1. Handles the owner's Feishu replies and same-day brief catch-up (pure code).
2. Runs the meeting, prework and chat checks.
Prints one JSON line. Exit 0 = there is model work (wake your agent with prompts/routine.md),
exit 2 = nothing to do, exit 1 = a check failed (not "nothing to do").
"""
import json
from pathlib import Path
import subprocess
import sys

CHECKS = [("meetings", "agent_matrix.meeting_watch"), ("prework", "agent_matrix.prework"),
          ("chats", "agent_matrix.chat_review")]


def _run(module, command, root):
    return subprocess.run([sys.executable, "-m", module, command], cwd=root, capture_output=True,
                          text=True, encoding="utf-8", timeout=120)


def main(argv=None):
    root = Path.cwd()
    _run("agent_matrix.reply", "poll", root)  # replies + catch-up; a failure here never blocks the checks
    results = {}
    for name, module in CHECKS:
        run = _run(module, "check", root)
        try:
            result = json.loads(run.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            print(json.dumps({"ok": False, "error": f"{name} 检查没有返回结果"}, ensure_ascii=False))
            return 1
        if run.returncode not in (0, 2) or result.get("ok") is not True:
            print(json.dumps({"ok": False, "error": f"{name} 检查未成功", "detail": result}, ensure_ascii=False))
            return 1
        results[name] = result
    if any(r.get("reason") in {"busy", "backoff"} for r in results.values()):
        print(json.dumps({"ok": True, "run_model": False, "reason": "busy"}))
        return 2
    run_model = any(r.get("run_model") for r in results.values())
    print(json.dumps({"ok": True, "run_model": run_model, **results}, ensure_ascii=False))
    return 0 if run_model else 2


if __name__ == "__main__":
    raise SystemExit(main())
