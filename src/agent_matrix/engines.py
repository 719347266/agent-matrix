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
from .workenv import _env, claude_binary, codex_binary, now, provider_overrides, scratch


class CodexRun:
    """One `codex exec` turn in a folder, streamed so the panel can follow it."""
    THREAD_FILE = ".thread"
    NAME = "本机 GPT"

    def __init__(self, config, workdir, *, thread=None, effort=None, timeout_minutes=None, ephemeral=False, guard=None):
        self.config, self.workdir, self.thread = config, Path(workdir), thread
        self.guard = guard  # 开发模式 {"root", "mode", "record_id"}: only ClaudeRun can wait for the owner
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
        self.proc = proc
        try:
            try:
                self._send_prompt(proc, prompt)
            except BrokenPipeError:
                pass
            self._loop(proc, lines, time.monotonic() + self.timeout, on_event, on_tick, should_stop)
        finally:
            done.set()
            if watcher:
                watcher.join(timeout=2)
            self._finish()
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
            self._poll()
            if on_tick:
                on_tick()
            if time.monotonic() > deadline:
                self.timed_out = True
                self._kill_tree(proc)
                finished = True

    def _send_prompt(self, proc, prompt):
        proc.stdin.write(prompt)
        proc.stdin.close()

    def _poll(self):
        pass

    def _finish(self):
        pass

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
        settings = {**json.loads(self.SANDBOX), **({"disableAllHooks": True} if mcp else {})}
        allowed = ["--allowedTools", " ".join(["Bash Read Write Edit Glob Grep WebFetch WebSearch", *mcp])]
        if self.guard:
            # 开发模式: whatever would prompt comes to us over stdin/stdout (control_request can_use_tool) and waits
            # for the owner in 心响. "risky": the RISKY commands prompt; "every": nothing is pre-allowed.
            from .approvals import RISKY
            settings["sandbox"] = {**settings["sandbox"], "autoAllowBashIfSandboxed": False}
            if self.guard["mode"] == "every":
                allowed = []
            else:
                settings["permissions"] = {"ask": RISKY}
            permission = ["--permission-mode", "default", "--input-format", "stream-json", "--permission-prompt-tool", "stdio"]
        else:
            permission = ["--permission-mode", "dontAsk"]
        argv = [binary, "-p", *(["--setting-sources", "user,project"] if mcp else ["--safe-mode"]),
                "--output-format", "stream-json", "--verbose", *permission, *allowed,
                "--settings", json.dumps(settings),
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

    def _send_prompt(self, proc, prompt):
        if not self.guard:
            return super()._send_prompt(proc, prompt)
        # stdin stays open: the answers to permission requests go back this way; closed once the turn ends
        self.waiting = {}
        proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    def _reply(self, request_id, response):
        try:
            self.proc.stdin.write(json.dumps({"type": "control_response", "response": {
                "subtype": "success", "request_id": request_id, "response": response}}, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass

    def _ask_owner(self, event):
        from . import approvals
        request = event.get("request") or {}
        if request.get("subtype") != "can_use_tool":
            return self._reply(event.get("request_id"), {})
        root = self.guard["root"]
        approval_id = approvals.ask(root, self.guard.get("record_id"), request.get("tool_name"), request.get("input"),
                                    request.get("description"))
        self.waiting[approval_id] = (event.get("request_id"), request.get("input") or {})
        try:
            from . import thread as conversation
            conversation.add(root, self.guard.get("record_id"), "ai", "say",
                             f"【开发模式】等你批准：{request.get('tool_name')} {approvals.summary(request.get('tool_name'), request.get('input'))[:200]}",
                             driver=self.NAME)
        except Exception as exc:
            from . import activity
            activity.swallowed(root, "开发模式：在对话里提示等你批准", exc)

    def _poll(self):
        from . import approvals
        for approval_id, (request_id, tool_input) in list(getattr(self, "waiting", {}).items()):
            entry = approvals.get(self.guard["root"], approval_id)
            if not entry or entry["status"] == "pending":
                continue
            del self.waiting[approval_id]
            if entry["status"] == "allowed":
                self._reply(request_id, {"behavior": "allow", "updatedInput": tool_input})
            else:
                self._reply(request_id, {"behavior": "deny", "message": "本人没有批准这一步" + (f"：{entry['note']}" if entry.get("note") else "。")
                                         + "不要换个办法绕过去做同样的事；做不了就在结果里写明需要本人做什么。"})

    def _finish(self):
        from . import approvals
        for approval_id in getattr(self, "waiting", {}):
            approvals.expire(self.guard["root"], approval_id)
        self.waiting = {}

    def _event(self, line, on_event):
        try:
            event = json.loads(line)
        except ValueError:
            return
        kind = event.get("type")
        if kind == "control_request" and self.guard:
            return self._ask_owner(event)
        if kind == "result" and self.guard:
            try:
                self.proc.stdin.close()  # the turn is over: let claude exit
            except (BrokenPipeError, OSError):
                pass
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


ENGINES = {"codex": "CodexRun", "claude": "ClaudeRun"}


def engine(root, purpose="work"):
    """Which local AI the built-in driver uses (设置 → AI 执行): one for doing items ("work"), one for sorting
    meetings and chats, judging new items and learning rules ("sort"; same as "work" unless chosen)."""
    from . import thread as conversation
    work = conversation.setting(Path(root), "engine", "codex")
    if work not in ENGINES:   # items done by Cindy: the built-in driver still sorts, with GPT unless chosen
        work = "codex"
    chosen = conversation.setting(Path(root), "engine_sort", work) if purpose == "sort" else work
    return ClaudeRun if chosen == "claude" else CodexRun


def test_engine(root, config, name):
    """Can this AI be reached right now? One tiny turn in a scratch folder: did it answer, how long, which model.
    The result is kept for the settings page."""
    from . import thread as conversation
    base = ClaudeRun if name == "claude" else CodexRun

    class Probe(base):
        raw = ()

        def _event(self, line, on_event):
            self.raw = (*self.raw, line)[-30:]
            super()._event(line, on_event)

    folder = scratch(root, "engine-test", name)
    (folder / ".last.txt").unlink(missing_ok=True)
    started = time.monotonic()
    model = (config.worker_claude_model or "Claude 账号默认") if name == "claude" else config.worker_model
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
        if "login" in problem.lower() or "logged in" in problem.lower() or "unauthorized" in problem.lower() or "401" in problem:
            problem += "（没登录：在终端运行一次这个命令行并登录）"
        result = {"ok": ok, "reply": reply[:60], "error": problem, "seconds": round(time.monotonic() - started, 1)}
    except MatrixError as exc:
        result = {"ok": False, "reply": "", "error": str(exc), "seconds": round(time.monotonic() - started, 1)}
    result.update(model=model, at=now().strftime("%m-%d %H:%M"), name=base.NAME)
    conversation.set_setting(Path(root), f"engine_test:{name}", result)
    return result
