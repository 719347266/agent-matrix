"""Getting 心响 set up without the owner typing a single command.

The owner answers the few things only they know (in the page), then pastes one short message to the AI they
already use (Cindy, Claude Code, Codex, Cursor, Gemini …). That AI runs `xinxiang-setup doctor`, which says
what is still missing and exactly how to do each thing; it does them, asks the owner only for what a person
must do (sign in, approve in the browser), and runs doctor again until everything is ready.

    xinxiang-setup doctor                 what is ready, what is missing and how to do it
    xinxiang-setup set key=value ...      save a choice (the same checks as the settings page)
    xinxiang-setup test codex|claude|gemini   can this AI be reached right now
"""
import argparse
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

NAMES = {"cindy": "Cindy", "codex": "本机 GPT", "claude": "本机 Claude", "gemini": "本机 Gemini"}
INSTALL = {"codex": ("@openai/codex", "codex login", "会打开浏览器，请本人登录 ChatGPT 账号"),
           "claude": ("@anthropic-ai/claude-code", "claude", "第一次运行会让本人登录 Claude 账号；登录完输入 /exit 退出"),
           "gemini": ("@google/gemini-cli", "gemini", "第一次运行会让本人登录 Google 账号；个人账号登录不了的话，请本人到 心响 → 设置 → 谁来做事 里填一个 Gemini API Key")}
SETTABLE = {"owner_name", "owner_aliases", "engine", "engine_sort", "intake_mode", "brief_channel", "meeting_watch_enabled",
            "chat_watch_enabled", "feishu_tasks_enabled", "want_feishu", "want_slack", "want_wechat", "wechat_enabled", "cindy_project", "prework_enabled", "onboarded"}


from .setupshim import _cmd, install_shim, prompt, shim_path, work_cmd  # noqa: F401


def _local(name, job, found, tests, here, cmd):
    """One local AI: is it installed, signed in and answering; if not, how to get there."""
    label, (package, login, login_note) = NAMES[name], INSTALL[name]
    test = tests.get(name) or {}
    if found.get(name) and test.get("ok"):
        return {"ok": True, "say": f"{label} 已连通。"}
    steps = []
    if not found.get(name):
        if not here.get("npm"):
            steps.append("先装 Node.js（LTS 版）：" + ("Windows 上运行 `winget install OpenJS.NodeJS.LTS`" if here.get("platform") == "windows"
                         else "macOS 上有 Homebrew 就运行 `brew install node`，没有就到 https://nodejs.org 下载安装包") + "。装完重新打开终端。")
        steps.append(f"安装命令行：`npm install -g {package}`")
    steps.append(f"登录：运行 `{login}`（{login_note}）。")
    steps.append(f"测试：运行 `{cmd} test {name}`，返回 ok 才算好；没好就看它给的原因再处理。")
    why = f"这台电脑上还没有 {label}" if not found.get(name) else f"{label} 还没试通" + (f"：{test.get('error')}" if test.get("error") else "")
    return {"ok": False, "say": f"{why}，{job}。", "steps": steps}


