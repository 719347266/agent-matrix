"""One conversation with a local AI program: CodexRun (GPT) and ClaudeRun, and which one the owner chose."""
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
from .workenv import _env, claude_binary, codex_binary, gemini_binary, now, provider_overrides, scratch


class CodexRun:
    """One `codex exec` turn in a folder, streamed so the panel can follow it."""
    THREAD_FILE = ".thread"
    NAME = "本机 GPT"

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

    def env(self):
        return _env(self.config)

    def run(self, prompt, on_event=None, on_tick=None, should_stop=None):
        if should_stop and should_stop():
            self.stopped = True
            return self
        flags = 0x08000000 if os.name == "nt" else 0  # no console window on Windows
        proc = subprocess.Popen(self.argv(), cwd=self.workdir, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                env=self.env(), creationflags=flags, start_new_session=os.name != "nt")
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
                (self.workdir / self.THREAD_FILE).write_text(self.thread)
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


MCP_CACHE_SECONDS = 6 * 3600


MCP_DENY = " ".join(f"mcp__*{w}*" for w in ("send", "publish", "deploy", "merge", "delete", "schedule_message", "post_", "reply",
                                              "create", "update", "move", "restore", "resolve", "comment", "duplicate",
                                              "spawn", "cancel", "upload"))


def mcp_servers(binary, *, run=subprocess.run, cache=None):
    """Tool-name prefixes of the MCP servers the owner's Claude has connected ("claude.ai Notion" → claude_ai_Notion).
    `claude mcp list` health-checks every server (~10 s), so the answer is cached for a few hours."""
    cache = Path(cache or Path.home() / ".cache/agent-matrix-mcp.json")
    try:
        saved = json.loads(cache.read_text())
        if time.time() - saved["at"] < MCP_CACHE_SECONDS:
            return saved["servers"]
    except (OSError, ValueError, KeyError):
        pass
    try:
        out = run([binary, "mcp", "list"], capture_output=True, text=True, timeout=120).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    servers = sorted({re.sub(r"[^A-Za-z0-9_-]", "_", line.split(": ", 1)[0].replace("claude.ai ", "claude_ai_"))
                      for line in out.splitlines() if ": " in line and "✔" in line})
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"at": time.time(), "servers": servers}))
    except OSError:
        pass
    return servers


