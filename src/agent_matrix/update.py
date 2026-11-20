"""One click to the newest version.

心响 looks at its own releases page now and then (a plain request, nothing about the owner is sent). When there
is a newer version the owner clicks once:

  the Windows client   downloads the new installer, runs it silently and starts 心响 again
  a copy from source   (git clone of the release branch, nothing changed locally) pulls, reinstalls, restarts

A copy that is being developed (another branch, local changes) is left alone: it says so instead.
"""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import urllib.request

from . import thread as conversation
from .timeutil import TZ

REPO = "MILESMAKMAK/agent-matrix"
RELEASES = f"https://api.github.com/repos/{REPO}/releases/latest"
CHECK_EVERY = timedelta(hours=6)
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_working = threading.Lock()


def current():
    repo = _source_root()
    if repo:   # a checkout says its version itself (what was installed from it may be older)
        found = re.search(r'^version = "(.+)"', (repo / "pyproject.toml").read_text(encoding="utf-8"), re.M)
        if found:
            return found.group(1)
    try:   # the newest one on record: an install over an older one may have left the old record behind
        from importlib.metadata import distributions
        found = [d.version for d in distributions() if (d.metadata["Name"] or "").lower().replace("_", "-") == "agent-matrix"]
        return max(found, key=_number) if found else ""
    except Exception:
        return ""


def _number(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v or ""))[:3]) or (0,)


def newer(latest, mine):
    return bool(latest) and _number(latest) > _number(mine)


def _source_root():
    """The checkout this runs from, when it is one."""
    here = Path(__file__).resolve().parents[2]
    return here if (here / ".git").exists() and (here / "pyproject.toml").is_file() else None


def _client_dir():
    """Where the Windows installer put 心响, when this is that copy."""
    app = Path(sys.executable).resolve().parent.parent
    return app if os.name == "nt" and (app / "xinxiang.ico").is_file() and (app / "python").is_dir() else None


def _git(repo, *args, timeout=60):
    run = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                         timeout=timeout, creationflags=NO_WINDOW)
    return run.returncode, (run.stdout or "").strip()


def how(root=None):
    """(kind, can it update itself, why not)."""
    if _client_dir():
        return "client", True, ""
    repo = _source_root()
    if not repo:
        return "other", False, "这份心响不是用安装包或 git 装的，请按当初的装法更新。"
    try:
        code, branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        dirty = _git(repo, "status", "--porcelain", "--untracked-files=no")[1]
        remote = _git(repo, "config", "--get", "remote.origin.url")[1]
    except (OSError, subprocess.SubprocessError):
        return "git", False, "这台电脑上找不到 git。"
    if code != 0 or branch != "main" or dirty or REPO.lower() not in remote.lower():
        return "git", False, "这份是开发中的版本（不在发布分支上，或有本地改动），不自动更新。"
    return "git", True, ""