def report(root):
    """Everything 心响 needs, each with whether it is ready and, when not, how to make it so."""
    from . import thread
    from .app import system_info
    from .config import load
    root = Path(root).resolve()
    cmd = _cmd(root)
    s = system_info(root)
    here, record, found = s.get("here") or {}, s.get("record") or {}, s.get("engines") or {}
    try:
        config = load(root / "config.yaml")
    except Exception:
        config = None
    engine = thread.setting(root, "engine", "codex")
    sort = thread.setting(root, "engine_sort", engine if engine != "cindy" else "codex")
    tests = {k: thread.setting(root, f"engine_test:{k}", None) for k in INSTALL}
    manual = bool(config and config.intake_mode == "manual")
    feishu, want_feishu = bool(record.get("feishu_ready")), bool(thread.setting(root, "want_feishu", False))
    slack, want_slack = bool(s.get("slack")), bool(thread.setting(root, "want_slack", False))
    wx, want_wechat = s.get("wechat") or {}, bool(thread.setting(root, "want_wechat", False))
    wechat_on = bool(wx.get("enabled") and wx.get("installed"))
    reading = not manual and (feishu or slack or wechat_on)
    python = here.get("python") or sys.executable
    out = []

    running = s.get("listener") == "运行中"
    out.append({"key": "listener", "title": "后台程序", "need": True, "ok": running,
                "say": "在运行。" if running else "没在运行，所以什么都不会自动发生。",
                "steps": [] if running else (["请本人从开始菜单（或桌面图标）重新打开「心响」，等一分钟。"] if here.get("client")
                          else [f"运行 `powershell -ExecutionPolicy Bypass -File \"{root}\\windows\\install.ps1\"`，等一分钟。"] if here.get("platform") == "windows"
                          else [f"运行 `\"{root}/launchd/install-listen.sh\"`（装好并开机自启），等一分钟。"])})

    def cindy(job):
        from .local_sessions import cindy_setup_text, cindy_status
        c = cindy_status(root)
        if c.get("ready") and c.get("same_project"):
            return {"ok": True, "say": "Cindy 每 5 分钟来领一次活。"}
        if not c.get("installed"):
            return {"ok": False, "say": f"这台电脑上没有装 Cindy，{job}。", "steps": [
                f"问本人：是装上 Cindy，还是换一个本机 AI 来做。换的话运行 `{cmd} set engine=codex`（或 claude、gemini），再运行一次 doctor。"]}
        return {"ok": False, "say": f"Cindy 的定时任务还没接好，{job}。", "steps": [
            "如果你就是 Cindy 里的 AI：直接照下面这段话做。否则请本人把这段话粘贴到 Cindy 里发送：\n" + cindy_setup_text(root)]}

    from . import aidrivers
    xin = work_cmd()
    takeover = (f"最省事的办法：你自己来当心响的 AI，不用装别的。运行 `{xin} hello --name \"<你的名字>\" --app \"<你所在的客户端>\" --model \"<你用的模型>\"`，"
                f"再运行 `{xin} next --driver \"<你的名字>\" --wait 600` 守着领活。做了这一步，这一项下面的步骤就不用做了。")

    def outside(name, job):
        """An AI that said hello: it does the work for as long as it keeps asking for it."""
        info = next((d for d in aidrivers.known(root) if d["name"] == name), None)
        if info and info["online"]:
            return {"ok": True, "say": f"{name} 在这儿，正在领活。"}
        return {"ok": False, "say": f"{name} 现在不在{('（上次来是 ' + info['seen'] + '）') if info and info['seen'] else ''}，{job}。", "steps": [
            f"如果你就是 {name}：运行 `{xin} next --driver \"{name}\" --wait 600`，接着守着领活。",
            f"如果你是另一个 AI：你来接手。运行 `{xin} hello --name \"<你的名字>\" --app \"<你所在的客户端>\" --model \"<你用的模型>\"`，再运行 `{xin} next --driver \"<你的名字>\" --wait 600`。"]}

    def who(chosen, job):
        name = aidrivers.name_in(chosen)
        if name:
            return outside(name, job), name
        if chosen == "cindy":
            return cindy(job), "Cindy"
        one = _local(chosen if chosen in INSTALL else "codex", job, found, tests, here, cmd)
        if not one["ok"]:
            one["steps"] = [takeover, *one.get("steps", [])]
        return one, NAMES.get(chosen, chosen)

    work, label = who(engine, "交给 AI 的事没人做")
    out.append({"key": "engine", "title": f"做事的 AI：{label}", "need": True, **work})
    if reading and sort != engine:
        sorting, label = who(sort, "会议和聊天里的事提不出来")
        out.append({"key": "sort", "title": f"整理消息的 AI：{label}", "need": True, **sorting})

    name = (config.owner_name if config else "") or ""
    out.append({"key": "me", "title": "怎么称呼本人", "need": bool(reading or want_feishu or want_slack or want_wechat), "ok": bool(name),
                "say": f"{name}。" if name else "还没填，分不清会议和聊天里哪些事是本人的。",
                "steps": [] if name else [f"问本人：同事在会议和聊天里怎么叫你（名字，和别的叫法）。然后运行 `{cmd} set owner_name=名字 owner_aliases=叫法1,叫法2`（没有别的叫法就不写 owner_aliases）。"]})

    broken = record.get("feishu_broken")
    if feishu and broken:
        out.append({"key": "feishu", "title": "飞书", "need": True, "ok": False,
                    "say": f"接着，但从 {broken.get('since') or '刚才'} 起读不到：{broken.get('why') or '原因不明'}。会议、聊天、飞书任务都收不进来。",
                    "steps": ["先看现在的登录状态：`lark-cli auth status --json --verify`。",
                              "重新登录：运行 `lark-cli auth login`，按它的提示做（会打开浏览器，请本人点同意）。",
                              "再运行一次 `lark-cli auth status --json --verify`，user 和 bot 两项都是 ready、verified 才算好。",
                              "等两分钟，再运行 doctor：心响下一次定时检查通过后，这一项会变成已就绪。"]})
    elif feishu:
        out.append({"key": "feishu", "title": "飞书", "need": False, "ok": True, "say": "已接入。"})
    elif want_feishu:
        run = (f'$env:PYTHONUTF8=1; cd "{root}"; & "{python}" -m ' if here.get("platform") == "windows" else f'cd "{root}" && "{python}" -m ')
        steps = [] if here.get("lark_cli") else [*([] if here.get("npm") else ["先装 Node.js（LTS 版），装完重新打开终端。"]), "安装飞书命令行：`npm install -g @larksuite/cli`"]
        steps += ["登录飞书：运行 `lark-cli auth login`，按它的提示做（会打开浏览器，请本人点同意；它要先配置应用的话照它的提示配）。",
                  f"建好存事项的表格（只做一次）：`{run}agent_matrix setup`",
                  f"补齐表格里的栏目：`{run}agent_matrix.brief_migration setup-fields`，然后 `{run}agent_matrix.brief_migration apply`",
                  f"打开要读的来源：`{cmd} set meeting_watch_enabled=true chat_watch_enabled=true feishu_tasks_enabled=true`"]
        out.append({"key": "feishu", "title": "飞书", "need": True, "ok": False, "say": "本人想接飞书，还没接好。", "steps": steps})
    else:
        out.append({"key": "feishu", "title": "飞书", "need": False, "ok": True,
                    "say": "本人没打算接（事项在心响里自己新建）。以后想接，在心响的引导或设置里打开「我要接飞书」。"})
    if slack:
        out.append({"key": "slack", "title": "Slack", "need": False, "ok": True, "say": "已接入。"})
    elif want_slack:
        out.append({"key": "slack", "title": "Slack", "need": True, "ok": False, "say": "本人想接 Slack，还没接好。", "steps": [
            "带本人打开 https://api.slack.com/apps ，点 Create New App → From scratch，选他自己的工作区。",
            "在 OAuth & Permissions → User Token Scopes 里加上这些权限：search:read channels:history groups:history im:history mpim:history channels:read groups:read im:read mpim:read users:read",
            "点 Install to Workspace，请本人点同意。页面上会出现一串 xoxp- 开头的 User OAuth Token。",
            "请本人自己把这串 Token 粘贴到 心响 → 设置 → 办公工具 → Slack 里，点「连接」。不要让他把 Token 发到对话里。"]})
    renew = "运行 `wechat-cli init` 提取微信的读取钥匙。这一步要本人在场：会让他输入电脑的开机密码，可能还要重新签名微信（先把这一点告诉本人，他同意再做）。"
    status = wx.get("status") or {}
    if wechat_on and status.get("ok") is False:
        out.append({"key": "wechat", "title": "微信", "need": True, "ok": False, "say": f"开着，但读不到：{status.get('error') or '原因不明'}。",
                    "steps": ["确认微信已经在这台电脑上打开并登录。", renew, "验证：`wechat-cli sessions --limit 3` 能列出聊天。"]})
    elif wechat_on and status.get("behind"):
        out.append({"key": "wechat", "title": "微信", "need": False, "ok": False,
                    "say": "已接入，但最近的聊天记录读不出来，现在只看得到每个聊天的最后一条消息。",
                    "steps": [renew, "验证：`wechat-cli history <某个群名> --limit 3` 能读到今天的消息。"]})
    elif wechat_on:
        out.append({"key": "wechat", "title": "微信", "need": False, "ok": True, "say": "已接入。"})
    elif want_wechat or wx.get("enabled"):
        steps = [] if wx.get("installed") else [*([] if here.get("npm") else ["先装 Node.js（LTS 版），装完重新打开终端。"]),
                                                  "安装微信命令行：`npm install -g @canghe_ai/wechat-cli`"]
        steps += ["确认微信已经在这台电脑上打开并登录。", renew, "验证：`wechat-cli sessions --limit 3` 能列出聊天。",
                  f"打开这个来源：`{cmd} set wechat_enabled=true`"]
        out.append({"key": "wechat", "title": "微信", "need": True, "ok": False, "say": "本人想接微信，还没接好。", "steps": steps})
    return {"ready": all(x["ok"] for x in out if x["need"]), "items": out, "cmd": cmd}


