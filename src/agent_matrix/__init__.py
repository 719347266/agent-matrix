"""心响 (agent-matrix)."""
import os
import subprocess

CREATE_NO_WINDOW = 0x08000000


def quiet_flags(kwargs, windows=None):
    """On Windows a console program (lark-cli, git, wechat-cli …) started from 心响, which itself has no console,
    opens a terminal window for a moment. Start every one of them without a window unless the caller chose its
    own creation flags (a detached updater, say)."""
    if (os.name == "nt" if windows is None else windows) and not kwargs.get("creationflags"):
        kwargs["creationflags"] = CREATE_NO_WINDOW
    return kwargs


def _no_console_windows():
    """One place instead of one flag per call: nothing started later can forget it."""
    if os.name != "nt" or getattr(subprocess.Popen, "_xinxiang_quiet", False):
        return
    start = subprocess.Popen.__init__

    def quiet(self, *args, **kwargs):
        start(self, *args, **quiet_flags(kwargs))
    subprocess.Popen.__init__ = quiet
    subprocess.Popen._xinxiang_quiet = True


_no_console_windows()
