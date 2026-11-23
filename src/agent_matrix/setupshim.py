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


TALK = """跟本人说话的方式（很重要，他多半不懂技术）：
- 语气温和、简短，像同事在帮他装软件。每开始一段，先用一句话说要做什么、为什么、大概多久。
- 检查、安装、配置都由你来做，不要把命令丢给他；要装软件时先说一声装什么、做什么用。
- 真要他亲手做时（登录、点授权、扫码、输电脑密码），一次只请他做一件事：说清在哪个窗口、点哪个按钮、看到什么就算好了，然后等他回你。
- 出错时不要贴报错原文：用一句话说清是怎么回事、接下来怎么办；是他电脑的问题（比如代理软件没开）就直说，不用他猜。
- 每接好一样告诉他一声，比如「飞书接好了，以后开完会，会上的事会自动收进来」。

进度（他在心响里看得到，你这边也要说）：
- 每做完一步：先运行领到的活里写的 step 命令，按「第几步/共几步 做了什么」报给心响（比如「2/5 装好飞书命令行」）；
  再在对话里用一句话告诉他：做完了什么、下一步做什么、大约还要多久。
- 报完**直接接着做下一步**，不要停下来等他回复、不要结束对话；只有要他亲手做事时才停下等他。"""


def work_cmd():
    """The command an AI asks for work with (drive.py installs it; the same plain path on every system)."""
    path = str(Path.home() / ".agent-matrix" / "bin" / ("xinxiang.cmd" if os.name == "nt" else "xinxiang"))
    return f'"{path}"' if " " in path else path


def rounds(xin, name='<你的名字>'):
    """How an AI works for 心响: it comes by on a schedule, a check in code decides whether to wake it, it does what
    is there and ends the round. It never stays on the line waiting: a subscription chat cannot hold a conversation
    open for hours, and one that times out leaves nobody doing the work."""
    return f"""不要一直挂着等活：每隔一段时间来一趟，有活才做，做完这一轮就结束。
- 你所在的客户端能建定时任务（比如 Cindy 的定时任务、Claude 的定时任务、Codex 的自动化）：建一个每 5 分钟一次的定时任务，叫「心响｜{name} 领活」。
  · 能设前置检查（运行前先跑的命令）就设成：{xin} gate --driver "{name}"。退出码 0 = 有活，开始这一轮；2 = 没活，跳过这一轮，不叫醒你、不花 token。
  · 设不了前置检查：每一轮第一步先运行这条 gate，退出码是 2 就直接结束这一轮。
  · 每一轮要做的：运行 {xin} next --driver "{name}" 领一件活，按它说的做完、交了，再领下一件；返回 {{"idle": true}} 就结束这一轮。
- 你的客户端建不了定时任务：现在把能领的活领完（同上，领到 idle 为止）就结束；以后心响有活会提醒我，我再叫你。
- 不要用 --wait 守着，也不要没活时反复运行 next。"""


def prompt(root):
    """The one message the owner pastes to whichever AI they use. That AI says who it is, sets 心响 up, then does
    the work: 心响 never needs to be told in advance which AI it will be."""
    cmd, xin = _cmd(root), work_cmd()
    return f"""请你来当我电脑上「心响」的 AI，并先把它配好。心响是一个本机的待办工具：把要做的事收进来，让 AI 先做，做完交给我验收。

第一步，告诉心响你是谁（只做一次）。名字写你自己，比如 Antigravity、Gemini、Claude Code、Cursor、Cindy：

{xin} hello --name "<你的名字>" --app "<你所在的客户端>" --model "<你现在用的模型>"

第二步，检查配置。这条命令会先查这台电脑缺什么（后台程序、Node.js、飞书命令行、网络代理、权限……），并告诉你每一项该怎么做；缺的你按步骤先装好、配好，每做完一项再运行一次，直到它说全部就绪：

{cmd} doctor

第三步，开始干活。他在心响里选了要接的工具、同意你认识他的话，你领到的头几件活就是「接入飞书/微信/Slack」和「认识你」，按顺序一件件做，每一步都报进度。之后心响里所有要 AI 做的事都交给你：做事项、整理会议和聊天、判断新进来的事。心响自己的后台程序负责定时去收消息和会议，收到要动脑的活就排队等你来领。现在先运行一次下面这条命令领活（名字和第一步一样）：

{xin} next --driver "<你的名字>"

- 返回一件事（folder、instructions、job）：进入 folder，按 instructions 做，用里面写的命令报进度、交活，然后再领下一件。
- 返回 {{"idle": true}}：现在没活了。

第四步，以后怎么来领活：
{rounds(xin)}

请遵守：
1. 只做心响让你做的事，不要改别的东西。
2. 命令都由你来运行，不要让我自己去敲。
3. 只有人能做的事（登录账号、在浏览器里点同意、扫码、回答问题）才来找我：一次把要我做什么说清楚，然后等我做完。
4. 不要在对话里问我要密码、验证码或密钥。
5. 对外发送、审批、付款、删除这类事只准备材料，不要执行。
6. 配好后用一句话告诉我可以用了，并说清你建好了定时任务，还是要我有活时再叫你。

""" + TALK


PARTS = {"work": ("做事", "交给 AI 的事项：找背景、写成果、按我的意见改"),
         "sort": ("整理", "从会议和聊天里找出我的待办、判断新进来的事")}


def part_prompt(part):
    """A message for a second AI that takes over one part of the work; the rest stays with the AI that has it."""
    xin, (name, what) = work_cmd(), PARTS[part]
    return f"""请你来接手我电脑上「心响」里的「{name}」：{what}。心响是本机的待办工具，已经配好了；别的部分还由原来的 AI 做，你只管这一块。

1. 告诉心响你是谁，并接下「{name}」（名字写你自己，比如 Antigravity、Gemini、Claude Code、Cursor）：
{xin} hello --name "<你的名字>" --app "<你所在的客户端>" --model "<你现在用的模型>" --part {part}
2. 现在先领一次活（名字和上面一样）：{xin} next --driver "<你的名字>"
- 返回一件事（folder、instructions、job）：进入 folder，按 instructions 做，用里面写的命令报进度、交活，然后再领下一件；返回 {{"idle": true}} 就是现在没活了。
3. 以后怎么来领活：
{rounds(xin)}

请遵守：命令都由你来运行，不要让我自己去敲；不要在对话里问我要密码、验证码或密钥；对外发送、审批、付款、删除只准备材料，不要执行；做完后告诉我你建好了定时任务，还是要我有活时再叫你。"""
