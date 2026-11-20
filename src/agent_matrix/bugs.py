"""What goes wrong while using 心响, written down as it happens and handed in together.

The owner (or whoever is trying a build) says in a sentence what was wrong; 心响 adds what it knows about its own
state at that moment: version, system, what is set up, and its own recent errors. Kept on this computer
(state/xinxiang.db, table bugs) until the owner copies them out or opens them as a GitHub issue. Nothing is sent
by itself. Item titles, messages and file contents are not part of it; home folder names and anything that looks
like a key are blanked.
"""
from datetime import datetime
import json
import os
from pathlib import Path
import platform
import re
import sys

from . import activity, db as database
from .timeutil import TZ

ISSUES = "https://github.com/MILESMAKMAK/agent-matrix/issues/new"
SECRET = re.compile(r"(xox[abp]-[\w-]{6,}|sk-[\w-]{8,}|ntn_[\w-]{8,}|AIza[\w-]{10,}|(?i:token|secret|password|apikey|api_key)\W{0,3}[\w-]{8,})")
TROUBLE = re.compile(r"failed|stopped|Traceback|Error|出错|没成功|error \d", re.I)


def _db(root):
    return database.open(root, "xinxiang")


def scrub(text):
    """No home folder names, nothing that looks like a key."""
    text = str(text or "")
    home = str(Path.home())
    if len(home) > 3:
        text = text.replace(home, "~").replace(home.replace("\\", "/"), "~")
    text = re.sub(r"(?i)([A-Z]:\\Users\\|/Users/|/home/)[^\\/\s\"']+", r"\1…", text)
    return SECRET.sub("<已隐去>", text)


def _version():
    here = Path(__file__).resolve().parents[2] / "pyproject.toml"   # a checkout says its version itself
    try:
        found = re.search(r'^version = "(.+)"', here.read_text(encoding="utf-8"), re.M) if (here.parent / ".git").exists() else None
        if found:
            return found.group(1)
    except OSError:
        pass
    try:
        from importlib.metadata import distributions
        found = [d.version for d in distributions() if (d.metadata["Name"] or "").lower().replace("_", "-") == "agent-matrix"]
        return max(found, key=lambda v: tuple(int(x) for x in re.findall(r"\d+", v)[:3])) if found else ""
    except Exception:
        return ""


def diagnostics(root, *, events=12, lines=25):
    """What 心响 knows about itself right now. Its own errors only; no item or message content."""
    root = Path(root)
    out = {"version": _version(), "system": f"{platform.system()} {platform.release()}", "python": sys.version.split()[0],
           "client": bool(os.environ.get("XINXIANG_CLIENT"))}
    try:
        recent, beats = activity.recent(root, limit=300)
        out["errors"] = [{"at": str(e["at"])[5:16].replace("T", " "), "what": scrub(e["title"])[:120], "detail": scrub(e["detail"])[:300]}
                         for e in recent if e["status"] in ("failed", "error")][:events]
        out["beats"] = {name: str(b["at"])[5:16].replace("T", " ") for name, b in beats.items()}
    except Exception as exc:
        out["errors"], out["beats"] = [{"at": "", "what": f"读不到出错记录（{type(exc).__name__}）", "detail": ""}], {}
    try:
        tail = (root / "logs/listen.log").read_text(encoding="utf-8", errors="replace").splitlines()[-800:]
        out["log"] = [scrub(l)[:300] for l in tail if TROUBLE.search(l)][-lines:]
    except OSError:
        out["log"] = []
    return out


def add(root, text):
    text = str(text or "").strip()[:2000]
    if not text:
        return None
    db = _db(root)
    try:
        with db:
            cur = db.execute("INSERT INTO bugs (at, text, diag, status) VALUES (?,?,?, 'open')",
                             (datetime.now(TZ).isoformat(timespec="seconds"), text, json.dumps(diagnostics(root), ensure_ascii=False)))
            return cur.lastrowid
    finally:
        db.close()


def all(root, with_diag=False):
    db = _db(root)
    try:
        return [{"id": i, "at": at[5:16].replace("T", " "), "text": text, "status": status,
                 **({"diag": json.loads(diag or "{}")} if with_diag else {})}
                for i, at, text, diag, status in db.execute("SELECT id, at, text, diag, status FROM bugs ORDER BY id DESC")]
    finally:
        db.close()


def remove(root, bug_id):
    db = _db(root)
    try:
        with db:
            db.execute("DELETE FROM bugs WHERE id=?", (int(bug_id),))
    finally:
        db.close()


def mark_sent(root, ids=None):
    db = _db(root)
    try:
        with db:
            if ids is None:
                db.execute("UPDATE bugs SET status='sent' WHERE status='open'")
            else:
                db.executemany("UPDATE bugs SET status='sent' WHERE id=?", [(int(i),) for i in ids])
    finally:
        db.close()


def report(root, only_open=True):
    """Everything written down, as one text to hand in: what was reported, then what 心响 knew each time."""
    rows = [b for b in reversed(all(root, with_diag=True)) if not only_open or b["status"] == "open"]
    now = diagnostics(root)
    lines = [f"# 心响问题反馈（{len(rows)} 条）", "",
             f"- 版本：{now['version'] or '未知'}　系统：{now['system']}　Python {now['python']}　{'安装包' if now['client'] else '源码安装'}", ""]
    for k, b in enumerate(rows, 1):
        d = b.get("diag") or {}
        lines += [f"## {k}. {b['text'].splitlines()[0][:80]}", "", f"- 时间：{b['at']}　当时版本：{d.get('version') or '未知'}", ""]
        if "\n" in b["text"] or len(b["text"]) > 80:
            lines += [b["text"], ""]
        if d.get("errors"):
            lines += ["当时心响自己记下的出错：", ""] + [f"- {e['at']} {e['what']}" + (f"：{e['detail']}" if e.get("detail") else "") for e in d["errors"][:6]] + [""]
    if now.get("errors"):
        lines += ["## 最近的出错记录", ""] + [f"- {e['at']} {e['what']}" + (f"：{e['detail']}" if e.get("detail") else "") for e in now["errors"]] + [""]
    if now.get("log"):
        lines += ["## 后台日志里的出错行", "", "```"] + now["log"] + ["```", ""]
    return scrub("\n".join(lines))


def issue_url(root, limit=5500):
    """A GitHub "new issue" page with the report filled in (cut to what a link can carry)."""
    from urllib.parse import urlencode
    body = report(root)
    if len(body) > limit:
        body = body[:limit] + "\n\n（内容太长被截断：完整内容已复制到剪贴板，请粘贴到这里。）"
    count = sum(1 for b in all(root) if b["status"] == "open")
    return ISSUES + "?" + urlencode({"title": f"问题反馈：{count} 条（{_version() or '未知版本'}，{platform.system()}）", "body": body})
