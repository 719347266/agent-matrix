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


def prompt(root):
    """The one message the owner pastes to the AI they already use."""
    cmd = _cmd(root)
    return f"""请帮我把电脑上的「心响」配置好。心响是一个本机的待办工具：把要做的事收进来，让 AI 先做，做完交给我验收。

先运行这条命令，它会告诉你现在缺什么、每一项该怎么做：

{cmd} doctor

然后照它列的步骤逐项做，每做完一项就再运行一次这条命令，直到它说全部就绪。

请遵守：
1. 只做它列出来的事，不要改别的东西。
2. 命令都由你来运行，不要让我自己去敲。
3. 只有人能做的事（登录账号、在浏览器里点同意、扫码、回答问题）才来找我：一次把要我做什么说清楚，然后等我做完。
4. 不要在对话里问我要密码、验证码或密钥。
5. 全部就绪后，用一句话告诉我可以用了。"""