class ClaudeRun(CodexRun):
    """One `claude -p` turn, same contract as CodexRun. Like GPT: without the owner's CLAUDE.md, plugins,
    hooks and MCP (--safe-mode), except the MCP tools listed in worker_mcp_tools; writes only inside the folder,
    network allowed (OS sandbox)."""
    THREAD_FILE = ".claude-session"
    NAME = "本机 Claude"
    SANDBOX = json.dumps({"sandbox": {"enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False,
                                      "network": {"allowedDomains": ["*"]}}})

    def argv(self):
        binary = claude_binary(self.config)
        if not binary:
            raise MatrixError("找不到 Claude 命令行（claude）。在 config.yaml 的 worker_claude 写上它的路径。")
        wanted = self.config.worker_mcp_tools
        mcp = ([f"mcp__{p}__*" for p in mcp_servers(binary) if self.config.worker_claudeai_mcp or not p.startswith("claude_ai_")]
               if "*" in wanted else [t for t in wanted if t.startswith("mcp__")])
        # --safe-mode turns every MCP server off. With MCP wanted: load the owner's user settings (that is where the
        # MCP servers live) but switch all hooks off, and allow only MCP tools (dontAsk denies everything unlisted).
        settings = json.dumps({**json.loads(self.SANDBOX), "disableAllHooks": True}) if mcp else self.SANDBOX
        argv = [binary, "-p", *(["--setting-sources", "user,project"] if mcp else ["--safe-mode"]),
                "--output-format", "stream-json", "--verbose",
                "--permission-mode", "dontAsk", "--allowedTools", " ".join(["Bash Read Write Edit Glob Grep WebFetch WebSearch", *mcp]),
                "--settings", settings,
                *(["--disallowedTools", MCP_DENY] if mcp else [])]
        if self.config.worker_claude_model:
            argv += ["--model", self.config.worker_claude_model]
        if self.effort in {"low", "medium", "high", "xhigh", "max"}:
            argv += ["--effort", self.effort]
        if self.thread:
            argv += ["--resume", self.thread]
        if self.ephemeral:
            argv.append("--no-session-persistence")
        return argv

    def env(self):
        env = _env(self.config)
        if self.config.worker_mcp_tools and not self.config.worker_claudeai_mcp:
            # A claude.ai connector and a plugin pointing at the same server (Notion, Atlassian…) collapse into one,
            # and the claude.ai one wins even when only the plugin is signed in: leave the claude.ai ones out.
            env["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"
        return env

    def _event(self, line, on_event):
        try:
            event = json.loads(line)
        except ValueError:
            return
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init" and event.get("session_id"):
            self.thread = event["session_id"]
            if not self.ephemeral:
                (self.workdir / self.THREAD_FILE).write_text(self.thread)
        elif kind == "assistant":
            for part in (event.get("message") or {}).get("content") or []:
                if part.get("type") == "tool_use" and part.get("name") == "Bash":
                    self.commands += 1
                elif part.get("type") == "text":
                    self.messages += 1
        elif kind == "result":
            for k in ("input_tokens", "output_tokens", "cache_read_input_tokens"):
                self.usage[k] = self.usage.get(k, 0) + ((event.get("usage") or {}).get(k) or 0)
            if event.get("result"):  # codex writes its last message with -o; claude reports it here
                (self.workdir / ".last.txt").write_text(event["result"], encoding="utf-8")
        if on_event:
            on_event(event)


class GeminiRun(CodexRun):
    """One headless `gemini` turn (Google's Gemini CLI), the prompt on stdin, events as stream-json."""
    THREAD_FILE = ".gemini-session"
    NAME = "本机 Gemini"

    def argv(self):
        binary = gemini_binary(self.config)
        if not binary:
            raise MatrixError("找不到 Gemini 命令行（gemini）。在 config.yaml 的 worker_gemini 写上它的路径。")
        argv = [binary, "-o", "stream-json", "--approval-mode", "yolo"]   # headless: nobody is there to approve a tool
        if sys.platform == "darwin" and self.config.worker_gemini_sandbox:
            argv.append("-s")   # macOS Seatbelt: it can read, use the network, and write only inside this folder
        if self.config.worker_gemini_model:
            argv += ["-m", self.config.worker_gemini_model]
        if self.thread:
            argv += ["--resume", "latest"]   # sessions are kept per folder, and every job has its own folder
        return [*argv, "-p", ""]             # the prompt itself comes on stdin

    def env(self):
        env = _env(self.config)
        env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"   # the job's own folder; without it a headless run refuses to start
        if self.config.worker_gemini_api_key:
            env["GEMINI_API_KEY"] = self.config.worker_gemini_api_key
        return env

    def _event(self, line, on_event):
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "init" and event.get("session_id"):
            self.thread = event["session_id"]
            if not self.ephemeral:
                (self.workdir / self.THREAD_FILE).write_text(self.thread, encoding="utf-8")
        elif kind == "message" and event.get("role") == "assistant":
            if not event.get("delta") or not getattr(self, "_said", ""):
                self.messages += 1
            self._said = (getattr(self, "_said", "") if event.get("delta") else "") + str(event.get("content") or "")
            (self.workdir / ".last.txt").write_text(self._said, encoding="utf-8")   # its last message is the answer
        elif kind == "tool_use":
            self._said = ""   # what it says after a tool is a new message
            if event.get("tool_name") == "run_shell_command":
                self.commands += 1
        elif kind == "result":
            stats = event.get("stats") or {}
            for k in ("input_tokens", "output_tokens", "cached"):
                self.usage[k] = self.usage.get(k, 0) + (stats.get(k) or 0)
        if on_event:
            on_event(event)


ENGINES = {"codex": "CodexRun", "claude": "ClaudeRun", "gemini": "GeminiRun"}


def run_class(name):
    """The class for one local AI, looked up by name each time (a test may have put a stand-in in its place)."""
    return globals()[ENGINES.get(name, "CodexRun")]


def item_engine(root, record_id):
    """The local AI for one item: the one it was handed to, else the one chosen in settings."""
    from .thread import setting
    chosen = (setting(Path(root), "ai:for", None) or {}).get(record_id) if record_id else None
    return run_class(chosen) if chosen in ENGINES else engine(root)


def engine(root, purpose="work"):
    """Which local AI the built-in driver uses (设置 → AI 执行): one for doing items ("work"), one for sorting
    meetings and chats, judging new items and learning rules ("sort"; same as "work" unless chosen)."""
    from . import thread as conversation
    work = conversation.setting(Path(root), "engine", "codex")
    if work not in ENGINES:   # items done by Cindy: the built-in driver still sorts, with GPT unless chosen
        work = "codex"
    chosen = conversation.setting(Path(root), "engine_sort", work) if purpose == "sort" else work
    return run_class(chosen if chosen in ENGINES else work)   # sorting handed to Cindy: what the built-in driver still does itself uses the work AI


def test_engine(root, config, name):
    """Can this AI be reached right now? One tiny turn in a scratch folder: did it answer, how long, which model.
    The result is kept for the settings page."""
    from . import thread as conversation
    base = run_class(name)

    class Probe(base):
        raw = ()

        def _event(self, line, on_event):
            self.raw = (*self.raw, line)[-30:]
            super()._event(line, on_event)

    folder = scratch(root, "engine-test", name)
    (folder / ".last.txt").unlink(missing_ok=True)
    started = time.monotonic()
    model = {"claude": config.worker_claude_model or "Claude 账号默认", "gemini": config.worker_gemini_model or "Gemini 默认"}.get(name, config.worker_model)
    try:
        run = Probe(config, folder, effort="low", timeout_minutes=2, ephemeral=True).run("只回复两个字：连通。不要运行任何命令。")
        reply = (folder / ".last.txt").read_text(encoding="utf-8").strip() if (folder / ".last.txt").is_file() else ""
        problem = ""
        if not reply:
            said = [l for l in run.raw if l.strip()]
            errors = [l for l in said if any(w in l.lower() for w in ("error", "failed", "unauthorized", "401", "403", "not found", "invalid"))]
            problem = "超时了（2 分钟没回）" if run.timed_out else (errors or said or ["没有回复"])[-1][:300]
        ok = "连通" in reply
        if reply and not ok:  # it answered, but not the AI: e.g. "Not logged in · Please run /login"
            problem = reply[:300]
        if "no longer supported" in problem or "IneligibleTier" in problem or "GEMINI_API_KEY" in problem:
            problem = "Google 已不再让个人账号直接登录这个命令行。到 aistudio.google.com 申请一个 Gemini API Key，填到下面的「API Key」里再测。"
        elif "login" in problem.lower() or "logged in" in problem.lower() or "unauthorized" in problem.lower() or "401" in problem:
            problem += "（没登录：在终端运行一次这个命令行并登录）"
        result = {"ok": ok, "reply": reply[:60], "error": problem, "seconds": round(time.monotonic() - started, 1)}
    except MatrixError as exc:
        result = {"ok": False, "reply": "", "error": str(exc), "seconds": round(time.monotonic() - started, 1)}
    result.update(model=model, at=now().strftime("%m-%d %H:%M"), name=base.NAME)
    conversation.set_setting(Path(root), f"engine_test:{name}", result)
    return result
