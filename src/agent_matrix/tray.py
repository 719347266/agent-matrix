"""Windows tray monitor (also works on Linux desktops; macOS uses the native app in macapp/).

    pythonw -m agent_matrix.tray          # started at logon by windows/install.ps1

Same view as the macOS menu bar app: what needs you, what is moving, what happened today,
and system problems only when there is one. Colored dot: blue = something needs you,
green = all good, orange = needs attention, red = broken. Notifies on every message sent to
you and every error. Reads local data only; no model calls.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import webbrowser

import pystray
from PIL import Image, ImageDraw

from . import monitor

COLORS = {"need": (47, 111, 237), "ok": (43, 154, 102), "warn": (232, 119, 14),
          "error": (229, 72, 77), "unknown": (134, 144, 156)}
LISTENER_TASK = "AgentMatrix Listener"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def noop(icon, item):
    pass


def dot(color):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((6, 6, 58, 58), fill=color)
    return img


class Tray:
    def __init__(self, root):
        self.root = Path(root)
        self.status = {}
        self.seen_path = self.root / "state/tray.json"
        try:
            self.last_seen = json.loads(self.seen_path.read_text()).get("last_seen", 0)
        except (OSError, ValueError):
            self.last_seen = 0
        self.last_restart = 0
        self.icon = pystray.Icon("agent-matrix", dot(COLORS["unknown"]), "事项中心", menu=pystray.Menu(self.items))

    # ---- actions --------------------------------------------------------------
    def python(self, *args, wait=False):
        proc = subprocess.Popen([sys.executable, "-m", *args], cwd=self.root, creationflags=NO_WINDOW)
        if wait:
            proc.wait()

    def open(self, target):
        if not target:
            return
        if target.startswith("http"):
            webbrowser.open(target)
        elif os.name == "nt":
            os.startfile(target)  # noqa: S606 - local monitor page only
        else:
            webbrowser.open(Path(target).as_uri())

    def opener(self, urls):
        """pystray accepts actions taking (icon, item) only, so bind the links in a closure."""
        def action(icon, item):
            for u in urls:
                self.open(u)
        return action

    def command(self, cmd):
        """Run "16 过" / "1 完成" exactly like a typed reply and pop up what happened."""
        def action(icon, item):
            done = subprocess.run([sys.executable, "-m", "agent_matrix.reply", "--text", cmd], cwd=self.root,
                                  capture_output=True, text=True, encoding="utf-8", creationflags=NO_WINDOW)
            try:
                self.icon.notify((done.stdout or done.stderr or "").strip()[:200] or "已处理", "事项中心")
            except Exception:
                pass
            self.refresh()
        return action

    def restart_listener(self):
        if os.name == "nt":
            for verb in ("/End", "/Run"):
                subprocess.run(["schtasks", verb, "/TN", LISTENER_TASK], creationflags=NO_WINDOW, capture_output=True)

    def center(self):
        try:
            return json.loads((self.root / "state/app.json").read_text()).get("url") or "http://127.0.0.1:8765/"
        except (OSError, ValueError):
            return "http://127.0.0.1:8765/"

    def page(self):
        out = self.root / "state/monitor.html"
        out.write_text(monitor.render(self.status or monitor.build(self.root)), encoding="utf-8")
        return str(out)

    # ---- refresh loop ---------------------------------------------------------
    def refresh(self):
        try:
            self.status = monitor.build(self.root)
        except Exception as exc:  # never let the tray die; show the problem instead
            self.status = {"level": "error", "headline": "读不到状态", "summary": str(exc)[:80], "need_you": [],
                           "in_progress": [], "timeline": [], "issues": [], "notify": [], "latest_event_id": self.last_seen}
        s = self.status
        tone = "error" if s["level"] == "error" else "need" if s["need_you"] else s["level"]
        self.icon.icon = dot(COLORS.get(tone, COLORS["unknown"]))
        self.icon.title = (s["headline"] + "\n" + s.get("summary", ""))[:120]
        if s.get("auto_restart_listener") and time.time() - self.last_restart > 300:
            self.last_restart = time.time()
            self.restart_listener()
        self.notify_new(s.get("notify", []), s.get("latest_event_id", 0))
        self.icon.update_menu()

    def notify_new(self, events, latest):
        if self.last_seen == 0 or latest < self.last_seen:
            self.last_seen = latest
        else:
            for e in sorted((e for e in events if e["id"] > self.last_seen), key=lambda e: e["id"])[:4]:
                title = {"send": "心响" if e["ok"] else "心响 · 消息没送到", "prework": "AI 有进展"}.get(
                    e["kind"], "事项中心出了点问题")
                if e["kind"] in {"send", "prework"} or not e["ok"]:
                    try:
                        self.icon.notify(e["title"][:100], title)
                    except Exception:
                        pass
            self.last_seen = max(self.last_seen, latest)
        try:
            self.seen_path.write_text(json.dumps({"last_seen": self.last_seen}))
        except OSError:
            pass

    def loop(self):
        while True:
            self.refresh()
            time.sleep(15)

    # ---- menu (rebuilt each time it opens) -----------------------------------
    def items(self):
        s = self.status or {"headline": "读取中…", "summary": "", "need_you": [], "in_progress": [], "timeline": [], "issues": []}
        Item, Menu = pystray.MenuItem, pystray.Menu

        def task(x):
            text = f"{x['label']}  #{x['n']} {x['title']}" + (f" · {x['due']}" if x.get("due") else "")
            actions = []
            if x.get("url"):
                actions.append(Item("打开交付文件" if x.get("label") in {"已交付", "待你验收", "待你复查"} else "打开", self.opener([x["url"]])))
            if x.get("cmd"):
                actions.append(Item("✓ " + (x.get("cmd_label") or "完成"), self.command(x["cmd"])))
            if x.get("reply"):
                actions.append(Item("回复：" + x["reply"], noop, enabled=False))
            return Item(text, Menu(*actions)) if actions else Item(text, noop, enabled=False)

        yield Item(s["headline"], noop, enabled=False)
        if s.get("summary"):
            yield Item(s["summary"], noop, enabled=False)
        w = s.get("worker") or {}
        if w.get("line"):
            yield Item(w["line"][:80], noop, enabled=False)
        for q in (w.get("queue") or [])[:3]:
            yield Item(f"   排队：#{q.get('n')} {q.get('title')}"[:80], noop, enabled=False)
        if w.get("last"):
            l = w["last"]
            yield Item(f"   上一件 {l.get('time')}：#{l.get('n')} {l.get('outcome')} · {l.get('text')}"[:80], noop, enabled=False)
        for p in [i for i in s["issues"] if i["level"] in {"error", "warn"}]:
            fix = (p.get("action") or {}).get("kind") == "restart_listener"
            yield Item(f"⚠ {p['title']}：{p['you'] or p['done']}", (lambda icon, item: self.restart_listener()) if fix else noop, enabled=fix)
        yield Menu.SEPARATOR
        need = s["need_you"]
        yield Item(f"需要你（{len(need)}）", Menu(*([task(x) for x in need] or [Item("没有要你处理的 ✓", noop, enabled=False)])))
        moving = s["in_progress"]
        extra = [Item(f"另有 {s['backlog_count']} 件没定日期或还没到期", noop, enabled=False)] if s.get("backlog_count") else []
        yield Item(f"正在进行（{len(moving)}）", Menu(*([task(x) for x in moving] + extra or [Item("暂无", noop, enabled=False)])))
        done = s.get("done_recent", [])
        yield Item(f"最近完成（{len(done)}）", Menu(*([task(x) for x in done[:15]] or [Item("最近 7 天还没有完成的事", noop, enabled=False)])))
        lines = [Item(f"{x['time']}  {x['kind']}  {x['title']}"[:80], noop, enabled=False) for x in s["timeline"][:15]]
        yield Item("今天发生了什么", Menu(*(lines or [Item("今天还没有动静", noop, enabled=False)])))
        for p in [i for i in s["issues"] if i["level"] == "setup"]:
            urls = (p.get("action") or {}).get("urls") or []
            if urls:
                yield Item("⚙ 订阅飞书推送（一次性设置）", self.opener(urls))
        yield Menu.SEPARATOR
        unread = s.get("unread") or 0
        yield Item("打开心响" + (f"（{unread} 条新消息）" if unread else ""), lambda icon, item: self.open(self.center()), default=True)
        yield Item("打开完整页面", lambda icon, item: self.open(self.page()))
        yield Item("打开飞书看板", lambda icon, item: self.open(s.get("board_url")))
        yield Item("更多", Menu(
            Item("立即检查一次", lambda icon, item: self.python("agent_matrix.reply", "poll")),
            Item("给我发条测试消息", lambda icon, item: self.python("agent_matrix.monitor", "--test-send")),
            Item("给我发张测试卡片（试按钮）", lambda icon, item: self.python("agent_matrix.monitor", "--test-card")),
            Item("清掉已处理的提示", lambda icon, item: (monitor.clear_resolved(self.root), self.refresh())),
            Item("重启监听", lambda icon, item: self.restart_listener()),
        ))
        yield Item("退出", lambda icon, item: icon.stop())

    def run(self):
        threading.Thread(target=self.loop, daemon=True).start()
        self.icon.run()


def main(argv=None):
    root = Path.cwd()
    if sys.stdout is None:  # pythonw: no console
        (root / "logs").mkdir(exist_ok=True)
        sys.stdout = sys.stderr = open(root / "logs/tray.log", "a", buffering=1, encoding="utf-8")
    Tray(root).run()


if __name__ == "__main__":
    raise SystemExit(main())
