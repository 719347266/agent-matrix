"""The AI employee: up to ten concurrent jobs, one conversation per item, results come back by themselves.

    python -m agent_matrix.worker run        # work until nothing is left (started by the listener)
    python -m agent_matrix.worker kick       # start `run` in the background unless it is already running
    python -m agent_matrix.worker status     # what it is doing right now (JSON, for the panel)

Each available lane picks new items, meetings, chat batches and AI work through the shared driver protocol.
Each AI item gets its own folder under state/work/<record_id>/ and its own GPT conversation (codex
exec); later feedback from the owner resumes the same conversation instead of starting over.

The model only reads (Feishu, read-only) and writes files in its folder: 交付.md and 结果.json.
Code does everything else: claim, background pack, checks, creating the Feishu document, writing the
table, and sending the card. Nothing here holds the project lock while the model is thinking.
"""
import argparse
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

SECTIONS_ALWAYS = ["用到的资料", "我做的假设"]
TEMPLATE_HINT = {"周报": "本周完成 / 进行中 / 下周计划 / 风险", "数据汇总": "结论 / 数据表 / 口径",
                 "方案": "推荐 / 选项对比 / 需要你拍板", "会议材料": "议程 / 需要决策 / 背景数据",
                 "回复草稿": "草稿 / 需要你确认", "其他": "按完成标准逐条覆盖"}
MAX_FIX_ROUNDS = 2
IDLE_POLL_SECONDS = 15  # a free lane looks for new work this often while other lanes are busy


# ---- small helpers ------------------------------------------------------------------------------------

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
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


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


# ---- running the model ----------------------------------------------------------------------------------

class CodexRun:
    """One `codex exec` turn in a folder, streamed so the panel can follow it."""

    def __init__(self, config, workdir, *, thread=None, effort=None, timeout_minutes=None, ephemeral=False):
        self.config, self.workdir, self.thread = config, Path(workdir), thread
        self.effort = effort or config.worker_effort
        self.timeout = 60 * (timeout_minutes or config.worker_timeout_minutes)
        self.ephemeral = ephemeral
        self.usage, self.commands, self.timed_out, self.returncode = {}, 0, False, None
        self.stopped = False
        self.messages = 0

    def argv(self):
        binary = codex_binary(self.config)
        if not binary:
            raise MatrixError("找不到 GPT 命令行（codex）。在 config.yaml 的 worker_codex 写上它的路径。")
        argv = [binary, "exec"]
        if self.thread:
            argv += ["resume", self.thread]
        argv += ["--json", "--skip-git-repo-check", "--ignore-user-config", *provider_overrides(), "-m", self.config.worker_model,
                 "-c", f"model_reasoning_effort={self.effort}", "-c", 'sandbox_mode="workspace-write"',
                 "-c", "sandbox_workspace_write.network_access=true", "-o", str(self.workdir / ".last.txt")]
        if not self.thread:
            argv += ["-C", str(self.workdir)]
        if self.ephemeral:
            argv.append("--ephemeral")
        return [*argv, "-"]

    def _kill_tree(self, proc):
        """Stop commands already started by this driver as well as the driver itself."""
        if os.name == "nt":
            if proc.poll() is None:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                               timeout=10, creationflags=0x08000000)
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                # Some process hosts disallow group signals even for our own child. Only
                # target descendants of this exact process; never signal unrelated jobs.
                if proc.poll() is None:
                    result = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, check=True)
                    children = {}
                    for line in result.stdout.splitlines():
                        pid, parent = map(int, line.split())
                        children.setdefault(parent, []).append(pid)
                    descendants = []
                    pending = [proc.pid]
                    while pending:
                        parent = pending.pop()
                        found = children.get(parent, [])
                        descendants.extend(found)
                        pending.extend(found)
                    for pid in reversed(descendants):
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
        if proc.poll() is None:
            proc.kill()

    def run(self, prompt, on_event=None, on_tick=None, should_stop=None):
        if should_stop and should_stop():
            self.stopped = True
            return self
        flags = 0x08000000 if os.name == "nt" else 0  # no console window on Windows
        proc = subprocess.Popen(self.argv(), cwd=self.workdir, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                env=_env(self.config), creationflags=flags, start_new_session=os.name != "nt")
        lines = queue_mod.Queue()
        done = threading.Event()

        def read_lines():
            try:
                for line in proc.stdout:
                    lines.put(line)
            finally:
                lines.put(None)

        def watch_stop():
            # A progress callback can be waiting on Feishu; cancellation must still stop the process.
            while not done.wait(0.5):
                if should_stop():
                    self.stopped = True
                    self._kill_tree(proc)
                    return

        reader = threading.Thread(target=read_lines, daemon=True)
        watcher = threading.Thread(target=watch_stop, daemon=True) if should_stop else None
        reader.start()
        if watcher:
            watcher.start()
        try:
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
            except BrokenPipeError:
                pass
            self._loop(proc, lines, time.monotonic() + self.timeout, on_event, on_tick, should_stop)
        finally:
            done.set()
            if watcher:
                watcher.join(timeout=2)
            if self.stopped or proc.poll() is None:
                self._kill_tree(proc)
            try:
                self.returncode = proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
            reader.join(timeout=2)
            proc.stdout.close()
        return self

    def _loop(self, proc, lines, deadline, on_event, on_tick, should_stop=None):
        finished = False
        while not finished:
            try:
                line = lines.get(timeout=0.5)
            except queue_mod.Empty:
                line = ""
            if self.stopped or (should_stop and should_stop()):
                self.stopped = True
                self._kill_tree(proc)
                return
            if line is None:
                finished = True
            elif line:
                self._event(line, on_event)
            if on_tick:
                on_tick()
            if time.monotonic() > deadline:
                self.timed_out = True
                self._kill_tree(proc)
                finished = True

    def _event(self, line, on_event):
        try:
            event = json.loads(line)
        except ValueError:
            return
        kind = event.get("type")
        if kind == "thread.started" and event.get("thread_id"):
            self.thread = event["thread_id"]
            if not self.ephemeral:
                (self.workdir / ".thread").write_text(self.thread)
        elif kind == "turn.completed":
            for k, v in (event.get("usage") or {}).items():
                self.usage[k] = self.usage.get(k, 0) + (v or 0)
        item = event.get("item") or {}
        if item.get("type") == "command_execution" and kind == "item.started":
            self.commands += 1
        if item.get("type") == "agent_message" and kind == "item.completed":
            self.messages += 1
        if on_event:
            on_event(event)


