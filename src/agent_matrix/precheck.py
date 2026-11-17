"""Host-neutral pre-run hook for any scheduler (works the same on macOS, Windows and Linux).

    python -m agent_matrix.precheck        # every few minutes from any AI host / scheduler

1. Handles the owner's Feishu replies and same-day brief catch-up (pure code).
2. Makes sure the local AI employee (python -m agent_matrix.worker) is running when there is work.
Meetings, chats and AI items are all done by that employee, so this never asks the host to wake a
model: it always exits 2 ("nothing for your model"). Exit 1 only when the employee cannot start.
"""
import json
from pathlib import Path
import subprocess
import sys


def _run(module, command, root):
    return subprocess.run([sys.executable, "-m", module, command], cwd=root, capture_output=True,
                          text=True, encoding="utf-8", timeout=120)


def main(argv=None):
    root = Path.cwd()
    _run("agent_matrix.reply", "poll", root)  # replies + catch-up; a failure here never blocks the employee
    kick = _run("agent_matrix.worker", "kick", root)
    try:
        result = json.loads(kick.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        print(json.dumps({"ok": False, "error": "AI 员工没能启动", "detail": (kick.stderr or "")[-200:]}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, "run_model": False, **result}, ensure_ascii=False))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
