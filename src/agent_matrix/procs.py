"""Is a process still running? Asked without touching it, on every system.

On Windows `os.kill(pid, 0)` is not a harmless probe: it calls TerminateProcess and ends the process it asks about
(the panel asking whether the AI worker was alive killed the worker). So Windows asks the system instead.
"""
import os


def alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 1:
        return False
    if os.name == "nt":
        return _alive_windows(pid)
    try:
        os.kill(pid, 0)   # signal 0: only checks, sends nothing
        return True
    except PermissionError:
        return True       # it exists, it is just someone else's
    except OSError:
        return False


def _alive_windows(pid):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION: may look, may not touch
    if not handle:
        return ctypes.get_last_error() == 5           # access denied: it exists
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259                       # STILL_ACTIVE
    finally:
        kernel.CloseHandle(handle)