def text(rep):
    lines = ["心响现在的配置情况：", ""]
    for x in rep["items"]:
        mark = "已就绪" if x["ok"] else ("要处理" if x["need"] else "可选，没配")
        lines.append(f"【{mark}】{x['title']}：{x['say']}")
        for i, step in enumerate(x.get("steps") or [], 1):
            lines.append(f"    {i}. {step}")
    lines += ["", "全部就绪。用一句话告诉本人：心响已经配好，可以用了。" if rep["ready"]
              else f"还有没就绪的。按上面的步骤逐项处理，每做完一项就再运行一次 `{rep['cmd']} doctor`，直到全部就绪。"]
    return "\n".join(lines)


def _post(root, path, body, timeout=200):
    from .config import load
    port = load(Path(root) / "config.yaml").app_port
    token = (Path(root) / "state/app-token").read_text(encoding="utf-8").strip()
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json", "X-Xinxiang-Token": token})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except ValueError:
            return {"ok": False, "reply": f"心响返回 {exc.code}"}
    except (OSError, ValueError) as exc:
        return {"ok": False, "reply": f"连不上心响的后台程序（{type(exc).__name__}）。先让后台程序跑起来，再试。"}


def parse(pairs):
    """key=value words from the command line into what the settings page would send."""
    out = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        key = key.strip()
        if not sep or key not in SETTABLE:
            raise ValueError(f"不能设置 {key or pair}。可以设置的：{'、'.join(sorted(SETTABLE))}")
        value = value.strip()
        if key == "owner_aliases":
            out[key] = [v.strip() for v in value.replace("，", ",").replace("、", ",").split(",") if v.strip()]
        elif value.lower() in ("true", "false"):
            out[key] = value.lower() == "true"
        else:
            out[key] = value
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description="心响的配置检查（给 AI 用）")
    p.add_argument("command", choices=["doctor", "set", "test", "prompt"])
    p.add_argument("args", nargs="*")
    p.add_argument("--config", type=Path, default=Path("config.yaml"))
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    root = a.config.resolve().parent
    if a.command == "prompt":
        print(prompt(root))
        return 0
    if a.command == "doctor":
        rep = report(root)
        print(json.dumps(rep, ensure_ascii=False, indent=2) if a.json else text(rep))
        return 0 if rep["ready"] else 2
    if a.command == "set":
        try:
            body = parse(a.args)
        except ValueError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
        out = _post(root, "/api/settings", body) if body else {"ok": False, "reply": "没有要设置的内容。用法：set key=value"}
        print(json.dumps({"ok": bool(out.get("ok")), "saved": sorted(body) if out.get("ok") else [], **({} if out.get("ok") else {"error": out.get("reply") or out.get("error") or "没保存成功"})}, ensure_ascii=False))
        return 0 if out.get("ok") else 1
    name = (a.args or [""])[0]
    if name not in INSTALL:
        print(json.dumps({"ok": False, "error": "用法：test codex|claude|gemini"}, ensure_ascii=False))
        return 1
    out = _post(root, "/api/engine/test", {"engine": name})
    print(json.dumps({"ok": bool(out.get("ok")), **{k: out[k] for k in ("error", "seconds", "model", "reply") if out.get(k)}}, ensure_ascii=False))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