def fetch_latest(timeout=15):
    request = urllib.request.Request(RELEASES, headers={"User-Agent": "xinxiang-updater", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    setup = next((a.get("browser_download_url") for a in data.get("assets") or []
                  if str(a.get("name", "")).startswith("Xinxiang-Setup") and str(a.get("name", "")).endswith(".exe")), "")
    return {"version": str(data.get("tag_name") or "").lstrip("v"), "page": data.get("html_url") or "", "setup": setup,
            "notes": str(data.get("body") or "")[:600]}


def check(root, force=False):
    """What the newest release is; asked at most every few hours unless forced. Never raises."""
    root = Path(root)
    seen = conversation.setting(root, "update:latest", None) or {}
    try:
        fresh = datetime.now(TZ) - datetime.fromisoformat(seen.get("at", "")) < CHECK_EVERY
    except (TypeError, ValueError):
        fresh = False
    if fresh and not force:
        return seen
    try:
        seen = {**fetch_latest(), "at": datetime.now(TZ).isoformat(timespec="seconds"), "error": ""}
    except Exception as exc:
        seen = {**seen, "at": datetime.now(TZ).isoformat(timespec="seconds"), "error": f"连不上发布页（{type(exc).__name__}）"}
    conversation.set_setting(root, "update:latest", seen)
    return seen


def check_soon(root):
    """From the page's own refresh: look in the background when the last look is old. Costs nothing when fresh."""
    seen = conversation.setting(Path(root), "update:latest", None) or {}
    try:
        if datetime.now(TZ) - datetime.fromisoformat(seen.get("at", "")) < CHECK_EVERY:
            return
    except (TypeError, ValueError):
        pass
    threading.Thread(target=check, args=(root,), daemon=True).start()


def status(root):
    """For the page: this version, the newest one, and whether one click can get it."""
    root = Path(root)
    mine, seen = current(), conversation.setting(root, "update:latest", None) or {}
    kind, can, why = how(root)
    doing = conversation.setting(root, "update:doing", None) or {}
    return {"current": mine, "latest": seen.get("version") or "", "newer": newer(seen.get("version"), mine), "kind": kind,
            "can": can and (kind != "client" or bool(seen.get("setup"))), "why": why, "page": seen.get("page") or "",
            "checked": str(seen.get("at") or "")[5:16].replace("T", " "), "error": seen.get("error") or "",
            "doing": doing.get("step") or "", "failed": doing.get("error") or ""}


def _say(root, step="", error=""):
    conversation.set_setting(Path(root), "update:doing", {"step": step, "error": error, "at": datetime.now(TZ).isoformat(timespec="seconds")})


def _update_client(root, seen):
    app = _client_dir()
    target = Path(tempfile.gettempdir()) / f"Xinxiang-Setup-{seen['version']}.exe"
    _say(root, f"正在下载 {seen['version']}…")
    request = urllib.request.Request(seen["setup"], headers={"User-Agent": "xinxiang-updater"})
    with urllib.request.urlopen(request, timeout=60) as r, open(target, "wb") as out:
        while chunk := r.read(1 << 16):
            out.write(chunk)
    if target.stat().st_size < 1_000_000:
        raise OSError("下载下来的安装包不完整")
    _say(root, "正在安装，心响会自己重新打开…")
    # the installer stops this very program, so what runs it and starts 心响 again must live outside of it
    again = f'start "" "{app}\\python\\pythonw.exe" -m agent_matrix.winclient --no-window'
    subprocess.Popen(f'cmd /c ""{target}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- & {again}"', cwd=tempfile.gettempdir(),
                     creationflags=0x00000008 | 0x00000200 | NO_WINDOW, close_fds=True)   # detached, its own group


def _update_source(root):
    repo = _source_root()
    _say(root, "正在取新版本…")
    code, out = _git(repo, "pull", "--ff-only", "origin", "main", timeout=180)
    if code != 0:
        raise OSError("取新版本没成功：" + out[-200:])
    _say(root, "正在安装…")
    run = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-e", str(repo)],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600, creationflags=NO_WINDOW)
    if run.returncode != 0:
        raise OSError("安装没成功：" + (run.stderr or run.stdout or "")[-200:])
    _say(root, "装好了，后台程序正在重启…")
    if sys.platform == "darwin":
        subprocess.Popen(["/bin/sh", "-c", f"sleep 2; launchctl kickstart -k gui/{os.getuid()}/com.agent-matrix.listen"],
                         start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif os.name == "nt":
        subprocess.Popen('cmd /c "timeout /t 2 /nobreak >nul & schtasks /End /TN "AgentMatrix Listener" & schtasks /Run /TN "AgentMatrix Listener""',
                         creationflags=0x00000008 | 0x00000200 | NO_WINDOW, close_fds=True)


def start(root):
    """Begin updating in the background; the page follows `status` and reloads itself when the new one is up."""
    root = Path(root)
    kind, can, why = how(root)
    seen = check(root, force=True)
    if not can:
        return {"ok": False, "reply": why or "这份心响不能自己更新。"}
    if not newer(seen.get("version"), current()):
        return {"ok": False, "reply": "已经是最新版本。"}
    if kind == "client" and not seen.get("setup"):
        return {"ok": False, "reply": "这个版本还没有放出安装包，过一会儿再试。"}
    if not _working.acquire(blocking=False):
        return {"ok": True, "reply": "正在更新…"}

    def work():
        try:
            _update_client(root, seen) if kind == "client" else _update_source(root)
        except Exception as exc:
            _say(root, "", f"更新没成功：{str(exc)[:200] or type(exc).__name__}")
        finally:
            _working.release()
    _say(root, "开始更新…")
    threading.Thread(target=work, daemon=True).start()
    return {"ok": True, "reply": "开始更新，完成后页面会自己刷新。"}


def settle(root):
    """After a restart on the new version: the note about an update in progress is done with."""
    doing = conversation.setting(Path(root), "update:doing", None) or {}
    if doing.get("step") and not newer((conversation.setting(Path(root), "update:latest", None) or {}).get("version"), current()):
        _say(root)
