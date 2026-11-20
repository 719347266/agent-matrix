"""The command an AI calls to set 心响 up (`xinxiang-setup`), and the one message the owner pastes to that AI."""
import os
from pathlib import Path
import sys


def shim_path():
    return Path.home() / ".agent-matrix" / "bin" / ("xinxiang-setup.cmd" if os.name == "nt" else "xinxiang-setup")


def install_shim(root):
    """A command with a plain path that any AI can call, wherever 心响 is installed."""
    path, python = shim_path(), sys.executable
    if os.name == "nt":
        python = python[:-len("pythonw.exe")] + "python.exe" if python.lower().endswith("pythonw.exe") else python
        body = f'@echo off\r\nset PYTHONUTF8=1\r\ncd /d "{Path(root).resolve()}"\r\n"{python}" -m agent_matrix.setupcheck %*\r\n'
    else:
        body = f'#!/bin/sh\ncd "{Path(root).resolve()}" || exit 1\nexec "{python}" -m agent_matrix.setupcheck "$@"\n'
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file() or path.read_text(encoding="utf-8") != body:
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)
    except OSError:
        return None
    return path


def _cmd(root):
    path = str(install_shim(root) or shim_path())
    return f'"{path}"' if " " in path else path


def work_cmd():
    """The command an AI asks for work with (drive.py installs it; the same plain path on every system)."""
    path = str(Path.home() / ".agent-matrix" / "bin" / ("xinxiang.cmd" if os.name == "nt" else "xinxiang"))
    return f'"{path}"' if " " in path else path


def prompt(root):
    """The one message the owner pastes to whichever AI they use. That AI says who it is, sets 心响 up, then does
    the work: 心响 never needs to be told in advance which AI it will be."""
    cmd, xin = _cmd(root), work_cmd()
    return f"""请你来当我电脑上「心响」的 AI，并先把它配好。心响是一个本机的待办工具：把要做的事收进来，让 AI 先做，做完交给我验收。

第一步，告诉心响你是谁（只做一次）。名字写你自己，比如 Antigravity、Gemini、Claude Code、Cursor、Cindy：

{xin} hello --name "<你的名字>" --app "<你所在的客户端>" --model "<你现在用的模型>"

第二步，检查配置。这条命令会告诉你现在缺什么、每一项该怎么做；照着逐项做，每做完一项再运行一次，直到它说全部就绪：

{cmd} doctor

第三步，开始干活。运行下面这条命令领活（名字和第一步一样）。没活时它会等着，有活立刻返回：

{xin} next --driver "<你的名字>" --wait 600

- 返回 {{"idle": true}} 就再运行一次，一直守着。
- 返回一件事（folder、instructions、job）：进入 folder，按 instructions 做，用里面写的命令报进度、交活，然后再领下一件。

请遵守：
1. 只做心响让你做的事，不要改别的东西。
2. 命令都由你来运行，不要让我自己去敲。
3. 只有人能做的事（登录账号、在浏览器里点同意、扫码、回答问题）才来找我：一次把要我做什么说清楚，然后等我做完。
4. 不要在对话里问我要密码、验证码或密钥。
5. 对外发送、审批、付款、删除这类事只准备材料，不要执行。
6. 配好后用一句话告诉我可以用了；这段对话要结束、不能再守着时也告诉我一声，我把这段话再发你一次就能接着干。"""
