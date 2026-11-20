"""Where the built-in AI employee runs: its status file, the local AI programs (codex, claude), their
environment and work folders, and starting the worker process. Nothing here knows what an item is."""
from contextlib import contextmanager
from datetime import datetime, timedelta
from glob import glob
from hashlib import sha256
import json
import os
from pathlib import Path
import queue as queue_mod
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

from .models import MatrixBusy, MatrixError
from .state import State, exclusive_lock
from .timeutil import TZ


def now():
    return datetime.now(TZ)


def status_path(root):
    return Path(root) / "state/worker.json"


def read_status(root):
    try:
        return json.loads(status_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


_STATUS_LOCK = threading.Lock()


def write_status(root, **fields):
    """The panel reads this file; written atomically, never under the project lock (lanes share it)."""
    with _STATUS_LOCK:
        data = {**read_status(root), **fields, "updated_at": now().isoformat(timespec="seconds")}
        path = status_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
        return data


def _alive(pid):
    from .procs import alive
    return alive(pid)   # not a signal-0 probe: on Windows that ends the process


def is_running(root):
    s = read_status(root)
    return bool(s.get("running") and _alive(s.get("pid")))


def _version_key(path):
    m = re.search(r"/(\d+(?:\.\d+)*)/", path)
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def codex_binary(config):
    if getattr(config, "worker_codex", ""):
        return config.worker_codex
    found = shutil.which("codex")
    if found:
        return found
    bundled = glob(os.path.expanduser("~/Library/Application Support/Cindy/codex-package/*/bin/codex"))
    return max(bundled, key=_version_key) if bundled else None


def owner_codex_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def worker_codex_home():
    """The AI employee's own GPT folder: its conversations stay out of the owner's GPT app, and the
    owner's global instructions and skills are not loaded. Sign-in is shared through a link to the
    owner's auth.json (never copied). None when that is not possible; then the owner's folder is used."""
    home = Path.home() / ".agent-matrix" / "codex-home"
    if not str(home).isascii():
        return None
    auth = owner_codex_home() / "auth.json"
    if not auth.is_file():
        return None
    try:
        home.mkdir(parents=True, exist_ok=True)
        link = home / "auth.json"
        if not link.is_symlink() or os.readlink(link) != str(auth):
            link.unlink(missing_ok=True)
            link.symlink_to(auth)
        return home
    except OSError:
        return None


def _env(config):
    """GPT's command tool crashes on non-ASCII paths (codex 0.167), so its environment keeps none:
    PATH entries and variables with non-ASCII values (PWD, VIRTUAL_ENV, ...) are left out."""
    env = {k: v for k, v in os.environ.items() if str(v).isascii()}
    env.pop("CODEX_HOME", None)  # a host app (e.g. Cindy) may point this at its own GPT setup
    own = worker_codex_home()
    if own is not None:
        env["CODEX_HOME"] = str(own)
    env.pop("VIRTUAL_ENV", None)
    extra = []
    lark = shutil.which(config.lark_binary) or (config.lark_binary if os.path.isabs(config.lark_binary) else None)
    if lark:
        extra.append(os.path.dirname(lark))
    for d in ("/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.npm-global/bin")):
        if os.path.isdir(d):
            extra.append(d)
    paths = [*extra, *os.environ.get("PATH", "").split(os.pathsep)]
    env["PATH"] = os.pathsep.join(dict.fromkeys(x for x in paths if x and x.isascii()))
    return env


def work_base(root):
    """Where GPT works. Inside the project when its path is plain ASCII; otherwise a mirror under
    ~/.agent-matrix (the command tool cannot run in a folder like 「01 项目」)."""
    root = Path(root).resolve()
    if str(root).isascii():
        return root / "state/work"
    home = Path.home()
    if not str(home).isascii():
        return root / "state/work"
    return home / ".agent-matrix" / ("w" + sha256(str(root).encode()).hexdigest()[:8])


def scratch(root, *parts):
    path = work_base(root).joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def provider_overrides():
    """Run GPT without the owner's plugins, MCP servers and hooks (they distract the model and cost
    tokens), but with the same model provider: copy only `model_provider` and its
    `[model_providers.<name>]` table from the owner's codex config into -c flags."""
    import tomllib
    try:
        data = tomllib.loads((owner_codex_home() / "config.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    name = data.get("model_provider")
    table = (data.get("model_providers") or {}).get(name) if name else None
    if not name or not isinstance(table, dict):
        return []
    flags = ["-c", f"model_provider={json.dumps(name)}"]
    for key, value in table.items():
        if isinstance(value, (str, bool, int, float)):
            flags += ["-c", f"model_providers.{name}.{key}={json.dumps(value)}"]
    return flags


@contextmanager
def project(root, config, *, wait=120, client=True):
    """Short locked section: state (and a verified Feishu client) for a few reads/writes."""
    from .feishu import Feishu
    with exclusive_lock(Path(root) / "state", wait=wait):
        state = State(Path(root) / "state/state.db")
        try:
            feishu = None
            if client:
                feishu = Feishu(config, Path(root))
                feishu.verify_owner(feishu.auth())
            yield feishu, state
        finally:
            state.close()


def _cli(root, module, *args, timeout=300):
    """Run one of our own commands (they take the project lock themselves) and parse its JSON line."""
    run = subprocess.run([sys.executable, "-m", module, *args], cwd=root, capture_output=True, text=True,
                         encoding="utf-8", timeout=timeout)
    lines = [x for x in (run.stdout or "").strip().splitlines() + (run.stderr or "").strip().splitlines() if x.startswith("{")]
    try:
        return run.returncode, json.loads(lines[-1]) if lines else {}
    except ValueError:
        return run.returncode, {}


def _check(root, module, tries=6):
    """`check` commands skip when the project is busy; wait a little instead of concluding "nothing to do"."""
    for _ in range(tries):
        code, out = _cli(root, module, "check")
        if out.get("reason") != "busy":
            return code, out
        time.sleep(5)
    return code, out


def claude_binary(config):
    return getattr(config, "worker_claude", "") or shutil.which("claude")


def gemini_binary(config):
    return getattr(config, "worker_gemini", "") or shutil.which("gemini")


def kick(root, config=None, force=False):
    """Start `run` in the background unless it is already running. Cheap; safe to call every minute.

    Timers call it plainly (a run that just finished is not repeated within 90 s); events that bring
    new work (a reply, a meeting that ended) pass force=True and start it at once."""
    root = Path(root)
    if config is not None and not getattr(config, "worker_enabled", True):
        return "off"
    from . import thread as conversation
    if not conversation.setting(root, "builtin_driver", True):
        return "off"  # the owner lets other AIs drive; they call `xinxiang next` themselves
    if is_running(root):
        return "running"
    if not force:
        try:
            last = datetime.fromisoformat(read_status(root).get("updated_at", ""))
            if now() - last < timedelta(seconds=90):
                return "recent"
        except ValueError:
            pass
    kwargs = {"cwd": root, "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    log = root / "logs/worker.log"
    log.parent.mkdir(exist_ok=True)
    kwargs["stdout"] = kwargs["stderr"] = log.open("a", encoding="utf-8")
    subprocess.Popen([sys.executable, "-m", "agent_matrix.worker", "run"], **kwargs)
    return "started"


def shim_path():
    base = Path.home() / ".agent-matrix" / "bin"
    return base / ("xinxiang.cmd" if os.name == "nt" else "xinxiang")
