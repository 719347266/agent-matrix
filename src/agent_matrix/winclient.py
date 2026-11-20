"""心响 for Windows: the one program the owner double-clicks.

Nothing to set up by hand. The first start makes a place for the data (%LOCALAPPDATA%\\Xinxiang) with a
config that needs no account at all (items live on this computer, nothing is read from anywhere), starts
the background program, opens 心响 in its own window and sits in the tray. Office tools and the AI are
connected later, in 心响 itself (设置 → 配置检查 says what is missing and what to do).

    pythonw -m agent_matrix.winclient            the normal start (Start menu, desktop, at sign-in)
    python  -m agent_matrix.winclient --headless no window and no tray: for checking a build
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def data_root():
    """Where config, items and logs live. A plain path: tools the AI runs do not all cope with Chinese ones."""
    if os.environ.get("XINXIANG_HOME"):
        return Path(os.environ["XINXIANG_HOME"])
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData/Local")
    return Path(base) / "Xinxiang"


def ensure_config(root):
    """A first config that works with no account: add items by hand, connect things later from the page."""
    from .config import load, save
    path = Path(root) / "config.yaml"
    if path.exists():
        return False
    config = load(path)  # the defaults
    config.item_store = "device"
    config.chat_watch_enabled = config.feishu_tasks_enabled = config.meeting_watch_enabled = False
    config.prework_enabled = True   # what is handed to the AI gets worked on (the default mode says so)
    config.brief_channel = "local"
    if os.environ.get("XINXIANG_PORT"):
        config.app_port = int(os.environ["XINXIANG_PORT"])
    save(path, config)
    return True


def url_of(root):
    from .config import load
    return f"http://127.0.0.1:{load(Path(root) / 'config.yaml').app_port}/"


def serving(url):
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return "<title>心响</title>" in r.read(4000).decode("utf-8", "ignore")
    except Exception:
        return False


def start_listener(root):
    logs = Path(root) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    out = open(logs / "listen.log", "a", buffering=1, encoding="utf-8")
    return subprocess.Popen([sys.executable, "-u", "-m", "agent_matrix.listen"], cwd=root, stdout=out, stderr=out,
                            stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)


def open_window(url):
    """心响 in a window of its own (Edge's app mode, which every Windows 10/11 has); else the default browser."""
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        edge = Path(base or "") / "Microsoft/Edge/Application/msedge.exe"
        if base and edge.is_file():
            try:
                # "#win": the page sizes its own window once (Edge keeps whatever size its last app window had)
                # no "translate this page?" bubble or other browser prompts: this is an app window, not a web page
                subprocess.Popen([str(edge), f"--app={url}#win", "--window-size=1320,880", "--disable-features=Translate,msEdgeTranslate",
                                  "--no-first-run", "--no-default-browser-check"], creationflags=NO_WINDOW)
                return "edge"
            except OSError:
                break
    webbrowser.open(url)
    return "browser"


class Keeper:
    """Keeps the background program running for as long as the client does."""

    def __init__(self, root, url):
        self.root, self.url, self.proc, self.stopping = Path(root), url, None, False

    def start(self, wait=60):
        if not serving(self.url):  # someone else (an earlier start) may already be serving
            self.proc = start_listener(self.root)
        until = time.time() + wait
        while time.time() < until:
            if serving(self.url):
                return True
            time.sleep(1)
        return False

    def restart(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = start_listener(self.root)

    def watch(self):
        while not self.stopping:
            time.sleep(20)
            if self.proc and self.proc.poll() is not None and not self.stopping:  # it died: bring it back
                self.proc = start_listener(self.root)

    def stop(self):
        self.stopping = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


def main(argv=None):
    p = argparse.ArgumentParser(description="心响 Windows 客户端")
    p.add_argument("--headless", action="store_true", help="不开窗口、不进托盘（检查构建用）")
    p.add_argument("--no-window", action="store_true", help="开机自启时用：只进托盘，不弹窗口")
    args = p.parse_args(argv)
    if not sys.flags.utf8_mode:  # every file here is UTF-8; say so once, for this program and all it starts
        env = dict(os.environ, PYTHONUTF8="1", XINXIANG_CLIENT="1")
        return subprocess.call([sys.executable, "-m", "agent_matrix.winclient", *(argv if argv is not None else sys.argv[1:])],
                               env=env, creationflags=NO_WINDOW)
    os.environ["XINXIANG_CLIENT"] = "1"   # the page then says "reopen 心响", not "run the install script"
    root = data_root()
    (root / "logs").mkdir(parents=True, exist_ok=True)
    if sys.stdout is None:  # pythonw: no console
        sys.stdout = sys.stderr = open(root / "logs/client.log", "a", buffering=1, encoding="utf-8")
    first = ensure_config(root)
    os.chdir(root)
    url = url_of(root)
    already = serving(url)
    keeper = Keeper(root, url)
    ok = keeper.start()
    print(f"心响 {url} data={root} first_run={first} serving={ok}", flush=True)
    if args.headless:
        if not ok:
            return 1
        try:
            while keeper.proc and keeper.proc.poll() is None:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        keeper.stop()
        return 0
    if not args.no_window or first:
        open_window(url)
    if already:  # the tray of the first start is still there; this one only opened the window
        return 0
    threading.Thread(target=keeper.watch, daemon=True).start()
    try:
        from .tray import Tray

        class ClientTray(Tray):
            def restart_listener(self):
                keeper.restart()

            def open(self, target):
                open_window(target) if target and target.startswith(url) else super().open(target)
        ClientTray(root).run()   # returns when the owner picks 退出
    except Exception as exc:     # no tray on this desktop: 心响 still has to keep running
        print(f"tray unavailable ({type(exc).__name__}: {exc}); running without it", flush=True)
        threading.Event().wait()   # until this program is closed; the keeper thread looks after the background program
    finally:
        keeper.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