# ---- checking what came back ----------------------------------------------------------------------------

def _plain(markdown):
    return "".join(re.sub(r"[#*`>|_\-\[\]()!~]", "", markdown).split())


def _section_names(markdown):
    return [re.sub(r"\s+", "", m.group(1)) for m in re.finditer(r"^#{1,6}\s*(.+?)\s*$", markdown, re.M)]


def check_result(workdir, row):
    """(result, problem). problem is a short sentence the model can act on, or None when it passes."""
    from .prework import PLACEHOLDERS, TEMPLATES
    try:
        result = json.loads((Path(workdir) / "结果.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "没有找到有效的 结果.json。按说明写好再结束。"
    if not isinstance(result, dict) or result.get("outcome") not in {"done", "blocked"}:
        return result, "结果.json 的 outcome 只能是 done 或 blocked。"
    if result["outcome"] == "blocked":
        if not str(result.get("question") or "").strip() or not str(result.get("tried") or "").strip():
            return result, "卡住时必须写 question（问本人的一个具体问题）和 tried（已经试过哪些办法）。"
        return result, None
    template = result.get("template")
    if template not in TEMPLATES:
        return result, "template 只能是：" + "、".join(TEMPLATES) + "。"
    summary = str(result.get("summary") or "").strip()
    if not summary or len(summary) > 40:
        return result, "summary 要有，且不超过 40 字。"
    if len(str(result.get("confirmation") or "")) > 30:
        return result, "confirmation 不超过 30 字。"
    if not result.get("sources"):
        return result, "sources 要列出实际用到的资料。"
    try:
        doc = (Path(workdir) / "交付.md").read_text(encoding="utf-8")
    except OSError:
        return result, "没有找到 交付.md。"
    names = _section_names(doc)
    missing = [s for s in SECTIONS_ALWAYS if not any(s in n for n in names)]
    if missing:
        return result, "交付.md 最后要有这两节小标题：" + "、".join(f"## {s}" for s in missing) + "。"
    sections, cap = TEMPLATES[template]
    missing = [s for s in sections if not any(s in n for n in names)]
    if missing:
        return result, f"按「{template}」模板，交付.md 缺少这些小标题：" + "、".join(missing) + "。"
    found = [w for w in PLACEHOLDERS if w in doc]
    if found:
        return result, "交付.md 里还有占位文字：" + "、".join(found) + "。换成真实内容。"
    body = re.split(r"^#{1,6}\s*用到的资料", doc, flags=re.M)[0]
    length = len(_plain(body))
    if length > cap:
        return result, f"正文 {length} 字，「{template}」上限 {cap} 字。先给结论，删掉铺垫和重复。"
    if length < 80:
        return result, "交付.md 正文太短，像没做完。"
    return result, None


# ---- one AI item ---------------------------------------------------------------------------------------

def workdir_for(root, record_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", record_id or ""):
        raise MatrixError("事项 ID 无效。")
    return scratch(root, record_id)


def record_copy(root, record_id):
    """The project keeps its own copy of each item's folder (deliverable, result, notes for review)."""
    path = Path(root) / "state/work" / record_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _changes(workdir, row):
    """What the owner added since the last round: new 补充材料 / 退回意见 lines."""
    from .background import _one
    try:
        seen = json.loads((workdir / ".seen.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        seen = {}
    out = []
    for key in ("补充材料", "退回意见", "完成标准"):
        new = [x for x in _one(row.get(key)).splitlines() if x.strip()]
        old = set(x for x in str(seen.get(key) or "").splitlines() if x.strip())
        out += [f"{key}：{x}" for x in new if x not in old]
    return out


def _remember(workdir, row):
    from .background import _one
    (workdir / ".seen.json").write_text(json.dumps({k: _one(row.get(k)) for k in ("补充材料", "退回意见", "完成标准")},
                                                   ensure_ascii=False), encoding="utf-8")


def first_prompt(root, row):
    from .background import _one
    template = (Path(root) / "prompts/worker.md").read_text(encoding="utf-8")
    hints = "；".join(f"{k}：{v}" for k, v in TEMPLATE_HINT.items())
    return template.format(item=f"#{row.get('编号')} {_one(row.get('标题'))}", templates=hints)


def resume_prompt(changes, timed_out):
    if changes:
        lines = "\n".join("- " + c for c in changes)
        return ("本人有新的要求，任务单.md 已更新：\n" + lines +
                "\n\n在上一版（上一版.md）基础上改，逐条改到。规则不变：先补背景，写 step.txt，"
                "最后重写 交付.md 和 结果.json 再结束。")
    if timed_out:
        return "上一轮时间到了，没做完。接着做，不要从头来；最后写好 交付.md 和 结果.json 再结束。"
    return "接着上次往下做，最后写好 交付.md 和 结果.json 再结束。"


class Item:
    """Runs one claimed item from background to card."""

    def __init__(self, root, config, pick):
        self.root, self.config = Path(root), config
        self.rid, self.version = pick["record_id"], pick["input_version"]
        self.title, self.n = pick.get("title") or "", None
        self.workdir = workdir_for(root, self.rid)
        self.last_step, self.last_step_write = None, 0.0

    # status shown on the panel
    def show(self, stage, step=None):
        write_status(self.root, kind="item", record_id=self.rid, n=self.n, title=self.title[:26], stage=stage,
                     step=step if step is not None else read_status(self.root).get("step", ""),
                     stage_at=now().isoformat(timespec="seconds"))

    def claim(self, lease_minutes=None):
        from .prework import current, read_notes
        from .prework_store import WorkQueue
        from .workflow import Gateway
        with project(self.root, self.config) as (client, state):
            current(self.config, client, state, self.rid, self.version)
            claimed = WorkQueue(state).claim(self.rid, self.version, now(),
                                             lease_minutes=lease_minutes or self.config.worker_timeout_minutes * (MAX_FIX_ROUNDS + 1) + 30)
            if not claimed:
                return None
            rows = Gateway(client, state).rows()
            row = next((r for r in rows if r["record_id"] == self.rid), None)
            notes = read_notes(self.root, self.rid)
        if row is None:
            return None
        from .background import _one
        self.n, self.title = row.get("编号"), _one(row.get("标题")) or self.title
        return client, rows, row, notes

    def prepare_folder(self, client, rows, row, notes):
        from . import background
        keep = record_copy(self.root, self.rid)
        for name in (".thread", "交付.md", ".seen.json"):  # carry the conversation over if the folder moved
            if (keep / name).is_file() and not (self.workdir / name).is_file():
                shutil.copyfile(keep / name, self.workdir / name)
        previous = None
        if (self.workdir / "交付.md").is_file():
            shutil.copyfile(self.workdir / "交付.md", self.workdir / "上一版.md")
            previous = "上一版.md"
        for name in ("结果.json", "step.txt", ".last.txt"):
            (self.workdir / name).unlink(missing_ok=True)
        (self.workdir / "任务单.md").write_text(background.task_sheet(row, rows), encoding="utf-8")
        self.show("找背景", "程序在拉背景")
        own_base = "/base/" + self.config.base_token if self.config.base_token else ""
        (self.workdir / "背景.md").write_text(background.build(self.root, client, rows, row, now(), notes=notes, previous=previous,
                                                             skip_urls=[own_base], copy_to=self.workdir), encoding="utf-8")

    def on_tick(self):
        """Mirror step.txt to the panel right away and to the table's 进度 at most once a minute."""
        try:
            step = (self.workdir / "step.txt").read_text(encoding="utf-8").strip().splitlines()[0][:15]
        except (OSError, IndexError):
            return
        if step == self.last_step:
            return
        self.last_step = step
        self.show("在做", step)
        if time.monotonic() - self.last_step_write >= 60:
            self.last_step_write = time.monotonic()
            try:
                from .prework import report_progress
                with project(self.root, self.config, wait=20) as (client, state):
                    report_progress(self.config, client, state, self.rid, step)
            except (MatrixError, MatrixBusy, OSError):
                pass

    def require_active(self):
        job_id = getattr(self, "job_id", None)
        if job_id:
            from . import drive
            if drive.get(self.root, job_id)["status"] != "open":
                raise MatrixError("这件活已经结束，旧运行不能再写入。")

    def release(self):
        """Give the item back to the queue at once (interrupted, or the driver let it go)."""
        from .prework_store import WorkQueue
        try:
            with project(self.root, self.config, client=False, wait=30) as (_, state):
                from . import drive
                if any(j["kind"] == "item" and j["id"] != getattr(self, "job_id", None)
                       and j["ref"].get("record_id") == self.rid and j["ref"].get("version") == self.version
                       for j in drive.open_jobs(self.root)):
                    return  # a restored run already owns this lease
                WorkQueue(state).release(self.rid, self.version)
        except Exception:
            pass

    def mirror_reply(self):
        """What the AI said at the end of its turn goes into the item's conversation in 心响."""
        try:
            text = (self.workdir / ".last.txt").read_text(encoding="utf-8").strip()
        except OSError:
            return
        if text:
            from . import thread as conversation
            conversation.add(self.root, self.rid, "ai", "say", text[:3000], driver="本机 GPT")
        (self.workdir / ".last.txt").unlink(missing_ok=True)

    def defer(self, reason, tokens):
        from .prework import add_note
        from .prework_store import WorkQueue
        add_note(self.root, self.rid, reason)
        with project(self.root, self.config, client=False) as (_, state):
            WorkQueue(state).defer(self.rid, self.version, now(), reason)
        self.done_status("推迟", reason, tokens)
        return {"record_id": self.rid, "result": "deferred", "reason": reason}

    def block(self, question, result, tokens):
        from .prework import _v1, add_note, current, mark_blocked, notify
        from .prework_store import WorkQueue
        with project(self.root, self.config) as (client, state):
            self.require_active()
            current(self.config, client, state, self.rid, self.version)
            add_note(self.root, self.rid, f"卡住：{question}；试过：{result.get('tried', '')}；{result.get('note', '')}")
            WorkQueue(state).block(self.rid, self.version, question)
            if _v1(state):
                mark_blocked(self.config, client, state, self.rid, question)
            notify(self.config, client, state)
        self.done_status("卡住", question[:28], tokens)
        return {"record_id": self.rid, "result": "blocked", "question": question}

    def publish(self, result, tokens):
        from .prework import add_note, notify, publish_markdown
        self.show("交付中", "建飞书文档")
        keep = record_copy(self.root, self.rid)
        for name in ("交付.md", "结果.json", "任务单.md", "背景.md", ".thread", ".seen.json"):
            if (self.workdir / name).is_file():
                shutil.copyfile(self.workdir / name, keep / name)
        with project(self.root, self.config) as (client, state):
            self.require_active()
            published = publish_markdown(self.config, client, state, self.rid, self.version,
                                         keep / "交付.md", result, title=self.title)
            notify(self.config, client, state)
        from . import thread as conversation
        doc = (keep / "交付.md").read_text(encoding="utf-8")
        version = conversation.add_deliverable(
            self.root, self.rid, summary=result.get("summary", ""), url=published["url"], confirm=result.get("confirmation", ""),
            assumptions=conversation.assumptions_of(doc), sources=[x for x in result.get("sources", []) if str(x).startswith("http")])
        add_note(self.root, self.rid, f"已交付 v{version}：{published['url']}；{result.get('note', '')}")
        self.done_status("已交付", result["summary"], tokens, url=published["url"])
        return {"record_id": self.rid, "result": "published", "url": published["url"]}

    def done_status(self, outcome, text, tokens, url=None):
        from . import activity
        self.finished = True
        activity.log(self.root, "prework", f"#{self.n} {self.title[:20]} · {outcome}", detail=text, ref=url or "")
        write_status(self.root, last={"n": self.n, "title": self.title[:26], "outcome": outcome, "text": text[:40],
                                      "tokens": tokens, "at": now().isoformat(timespec="seconds")},
                     stage="空闲", step="", record_id=None, n=None, title="")


# ---- new items: the AI decides whether it can do them (owner's switch in 心响) -----------------------------

TRIAGE_LABEL = {"ai": "AI 能独立做，已接手", "prep": "需要你来做，AI 先备材料", "me": "需要你自己来",
                "skip": "不用做，已放下", "done": "你已经办完了，已关掉"}


def _pending_new(rows, judged):
    out = []
    for r in rows:
        status = (r.get("状态") or [None])[0] if isinstance(r.get("状态"), list) else r.get("状态")
        who = (r.get("执行方") or [None])[0] if isinstance(r.get("执行方"), list) else r.get("执行方")
        if status != "待确认" or who == "别人" or r.get("排除本人提醒") or r.get("record_id") in judged:
            continue
        try:
            n = int(str(r.get("编号")).strip())
        except (TypeError, ValueError):
            continue
        from .background import _one
        out.append({"n": n, "record_id": r["record_id"], "who": who or "", "title": _one(r.get("标题")),
                    "what": _one(r.get("下一步动作"))[:300], "quote": _one(r.get("原话引用"))[:300], "source": _one(r.get("来源")),
                    "since": _one(r.get("创建时间"))[:16]})
    return out


# ---- the queue ------------------------------------------------------------------------------------------

def pick_next(root, config, skip=()):
    """Next AI item: the owner's "先做" first, then by due date (prework.check already orders by due date)."""
    code, checked = _check(root, "agent_matrix.prework")
    ready = [r for r in checked.get("ready") or [] if r.get("status") in {"queued", "running", "prepared"}
             and r["record_id"] not in skip]
    with project(root, config, client=False, wait=60) as (_, state):
        first = state.get("work:first", [])
    ready.sort(key=lambda r: (r["record_id"] not in first, first.index(r["record_id"]) if r["record_id"] in first else 0))
    write_status(root, queue=[{"record_id": r["record_id"], "title": str(r.get("title") or "")[:26]} for r in ready[1:]])
    return ready[0] if ready else None


def _tick(root, config, job, item, seen):
    """Mirror step.txt to the panel and to the job (and, once a minute, to the table's 进度)."""
    try:
        text = (item.workdir / "step.txt").read_text(encoding="utf-8").strip().splitlines()[0][:15]
    except (OSError, IndexError):
        return
    if text != seen.get("step"):
        seen["step"] = text
        from . import drive
        drive.step(root, config, job["id"], text)
        item.show("在做", text)


def run_item_job(root, config, job):
    """The built-in driver on one item: resume its own GPT conversation when it has one."""
    from . import drive
    item = drive._item(root, config, job)
    write_status(root, kind="item", record_id=item.rid, n=job.get("n"), title=(job.get("title") or "")[:26],
                 stage="找背景", step="读任务单和背景", item_started=now().isoformat(timespec="seconds"),
                 stage_at=now().isoformat(timespec="seconds"))
    thread_file, timed = item.workdir / ".thread", item.workdir / ".timed_out"
    thread = thread_file.read_text().strip() if thread_file.is_file() else None
    prompt, seen, result = (job["builtin_prompt"] if thread else job["instructions"]), {}, {}
    stop = lambda: drive.get(root, job["id"])["status"] != "open"
    for _ in range(MAX_FIX_ROUNDS + 1):
        run = CodexRun(config, item.workdir, thread=thread).run(prompt, on_tick=lambda: _tick(root, config, job, item, seen), should_stop=stop)
        if not run.stopped and not stop() and thread and not run.messages and not run.timed_out and run.returncode:
            # the old conversation cannot be resumed: start a new one, the history comes with the instructions
            thread_file.unlink(missing_ok=True)
            thread = None
            run = CodexRun(config, item.workdir).run(job["instructions"], on_tick=lambda: _tick(root, config, job, item, seen), should_stop=stop)
        if run.stopped or stop():
            item.release()
            from .prework import add_note
            add_note(root, item.rid, "本轮已停止，本机 GPT 停下了")
            return {"status": "cancelled"}
        thread = run.thread or thread
        item.mirror_reply()
        if run.timed_out or not thread:
            if run.timed_out:
                timed.write_text(now().isoformat())
            item.defer(f"这一轮超时（{config.worker_timeout_minutes} 分钟），下一轮接着做" if run.timed_out else "GPT 没有启动成功，稍后重试", 0)
            drive._close(root, drive.get(root, job["id"]), "released")
            return {"status": "deferred"}
        timed.unlink(missing_ok=True)
        item.show("自检", "程序检查交付")
        result = drive.submit(root, config, job["id"])
        if result.get("status") != "fix":
            return result
        prompt = f"程序检查没通过：{result['problem']}\n改好 交付.md / 结果.json 再结束。"
    return result


def run_job(root, config, job):
    """The built-in driver on any job; extraction and triage run once, plus one round of fixes."""
    from . import drive
    if job["kind"] == "item":
        return run_item_job(root, config, job)
    write_status(root, kind=job["kind"], stage=job.get("title") or job["kind"], step="", n=None, title=(job.get("title") or "")[:26],
                 stage_at=now().isoformat(timespec="seconds"))
    folder = Path(job["folder"])
    run = CodexRun(config, folder, effort=config.extract_effort, timeout_minutes=15).run(job["instructions"])
    result = drive.submit(root, config, job["id"])
    if result.get("status") == "fix" and run.thread:
        CodexRun(config, folder, thread=run.thread, effort=config.extract_effort, timeout_minutes=10).run(
            f"程序检查没通过：{result['problem']}\n改好 {'、'.join(job.get('output') or [])} 再结束。")
        result = drive.submit(root, config, job["id"])
    if result.get("status") == "fix":
        return drive.fail(root, drive.get(root, job["id"]), result.get("problem") or "改了一轮还是没通过检查")
    return result


def run_loop(root, config, *, max_jobs=200):
    """The built-in driver (本机 GPT): up to `worker_concurrency` jobs at the same time (default 10).

    Taking a job is serialised (so no job is taken twice); the work itself runs in parallel. A lane that
    finds nothing keeps looking while other lanes are busy, so new work never waits for the whole batch."""
    from . import activity, drive, thread as conversation
    root = Path(root)
    if not conversation.setting(root, "builtin_driver", True):
        return {"ok": True, "jobs": 0, "builtin": "off"}
    if os.name != "nt":
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit(143)))
    os.environ["XINXIANG_DRIVER"] = "本机 GPT"
    width = max(1, min(20, int(getattr(config, "worker_concurrency", 10) or 1)))
    take, active, failed, counter = threading.Lock(), {}, set(), {"n": 0}

    def lane():
        while True:
            with take:
                if counter["n"] >= max_jobs:
                    return
                try:
                    job = drive.next_job(root, config, "本机 GPT", skip=set(failed))
                except (MatrixError, MatrixBusy, OSError) as exc:
                    activity.log(root, "error", "本机 GPT 领活出错", detail=str(exc)[:300], status="failed")
                    job = None
                if job:
                    active[job["id"]], counter["n"] = job, counter["n"] + 1
                elif not active:
                    return  # nothing to do and nobody busy: done
            if not job:
                time.sleep(IDLE_POLL_SECONDS)  # others are still working; new work may arrive meanwhile
                continue
            try:
                activity.beat(root, "AI 员工", f"在做 {len(active)} 件")
                run_job(root, config, job)
            except (MatrixError, MatrixBusy, OSError) as exc:
                if job.get("ref", {}).get("record_id"):
                    with take:
                        failed.add(job["ref"]["record_id"])
                activity.log(root, "error", f"本机 GPT 处理「{str(job.get('title'))[:16]}」出错", detail=str(exc)[:300], status="failed")
                try:
                    drive.release(root, config, job["id"])
                except Exception:
                    pass
            finally:
                with take:
                    active.pop(job["id"], None)

    with exclusive_lock(root / "state/worker", wait=0):
        write_status(root, running=True, pid=os.getpid(), started_at=now().isoformat(timespec="seconds"), stage="开始",
                     step="", record_id=None, n=None, title="", lanes=width)
        lanes = [threading.Thread(target=lane, daemon=True, name=f"lane-{k}") for k in range(width)]
        try:
            for t in lanes:
                t.start()
            for t in lanes:
                while t.is_alive():
                    t.join(timeout=1)
        finally:
            for job in list(active.values()):  # interrupted: give unfinished work back at once
                try:
                    drive.release(root, config, job["id"])
                except Exception:
                    pass
            write_status(root, running=False, stage="空闲", step="", record_id=None, n=None, title="", queue=[])
            activity.beat(root, "AI 员工", "空闲")
    return {"ok": True, "jobs": counter["n"]}


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


def main(argv=None):
    from .config import load
    parser = argparse.ArgumentParser(description="AI 员工：最多并发十件，每件一个对话")
    parser.add_argument("command", choices=["run", "kick", "status"])
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    args = parser.parse_args(argv)
    path = args.config.resolve()
    config = load(path)
    if args.command == "status":
        print(json.dumps({**read_status(path.parent), "alive": is_running(path.parent)}, ensure_ascii=False))
        return 0
    if args.command == "kick":
        print(json.dumps({"ok": True, "worker": kick(path.parent, config)}))
        return 0
    if not config.worker_enabled:
        print(json.dumps({"ok": True, "worker": "off"}))
        return 0
    try:
        print(json.dumps(run_loop(path.parent, config), ensure_ascii=False))
        return 0
    except MatrixBusy:
        print(json.dumps({"ok": True, "worker": "already_running"}))
        return 0
    except Exception as exc:  # the panel must see why the employee stopped
        from . import activity
        activity.log(path.parent, "error", "AI 员工停下了", detail=f"{type(exc).__name__}: {exc}"[:300], status="failed")
        write_status(path.parent, running=False, stage="出错", step=str(exc)[:40])
        raise


if __name__ == "__main__":
    raise SystemExit(main())
