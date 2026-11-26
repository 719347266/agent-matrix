"""心响驱动协议：任何 AI 都能接入，领活 → 干活 → 交活。没有限制，谁能运行命令谁就能驱动。

    xinxiang next    --driver "Claude Code"              领下一件活（JSON）；没有活时返回 {"idle": true}
    xinxiang step    --job ID --text "2/4 查文档"         报进度，心响里实时显示
    xinxiang say     --job ID --text "……"                 在这件事的对话里说一句（给本人看）
    xinxiang submit  --job ID                             交活：程序自检；没过返回 problem，改完再交
    xinxiang ask     --job ID --question "…" --tried "…"  卡住了：问本人一个具体问题（只用于事项）
    xinxiang release --job ID                             不做了，放回队列
    xinxiang guide                                        给 AI 看的接入说明

Four kinds of work, in this order: judging new items (when the owner turned it on), extracting todos
from a meeting, extracting todos from chats, doing one AI item. Each job has its own folder, plain
instructions and an output file; code does everything else (checks, Feishu document, table, notice).

The conversation of every item lives in 心响 (thread.py), not in the AI that did the work, so any
driver picks up where the previous one stopped: `next` hands over the history and the owner's latest
words. The built-in driver (本机 GPT, worker.py) uses exactly these functions.
"""
import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from uuid import uuid4

from . import activity
from .workenv import shim_path  # noqa: F401
from . import promptfile
from .jobstore import now, _db, _save, get, open_jobs, drivers, stop_record  # noqa: F401
from . import db as database
from .models import MatrixBusy, MatrixError
from .timeutil import TZ

ITEM_LEASE_MINUTES = 120
OTHER_LEASE_MINUTES = 30
MAX_FIX_ROUNDS = 2
ITEM_FIX_ROUNDS = 3   # an item gets one more: one of its rounds may be the review (specs.review_request)




# ---- the job table (state/xinxiang.db) ------------------------------------------------------------------













STOPPED = {"ok": False, "status": "cancelled", "note": "本人已经把这件事标为不做或已完成：停下，不用再做、也不用交了。"}


def _closed(job):
    if job["status"] == "cancelled":
        if str(job.get("step") or "").startswith("本人把这件事交给了"):
            return {**STOPPED, "note": f"{job['step']}：停下，不用再做、也不用交了。"}
        return dict(STOPPED)
    if job["status"] != "open":
        return {"ok": True, "status": job["status"], "note": "这件活已经结束了。"}
    return None


def _busy(root, kind):
    return any(j["kind"] == kind for j in open_jobs(root))


def _lease(minutes):
    return (now() + timedelta(minutes=minutes)).isoformat(timespec="seconds")


# ---- how an outside AI calls back --------------------------------------------------------------------



def install_shim(root):
    """A command with a plain path any AI can call (the project path may contain Chinese)."""
    path = shim_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            body = f'@echo off\r\nset PYTHONUTF8=1\r\ncd /d "{Path(root).resolve()}"\r\n"{sys.executable}" -m agent_matrix.drive %*\r\n'
        else:
            body = f'#!/bin/sh\ncd "{Path(root).resolve()}" || exit 1\nexec "{sys.executable}" -m agent_matrix.drive "$@"\n'
        if not path.is_file() or path.read_text() != body:
            path.write_text(body)
            path.chmod(0o755)
    except OSError:
        return None
    return path


def _cmd():
    return str(shim_path())


def protocol_tail(job):
    c, j = _cmd(), job["id"]
    lines = ["", "## 交活（心响驱动协议）",
             f"- 进度：`{c} step --job {j} --text \"2/4 查文档\"`（15 字内，心响里实时显示）",
             f"- 做完：`{c} submit --job {j}`。返回 `\"status\": \"fix\"` 就按 problem 改好再交；返回 done 就结束。"]
    if job["kind"] == "setup":
        lines += [f"- 要本人亲手做的事（登录、点同意、扫码、输电脑密码）：`{c} say --job {j} --text \"一句话说清要他做什么\"`，"
                  "心响里会显示给他看；在你自己的对话里也告诉他，等他做完再继续。"]
    if job["kind"] == "item":
        lines += [f"- 想对本人说的话：`{c} say --job {j} --text \"……\"`（会记进这件事的对话）",
                  f"- 卡住：`{c} ask --job {j} --question \"问本人的一个具体问题\" --tried \"已经试过什么\"`",
                  f"- 这件事更适合另一个 AI 做：`{c} handoff --job {j} --to \"它的名字\" --why \"为什么\"`。只交这一件，别的事还是你做；"
                  "返回里有 message 就把它原样发给那个 AI（你发不了就告诉本人去发），它会接着你的进度做。"]
    lines += [f"- 做不了：`{c} release --job {j}`，放回队列。",
              "- 任何调用返回 cancelled：本人已叫停，立即结束本轮和已启动的命令，不再写入或提交这件事。",
              "然后再运行一次 next 领下一件，直到返回 idle。"]
    return "\n".join(lines)


# ---- preparing each kind of job ----------------------------------------------------------------------

def _prepare_triage(root, config, driver):
    from . import thread as conversation
    from .itemwork import _pending_new
    from .workenv import project, scratch
    if not conversation.setting(root, "auto_assign", False) or _busy(root, "triage"):
        return None
    from .workflow import Gateway
    with project(root, config) as (client, state):
        rows = Gateway(client, state).rows()
    items = _pending_new(rows, conversation.judgments(root))
    if not items:
        return None
    # What happened since: the owner may already have done it in a chat, or it was called off.
    from .background import after_context
    by_id = {r["record_id"]: r for r in rows}
    for i in items:
        i["after"] = after_context(client, by_id[i["record_id"]], now())
    others, numbers = _triage_others(root, rows)
    for i in items:   # which version the new item is in (预计会遇到的事 go there); "" when it is in none
        i["version"] = next((o["version"] for o in others if o["n"] == i["n"]), "")
    stamp = now().strftime("%Y%m%d%H%M%S")
    folder = scratch(root, "triage", stamp)
    (folder / "items.json").write_text(json.dumps([{k: v for k, v in i.items() if k != "record_id"} for i in items],
                                                 ensure_ascii=False, indent=1), encoding="utf-8")
    (folder / "open.json").write_text(json.dumps(others, ensure_ascii=False, indent=1), encoding="utf-8")
    from . import versions as version_store   # the versions a new item may belong to
    (folder / "versions.json").write_text(json.dumps([{"project": v["project"], "version": v["name"], "status": v["status_label"], "note": v["note"]}
                                                      for v in version_store.all(root) if v["status"] != "done"], ensure_ascii=False, indent=1), encoding="utf-8")
    return {"id": f"triage-{stamp}", "kind": "triage", "title": f"判断新进来的 {len(items)} 件事", "folder": str(folder),
            "ref": {"items": items, "numbers": numbers}, "output": ["triage.json"], "lease_until": _lease(OTHER_LEASE_MINUTES),
            "instructions": promptfile.read(root, "triage.md")}


def _prepare_donecheck(root, config, driver):
    """The owner's own open items, with what was said after each: has the owner already done it? (donecheck)"""
    from . import donecheck
    from .workenv import project, scratch
    if _busy(root, "donecheck"):
        return None
    from .snapshot import _snapshot
    if not donecheck.due(root, _snapshot(root).get("rows") or [], now()):   # cheap, local: most rounds stop here
        return None
    from .workflow import Gateway
    with project(root, config) as (client, state):
        rows = Gateway(client, state).rows()
    picked = donecheck.due(root, rows, now())
    if not picked:
        return None
    from .background import after_context
    one = lambda v: (v[0] if isinstance(v, list) and v else v) or ""
    items = []
    for r in picked:
        try:
            n = int(str(r.get("编号")).strip())
        except (TypeError, ValueError):
            continue
        items.append({"n": n, "record_id": r["record_id"], "title": str(one(r.get("标题")))[:80],
                      "what": str(one(r.get("下一步动作")))[:300], "done_when": str(one(r.get("完成标准")))[:120],
                      "quote": str(one(r.get("原话引用")))[:300], "source": one(r.get("来源")), "since": str(r.get("创建时间") or "")[:16],
                      "after": after_context(client, r, now())})
    if not items:
        return None
    stamp = now().strftime("%Y%m%d%H%M%S")
    folder = scratch(root, "donecheck", stamp)
    (folder / "items.json").write_text(json.dumps([{k: v for k, v in i.items() if k != "record_id"} for i in items],
                                                 ensure_ascii=False, indent=1), encoding="utf-8")
    return {"id": f"donecheck-{stamp}", "kind": "donecheck", "title": f"看你的 {len(items)} 件事办完没有", "folder": str(folder),
            "ref": {"items": items}, "output": ["donecheck.json"], "lease_until": _lease(OTHER_LEASE_MINUTES),
            "instructions": promptfile.read(root, "donecheck.md")}


def _submit_donecheck(root, job):
    from . import activity, donecheck, thread as conversation
    try:
        verdicts = json.loads((Path(job["folder"]) / "donecheck.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        job["rounds"] += 1
        _save(root, job)
        return {"ok": False, "status": "fix", "problem": "没有找到有效的 donecheck.json（格式见说明里的例子）。"}
    by_n = {i["n"]: i for i in job["ref"]["items"]}
    found = []
    for v in verdicts if isinstance(verdicts, list) else []:
        item = by_n.get(v.get("n") if isinstance(v, dict) else None)
        if not item:
            continue
        hint = donecheck.accept(root, item, v, now())
        if hint:
            conversation.add(root, item["record_id"], "ai", "say", f"看起来已经办完了：{hint['evidence']}"
                             + (f"\n出处：{hint['where']}" if hint["where"] else "") + (f"\n{hint['url']}" if hint["url"] else "")
                             + "\n点「完成」关掉；没办完就点「没办完」。", driver=job.get("driver"))
            found.append(item["n"])
    donecheck.mark_checked(root, [i["record_id"] for i in job["ref"]["items"]], now())
    activity.log(root, "detect", f"看了你的 {len(by_n)} 件事办完没有" + (f"：#{'、#'.join(map(str, found))} 看起来办完了" if found else "：都还没办完"))
    _close(root, job, "done")
    return {"ok": True, "status": "done", "done": found}


TRIAGE_OTHERS = 200


def _triage_others(root, rows):
    """The owner's items that are not finished (newest last), for the AI to see what a new item comes after or
    leads to: [{n, title, status, version}], and {"n": record_id} to check the numbers it gives back."""
    from . import versions
    one = lambda v: (v[0] if isinstance(v, list) and v else v) or ""
    try:
        of = {x["rid"]: f"{v['project']} · {v['name']}" for v in versions.all(root) for x in v["items"]}
    except Exception as exc:
        activity.swallowed(root, "drive._triage_others", exc)
        of = {}
    out, numbers = [], {}
    for r in rows:
        try:
            n = int(str(r.get("编号")).strip())
        except (TypeError, ValueError):
            continue
        if one(r.get("状态")) in ("完成", "取消") or r.get("排除本人提醒"):
            continue
        out.append({"n": n, "title": str(one(r.get("标题")))[:60], "status": one(r.get("状态")), "version": of.get(r["record_id"], "")})
        numbers[str(n)] = r["record_id"]
    out = out[-TRIAGE_OTHERS:]
    return out, {str(o["n"]): numbers[str(o["n"])] for o in out}


def _numbers(value):
    """[12, "#15", "16"] / 12 / "12、15" → ["12", "15", "16"]."""
    if isinstance(value, (int, str)):
        value = re.findall(r"\d{1,6}", str(value))
    return [str(x).strip().lstrip("#") for x in value or [] if isinstance(x, (int, str)) and str(x).strip().lstrip("#").isdigit()]


def _triage_relations(root, job, item, verdict):
    """What the AI proposed next to its verdict: which items come before / after this one (linked by="AI") and what
    the item's version is expected to bring. Only known numbers, never the item itself; the item must be in a
    version for expected things. A wrong guess is dropped quietly: it must not hold up the verdict.
    Returns (links made, expected written)."""
    from . import itemlinks, versions
    from .models import MatrixError
    numbers = dict(job["ref"].get("numbers") or {})   # older jobs carry none: only the new items themselves
    numbers.update({str(i["n"]): i["record_id"] for i in job["ref"]["items"]})
    me, links, expected = item["record_id"], 0, 0
    put = verdict.get("version")   # a new item not in a version yet: the one it belongs to, if it is one that exists
    if isinstance(put, dict) and not versions.of(root, me):
        found = next((v for v in versions.all(root) if v["project"] == put.get("project") and v["name"] == str(put.get("version") or "")), None)
        if found:
            versions.place(root, me, found["id"], by="AI")
    for key, mine_is_later in (("before", True), ("after", False)):
        for n in _numbers(verdict.get(key))[:5]:
            other = numbers.get(n)
            if not other or other == me:
                continue
            try:
                itemlinks.link(root, *((other, me) if mine_is_later else (me, other)), by="AI")
                links += 1
            except MatrixError:
                pass
    proposed = verdict.get("expected") or []
    version = versions.of(root, me) if proposed else None
    for e in (proposed if isinstance(proposed, list) else [])[:5]:
        e = {"title": e} if isinstance(e, str) else e
        if version and isinstance(e, dict) and str(e.get("title") or "").strip():
            versions.expect(root, version["id"], e["title"], e.get("why", ""), e.get("due", ""))
            expected += 1
    return links, expected


LEARN_MIN_NEW, LEARN_EVERY_HOURS = 3, 24
LEARN_RETRY_MINUTES = (30, 120, 1440)


def _prepare_setup(root, config, driver):
    """The owner clicked 接入飞书 / Slack / 微信 (or asked for the rest to be set up): the AI that drives 心响 does it."""
    from . import setupasks
    from .setupshim import TALK, _cmd as setup_cmd
    from .workenv import scratch
    if _busy(root, "setup"):
        return None
    asks = setupasks.pending(root)
    if not asks:
        return None
    what = next(iter(asks))
    name = setupasks.WHAT[what]
    only = "" if what == "fix" else f" --only {what}"
    stamp = now().strftime("%Y%m%d%H%M%S")
    title = "把心响剩下的配好" if what == "fix" else f"接入 {name}" if name.isascii() else f"接入{name}"
    doctor = f"{setup_cmd(root)} doctor{only}"
    return {"id": f"setup-{what}-{stamp}", "kind": "setup", "title": title, "folder": str(scratch(root, "setup", stamp)),
            "ref": {"what": what}, "output": [], "lease_until": _lease(OTHER_LEASE_MINUTES),
            "instructions": f"""# {title}

本人在心响里点了「{title}」，要你来把它配好。你就是驱动心响的 AI，命令都由你来运行。

1. 运行 `{doctor}`。它会列出还缺什么、每一步怎么做；照着逐项做，每做完一步再运行一次，直到这一项就绪。
2. 只有人能做的事（在浏览器里登录、点同意、扫码、输电脑密码）才找本人：用下面的 say 命令说清要他做什么，然后等他做完再继续。
3. 先把要做的事拆成几步（看 doctor 列出的缺项），每做完一步就用 step 报「第几步/共几步 做了什么」（比如「2/4 登录飞书」，15 字内），本人在心响里看得到进度条；报完接着做，不要停下等他。
4. 做完运行 submit。心响会自己再查一遍：真接好了才算完成；没好会告诉你还差什么，照着继续。

{TALK}

请遵守：
- 不要在对话里问本人要密码、验证码、Token 或密钥。要填 Token 的（比如 Slack），请本人自己粘贴到心响的设置里。
- 改系统设置、重新签名应用这类会影响别的软件的事，先说清楚影响，本人同意了再做。
- 只做配置这一件事，不要改别的东西。"""}


SCAN_STEPS = {
    "feishu": """### 飞书近 30 天（全部用 `--as user`，只读）
- 他自己写的和常看的文档：`lark-cli docs +search --query "<产品名/项目名/周报/方案>" --page-size 20 --as user --format json`，挑他是作者或最近编辑的几篇，用 `lark-cli docs +fetch --doc <链接> --doc-format markdown --as user` 看结构和写法（只看开头和小标题）。
- 常在的群：`lark-cli im +chat-list --as user --format json`，看群名就能知道在做哪些项目、和谁合作。
- 会议：`lark-cli vc --help`、`lark-cli minutes --help` 找最近的会议和纪要，看主题和结论。
- 任务：`lark-cli task +get-my-tasks --complete=false --as user --format json`。
- 不清楚某个命令怎么用就加 `--help`。""",
    "folders": """### 本机项目文件夹（只看名字和说明，不看内容细节）
- 列出 桌面、文稿、下载 以及 ~/Projects、~/code、~/work 这类目录下两层以内的文件夹名。
- 找代码仓库（有 `.git` 的文件夹）：看 README 的前 20 行、package.json / pyproject.toml / go.mod 这类清单，知道每个仓库是什么、用什么语言。
- 看文件夹里最近改过的文档类型（.docx/.pptx/.xlsx/.md/.fig），推断他常产出什么。不要打开个人照片、证件、财务文件。""",
    "wechat": """### 微信近期聊天（只在本机读）
- `wechat-cli sessions --limit 40` 看最近的会话；只挑明显是工作的群和常聊工作的人。
- 对这几个会话：`wechat-cli history "<会话名>" --limit 60`，只归纳在推进什么事、他在里面是什么角色。
- 家人、朋友、闲聊群一律跳过，不写进画像。
- 没装 wechat-cli 或读不到：跳过这一项，在画像里说一声即可，不要为此去配置微信。""",
    "ai_memory": """### 他在别的 AI 里留下的说明和记忆（只读）
- Claude：`~/.claude/CLAUDE.md`、`~/.claude/projects/*/memory/*.md`。
- Codex：`~/.codex/AGENTS.md`、`~/.codex/memories/` 下的文件。
- Cindy：`~/Library/Application Support/Cindy` 下各工作区里的 CLAUDE.md / AGENTS.md（Windows 在 `%APPDATA%\\Cindy`）。
- 从这些里提取他的岗位、项目、偏好和规矩；里面的 Token、密钥、账号一律不写。""",
}


def _prepare_profile(root, config, driver):
    """The owner ticked where the AI may look to get to know them: it writes a draft of 关于你 for the owner to keep."""
    from . import memory
    from .promptfile import read
    from .workenv import scratch
    asked = memory.scan_asked(root)
    if not asked or _busy(root, "profile"):
        return None
    sources = [s for s in asked.get("sources") or [] if s in SCAN_STEPS]
    stamp = now().strftime("%Y%m%d%H%M%S%f")
    profile = memory.profile(root)
    current = profile["text"].strip()
    folder = scratch(root, "profile", stamp)
    if current:
        from .thread import setting
        checked = setting(root, "profile_checked_sources", {})
        since = "；".join(f"{memory.SCAN_SOURCES[s]}自 {checked.get(s, profile['modified'])}" for s in sources)
        output = "profile-updates.json"
        # Incremental scans never inherit the initial 30-day discovery checklist.
        text = read(root, "profile_update.md").replace("{current}", current).replace(
            "{sources}", "、".join(memory.SCAN_SOURCES[s] for s in sources)).replace(
            "{since}", since)
        title = "核对「关于你」的变化"
    else:
        output = "画像.md"
        text = read(root, "profile_scan.md").format(sources="、".join(memory.SCAN_SOURCES[s] for s in sources),
                   current="（还没有，本人没写过）", steps="\n\n".join(SCAN_STEPS[s] for s in sources))
        title = "认识你：写一份「关于你」"
    return {"id": f"profile-{stamp}", "kind": "profile", "title": title, "folder": str(folder),
            "ref": {"sources": sources, "incremental": bool(current), "requested_at": asked.get("at")},
            "output": [output], "lease_until": _lease(ITEM_LEASE_MINUTES), "instructions": text}


def _submit_profile(root, job):
    from . import inbox, memory
    request = job["ref"].get("requested_at")
    if request and (memory.scan_asked(root) or {}).get("at") != request:
        _close(root, job, "done")
        return {"ok": True, "status": "done", "proposed": 0}
    if job["ref"].get("incremental"):
        from . import tidy
        from .thread import set_setting, setting
        try:
            plan = json.loads((Path(job["folder"]) / "profile-updates.json").read_text(encoding="utf-8"))
            changes = plan["changes"]
            if not isinstance(changes, list) or len(changes) > tidy.MAX_PROFILE or any(
                    not isinstance(c, dict) or c.get("type") != "profile" for c in changes):
                raise ValueError
            proposals, problems = tidy.check(root, plan, rows=[])
        except (OSError, ValueError, KeyError, TypeError):
            proposals, problems = [], ["profile-updates.json 应包含 changes 数组，最多两项关于你的局部改动。"]
        if problems:
            job["rounds"] += 1
            if job["rounds"] > MAX_FIX_ROUNDS:
                return fail(root, job, problems[0])
            _save(root, job)
            return {"ok": False, "status": "fix", "problem": "；".join(problems)}
        added = tidy.save(root, proposals, job["id"])
        checked = setting(root, "profile_checked_sources", {})
        checked.update(dict.fromkeys(job["ref"]["sources"], request))
        set_setting(root, "profile_checked_sources", checked)
        if (memory.scan_asked(root) or {}).get("at") == job["ref"].get("requested_at"):
            memory.cancel_scan(root)
        _close(root, job, "done")
        if added:
            try:
                inbox.add(root, "profile-update:" + job["id"], f"「关于你」有 {added} 处变化待确认，可在记忆更新里逐条查看。", kind="system")
            except Exception as exc:
                activity.swallowed(root, "drive._submit_profile", exc)
        return {"ok": True, "status": "done", "proposed": added}
    path = Path(job["folder"]) / "画像.md"
    text = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    sections = [x for x in text.split("\n## ")[1:] if x.split("\n", 1)[-1].strip()]
    problem = ("没找到 画像.md" if not text else "画像太短，至少要写出岗位、在做的事、交付习惯这几节" if len(text) < 200 or len(sections) < 4
               else "画像太长，压到 6000 字以内，只写结论" if len(text) > memory.PROFILE_MAX
               else "里面还有占位文字（TODO/待补充/XX），写实或写「还看不出来」" if any(w in text for w in ("TODO", "待补充", "XX")) else "")
    if problem:
        job["rounds"] += 1
        if job["rounds"] > MAX_FIX_ROUNDS + 2:
            return fail(root, job, problem)
        _save(root, job)
        return {"ok": False, "status": "fix", "problem": problem}
    if memory.profile(root)["text"].strip():
        memory.cancel_scan(root)
        _close(root, job, "done")
        return {"ok": True, "status": "done", "proposed": 0}
    memory.save_draft(root, text, by=job.get("driver") or "AI")
    _close(root, job, "done")
    activity.log(root, "learn", "认识你：「关于你」草稿写好了", detail=job.get("driver") or "")
    try:
        inbox.add(root, f"profile-draft:{job['id']}", f"✓ {job.get('driver') or 'AI'} 写好了一份「关于你」的草稿，到「记忆」里看看，对的话点「用这份」。", kind="system")
    except Exception as exc:
        activity.swallowed(root, "drive._submit_profile", exc)
    return {"ok": True, "status": "done"}


def _prepare_organize(root, config, driver):
    """The owner asked for the work to be sorted into project → version, with what comes before, after and next."""
    from . import itemlinks, memory, projects, versions
    from .snapshot import _snapshot
    from .workenv import scratch
    if not versions.organize_asked(root) or _busy(root, "organize"):
        return None
    rows = _snapshot(root).get("rows") or []
    placed = projects.place(root, rows)
    names = {p["id"]: p["name"] for p in projects.all(root)}
    n_of = {r["record_id"]: r.get("编号") for r in rows}
    one = lambda v: (v[0] if isinstance(v, list) and v else v) or ""
    items = [{"n": r.get("编号"), "title": r.get("标题"), "status": one(r.get("状态")), "who": one(r.get("执行方")),
              "project": names.get((placed.get(r["record_id"]) or (None,))[0], ""),
              "version": (versions.of(root, r["record_id"]) or {}).get("name", ""),
              "before": [n_of.get(x) for x in itemlinks.upstream(root, r["record_id"], 3) if n_of.get(x)]}
             for r in rows if r.get("编号")][-300:]
    stamp = now().strftime("%Y%m%d%H%M%S")
    folder = scratch(root, "organize", stamp)
    (folder / "items.json").write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    (folder / "projects.json").write_text(json.dumps([{"project": v["project"], "version": v["name"], "status": v["status_label"]}
                                                      for v in versions.all(root)] or [{"projects": list(names.values())}], ensure_ascii=False, indent=1), encoding="utf-8")
    (folder / "profile.md").write_text(memory.profile(root)["text"] or "（还没有）", encoding="utf-8")
    return {"id": f"organize-{stamp}", "kind": "organize", "title": "整理项目和版本", "folder": str(folder), "ref": {},
            "output": ["organize.json"], "lease_until": _lease(ITEM_LEASE_MINUTES), "instructions": promptfile.read(root, "organize.md")}


def _submit_organize(root, job):
    from . import inbox, itemlinks, versions
    from .snapshot import _snapshot
    try:
        plan = json.loads((Path(job["folder"]) / "organize.json").read_text(encoding="utf-8"))
        assert isinstance(plan, dict)
    except (OSError, ValueError, AssertionError):
        job["rounds"] += 1
        _save(root, job)
        return {"ok": False, "status": "fix", "problem": "没有找到有效的 organize.json（格式见说明里的例子）。"}
    rid_of = {str(r.get("编号")): r["record_id"] for r in _snapshot(root).get("rows") or [] if r.get("编号")}
    vid, counts = {}, {"versions": 0, "items": 0, "links": 0, "expected": 0}
    for v in plan.get("versions") or []:
        if isinstance(v, dict) and v.get("project") and v.get("name"):
            vid[(v["project"], str(v["name"]))] = versions.upsert(root, str(v["project"]), str(v["name"]), v.get("status"), v.get("period", ""),
                                                                  v.get("note", ""), source=str(v.get("source") or "AI"))
            counts["versions"] += 1
    def version_id(project, name):
        key = (project, str(name))
        if key not in vid and project and name:
            vid[key] = versions.upsert(root, str(project), str(name))
        return vid.get(key)
    for it in plan.get("items") or []:
        if isinstance(it, dict) and str(it.get("n")) in rid_of and version_id(it.get("project"), it.get("version")):
            counts["items"] += bool(versions.place(root, rid_of[str(it["n"])], version_id(it.get("project"), it.get("version"))))
    for ln in plan.get("links") or []:
        a, b = (rid_of.get(str((ln or {}).get("earlier"))), rid_of.get(str((ln or {}).get("later")))) if isinstance(ln, dict) else (None, None)
        if a and b and a != b:
            try:
                itemlinks.link(root, a, b, by="AI")
                counts["links"] += 1
            except Exception as exc:
                activity.swallowed(root, "drive._submit_organize", exc)
    for e in plan.get("expected") or []:
        if isinstance(e, dict) and e.get("title") and version_id(e.get("project"), e.get("version")):
            versions.expect(root, version_id(e.get("project"), e.get("version")), e["title"], e.get("why", ""), e.get("due", ""))
            counts["expected"] += 1
    versions.organize_done(root)
    _close(root, job, "done")
    said = f"{counts['versions']} 个版本、{counts['items']} 件事归好了，补了 {counts['links']} 条前后关系、{counts['expected']} 件预计会遇到的事"
    activity.log(root, "learn", "整理项目和版本：" + said, detail=job.get("driver") or "")
    try:
        inbox.add(root, f"organize:{job['id']}", f"✓ {job.get('driver') or 'AI'} 整理好了项目和版本：{said}。到「记忆 → 项目与版本」里看看。", kind="system")
    except Exception as exc:
        activity.swallowed(root, "drive._submit_organize", exc)
    return {"ok": True, "status": "done", **counts}


def _prepare_tidy(root, config, driver):
    """Once a day, when something new came in: the AI reads the whole memory and proposes changes (tidy.py)."""
    from . import tidy
    from .workenv import scratch
    if _busy(root, "tidy"):
        return None
    new = tidy.due(root)
    if not new:
        return None
    tidy.tried(root)
    stamp = now().strftime("%Y%m%d%H%M%S")
    folder = scratch(root, "tidy", stamp)
    new.update(tidy.prepare(root, folder))
    return {"id": f"tidy-{stamp}", "kind": "tidy", "title": "整理记忆", "folder": str(folder), "ref": new,
            "output": ["tidy.json"], "lease_until": _lease(OTHER_LEASE_MINUTES), "instructions": promptfile.read(root, "tidy.md")}


def _submit_tidy(root, job):
    """Nothing in the memory changes here: every proposal waits in 记忆更新 for the owner's 用 / 不要."""
    from . import tidy
    try:
        plan = json.loads((Path(job["folder"]) / "tidy.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        plan = None
    proposals, problems = tidy.check(root, plan, profile_evidence=job["ref"].get("profile_evidence"))
    if plan is None or (problems and job["rounds"] < MAX_FIX_ROUNDS):
        job["rounds"] += 1
        _save(root, job)
        return {"ok": False, "status": "fix", "problem": ("没有找到有效的 tidy.json（格式见说明里的例子）。" if plan is None
                                                          else "；".join(problems)[:600] + "。改好或删掉这几条再交。")}
    added = tidy.save(root, proposals, job["id"])
    tidy.done(root, job["ref"].get("cursor"))
    _close(root, job, "done")
    activity.log(root, "learn", f"整理记忆：{added} 条更新等你确认" if added else "整理记忆：没有要改的", detail=job.get("driver") or "")
    if added:   # told once, like the other finished work: the list waits on top of 记忆
        try:
            from . import inbox
            inbox.add(root, f"tidy:{job['id']}", f"✓ {job.get('driver') or 'AI'} 整理了一遍记忆：{added} 条更新等你确认，在「记忆」最上面。", kind="system")
        except Exception as exc:
            activity.swallowed(root, "drive._submit_tidy", exc)
    return {"ok": True, "status": "done", "proposed": added}


def _prepare_learn(root, config, driver):
    """Now and then: turn the owner's recent feedback into standing rules (偏好快照)."""
    from . import memory, thread as conversation
    from .workenv import scratch
    if _busy(root, "learn"):
        return None
    since = conversation.setting(root, "learned_at", "")
    checked_at = now().isoformat(timespec="seconds")
    db = _db(root)
    try:
        # The job table survives restarts. A completed round resets the delay for the next batch.
        recent = db.execute("SELECT status,updated FROM jobs WHERE kind='learn' AND status IN ('done','failed') "
                            "ORDER BY updated DESC,rowid DESC LIMIT ?", (len(LEARN_RETRY_MINUTES),)).fetchall()
        failures = 0
        for status, _ in recent:
            if status != "failed":
                break
            failures += 1
        if failures and now() < datetime.fromisoformat(recent[0][1]) + timedelta(minutes=LEARN_RETRY_MINUTES[failures - 1]):
            return None
        messages = db.execute("SELECT record_id,at,text FROM messages WHERE sender='me' AND kind='feedback' "
                              "AND at>? AND length(text)>=6 ORDER BY at,id", (since,)).fetchall()
    finally:
        db.close()
    outcomes = memory.outcomes(root, since=since)
    if not messages and not outcomes:
        return None
    from .snapshot import _snapshot
    snap = {r.get("record_id"): r for r in _snapshot(root).get("rows") or []}
    feedback = [{"item": str((snap.get(rid) or {}).get("标题") or ""), "at": at, "said": said[:400]}
                for rid, at, said in messages]
    verdicts = []
    for o in outcomes:
        row = snap.get(o["record_id"]) or {}
        verdicts.append({**{k: v for k, v in o.items() if k != "record_id"}, "n": row.get("编号"), "item": str(row.get("标题") or "")})
    fresh_enough = len(feedback) + len(verdicts) >= LEARN_MIN_NEW
    stale = not since or now() - datetime.fromisoformat(since) > timedelta(hours=LEARN_EVERY_HOURS)
    if not (fresh_enough or stale):
        return None
    stamp = now().strftime("%Y%m%d%H%M%S")
    folder = scratch(root, "learn", stamp)
    (folder / "feedback.json").write_text(json.dumps(feedback[-60:], ensure_ascii=False, indent=1), encoding="utf-8")
    (folder / "outcomes.json").write_text(json.dumps(verdicts[-80:], ensure_ascii=False, indent=1), encoding="utf-8")
    from . import library
    library.snapshot(root, folder)
    what = "、".join(x for x in (f"{len(verdicts)} 次评审" if verdicts else "", f"{len(feedback)} 条反馈" if feedback else "") if x)
    return {"id": f"learn-{stamp}", "kind": "learn", "title": f"从你最近 {what}里学规矩", "folder": str(folder),
            "ref": {"count": len(feedback), "checked_at": checked_at}, "output": ["learned.json"], "lease_until": _lease(OTHER_LEASE_MINUTES),
            "instructions": promptfile.read(root, "learn.md")}


def _submit_learn(root, job):
    """learned.json → drafts for the 经验库, waiting in 记忆更新 for the owner's 用 (nothing is written to the library here)."""
    from . import activity
    folder = Path(job["folder"])
    problem = '缺少学经验的结果。请返回 {"drafts": [...]}；没有值得学的写 {"drafts": []}。'
    for name in ("learned.json", ".last.txt"):
        try:
            raw = (folder / name).read_text(encoding="utf-8-sig").strip()
            fenced = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", raw, re.S | re.I)
            learned = json.loads(fenced[1] if fenced else raw)
            if not isinstance(learned, dict) or not isinstance(learned.get("drafts"), list):
                raise ValueError
            break
        except FileNotFoundError:
            continue
        except (ValueError, UnicodeError):
            problem = '结果格式有误。请返回 {"drafts": [...]}，每份草稿的格式见说明。'
    else:
        if job["rounds"] >= MAX_FIX_ROUNDS:
            return fail(root, job, problem)
        job["rounds"] += 1
        _save(root, job)
        return {"ok": False, "status": "fix", "problem": problem}
    from . import library
    bad = []
    for i, d in enumerate(learned["drafts"][:3], 1):
        try:
            library.check_draft(root, d)
        except ValueError as exc:
            bad.append(f"第 {i} 份：{exc}")
    if bad and job["rounds"] < MAX_FIX_ROUNDS:
        job["rounds"] += 1
        _save(root, job)
        return {"ok": False, "status": "fix", "problem": "；".join(bad)[:600] + " 改好再交；拿不准的那份直接删掉。"}
    checked_at = job["ref"].get("checked_at") or job.get("created") or now().isoformat(timespec="seconds")
    db = _db(root)
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            closed = db.execute("UPDATE jobs SET status='done',updated=?,lease_until=NULL WHERE id=? AND status='open'",
                                (now().isoformat(timespec="seconds"), job["id"]))
            if not closed.rowcount:
                return _closed(get(root, job["id"]))
            last = db.execute("SELECT value FROM settings WHERE key='learned_at'").fetchone()
            if last and checked_at <= json.loads(last[0]):
                return {"ok": True, "status": "done", "proposed": 0}
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('learned_at',?)", (json.dumps(checked_at),))
    finally:
        db.close()
    proposed = save_drafts(root, learned["drafts"][:3], job["id"])
    if proposed:
        activity.log(root, "detect", f"从你的反馈里学到 {proposed} 条经验草稿，等你在「记忆更新」里点「用」")
    return {"ok": True, "status": "done", "proposed": proposed}

def _try_existing_review(root, source_dir, validate, publish, label):
    """Extracted earlier but the publish failed: publish without a new model run."""
    from . import activity
    from .workenv import _cli
    if not (Path(source_dir) / "review.json").is_file():
        return False
    code, out = _cli(root, *validate)
    if code != 0 or not out.get("ok"):
        return False
    code, out = _cli(root, *publish)
    if not out.get("ok"):
        activity.log(root, "error", f"{label}待办入表没成功", detail=str(out.get("error"))[:200], status="failed")
    return True


def _write_related(root, folder):
    """related_items.json: what a new item may be the next step of: everything still open, plus what was
    finished in the last two weeks."""
    from .snapshot import _snapshot
    since = (now() - timedelta(days=14)).isoformat()[:10]
    out = []
    for r in _snapshot(root).get("rows") or []:
        status = r.get("状态") or ""
        if r.get("排除本人提醒") or status == "取消" or not r.get("编号"):
            continue
        if status == "完成" and str(r.get("完成时间") or r.get("更新时间") or "")[:10] < since:
            continue
        out.append({"编号": int(str(r["编号"])), "标题": r.get("标题"), "状态": status})
    (Path(folder) / "related_items.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


def _listener_alive(root):
    from . import meeting_watch
    from .state import State
    state = State(Path(root) / "state/state.db")
    try:
        beat = state.get("listen:heartbeat")
    finally:
        state.close()
    try:
        return bool(beat) and now() - meeting_watch.parse_time(beat) < timedelta(minutes=3)
    except (TypeError, ValueError):
        return False


def _ready_meetings(root, config):
    from . import meeting_watch
    from .itemstore import feishu_free
    from .state import State
    if (not config or not config.meeting_watch_enabled or not config.meeting_watch_started_at
            or config.intake_mode == "manual" or feishu_free(config)):
        return []
    current = now()
    state = State(Path(root) / "state/state.db")
    try:
        queue = meeting_watch.MeetingQueue(state)
        ready = queue.ready(current)
        beat = state.get("listen:heartbeat")
        last = state.get("meeting_watch:last_search")
        if ready or (beat and current - meeting_watch.parse_time(beat) < timedelta(minutes=3)):
            return ready
        if last and current - meeting_watch.parse_time(last) < meeting_watch.SEARCH_WITHOUT_PUSH:
            return []
    finally:
        state.close()
    # Standalone installations can run a driver without a listener.
    from .workenv import _check
    _, checked = _check(root, "agent_matrix.meeting_watch")
    return checked.get("ready") or []


def _prepare_meeting(root, config, driver):
    from .meeting_review import Review
    from .workenv import _cli, scratch
    taken = {j["ref"].get("meeting_id") for j in open_jobs(root) if j["kind"] == "meeting"}
    for m in _ready_meetings(root, config):
        mid = m["meeting_id"]
        if mid in taken:
            continue
        if m.get("status") == "needs_attention":
            _cli(root, "agent_matrix.meeting_watch", "notify-missing", "--meeting-id", mid)
            continue
        if not config.transcripts_to_llm_allowed or config.llm_backend != "cindy_current":
            continue
        code, prep = _cli(root, "agent_matrix.meeting_watch", "prepare", "--meeting-id", mid)
        if code != 0 or not prep.get("transcript_path"):
            _cli(root, "agent_matrix.meeting_watch", "defer", "--meeting-id", mid)
            continue
        source = Path(prep["transcript_path"]).parent
        review = str(source / "review.json")
        validate = ["agent_matrix.meeting_review", "validate", "--review", review]
        publish = ["agent_matrix.meeting_review", "publish", "--review", review]
        if _try_existing_review(root, source, validate, publish, "会议"):
            continue
        (source / "review.schema.json").write_text(json.dumps(Review.model_json_schema(), ensure_ascii=False), encoding="utf-8")
        open_name = Path(prep.get("open_items_path") or "open_items.json").name
        folder = scratch(root, "extract", mid)
        for name in ("transcript.md", open_name, "review.schema.json"):
            if (source / name).is_file():
                shutil.copyfile(source / name, folder / name)
        (folder / "review.json").unlink(missing_ok=True)
        _write_related(root, folder)
        topic = str((prep.get("metadata") or {}).get("topic") or m.get("topic") or "会议")
        header = (f"本次会议：{topic}。meeting_id = {mid}，transcript_sha256 = {prep['transcript_sha256']}。\n"
                  f"逐字稿：transcript.md；没完成的事项：{open_name}；review.json 的结构见 review.schema.json。\n"
                  "只需要在当前文件夹写好 review.json；校验和发布由程序做。")
        return {"id": f"meeting-{mid}", "kind": "meeting", "title": f"提取会议待办：{topic}"[:40], "folder": str(folder),
                "ref": {"meeting_id": mid, "source": str(source), "validate": validate, "publish": publish},
                "output": ["review.json"], "lease_until": _lease(OTHER_LEASE_MINUTES),
                "instructions": header + "\n\n" + promptfile.read(root, "meeting_extract.md")}
    return None


def _chat_due(root, config=None):
    from .workenv import _check
    if (config and not (config.chat_watch_enabled or config.wechat_enabled)) or _busy(root, "chat"):
        return False
    code, checked = _check(root, "agent_matrix.chat_review")
    if code != 0 or not checked.get("run_model"):
        return False
    batch = (checked.get("batch") or {}).get("batch_id")
    if not batch:
        return True
    jid = "chat-" + batch
    db = _db(root)
    try:
        cutoff = (now() - timedelta(minutes=30)).isoformat(timespec="seconds")
        return not db.execute("SELECT 1 FROM jobs WHERE kind='chat' AND status='failed' AND updated>? "
                              "AND (id=? OR id GLOB ?) LIMIT 1", (cutoff, jid, jid + "-*")).fetchone()
    finally:
        db.close()


def _prepare_chat(root, config, driver):
    from .chat_review import ChatReview
    from .workenv import _cli, scratch
    if not _chat_due(root, config):
        return None
    code, prep = _cli(root, "agent_matrix.chat_review", "prepare")
    if code != 0 or not prep.get("candidates_path"):
        return None
    source = Path(prep["candidates_path"]).parent
    review = prep.get("review_path") or str(source / "review.json")
    validate = ["agent_matrix.chat_review", "validate", "--review", review]
    publish = ["agent_matrix.chat_review", "publish", "--review", review]
    (source / "review.schema.json").write_text(json.dumps(ChatReview.model_json_schema(), ensure_ascii=False), encoding="utf-8")
    batch = str(prep.get("batch_id") or source.name)
    folder = scratch(root, "extract", source.name)
    for name in (Path(prep["candidates_path"]).name, "review.schema.json"):
        if (source / name).is_file():
            shutil.copyfile(source / name, folder / name)
    (folder / "review.json").unlink(missing_ok=True)
    _write_related(root, folder)
    try:  # what was said after each candidate, so things already settled are not turned into todos
        from .background import add_followups
        from .workenv import project
        path = folder / Path(prep["candidates_path"]).name
        with project(root, config) as (client, state):
            pass
        data = add_followups(client, json.loads(path.read_text(encoding="utf-8")), now())
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        # the batch keeps them too: the program checks the owner's "好的" against these lines, not the model's word
        (source / path.name).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as exc:
        activity.swallowed(root, "drive._prepare_chat", exc)
    header = (f"本批聊天：batch_id = {batch}。候选消息：{Path(prep['candidates_path']).name}；结构见 review.schema.json。\n"
              "只需要在当前文件夹写好 review.json；校验和发布由程序做。")
    return {"id": f"chat-{re.sub(r'[^A-Za-z0-9_-]', '', batch)}", "kind": "chat", "title": "提取聊天待办", "folder": str(folder),
            "ref": {"source": str(source), "validate": validate, "publish": publish}, "output": ["review.json"],
            "lease_until": _lease(OTHER_LEASE_MINUTES),
            "instructions": header + "\n\n" + promptfile.read(root, "chat_extract.md")}


def _history_text(root, record_id, limit=30):
    from . import thread as conversation
    all_messages = conversation.history(root, [record_id]).get(record_id, {}).get("messages", [])
    # Both personal notes and historical closure events may contain the owner's private text.
    msgs = [m for m in all_messages if m["kind"] not in {"note", "closed"}][-limit:]
    who = {"me": "本人", "ai": "AI", "system": "系统"}
    return "\n".join(f"- {m['at']} {m.get('driver') if m['from'] == 'ai' and m.get('driver') else who.get(m['from'], '')}：{m['text'][:400]}"
                     for m in msgs)


def _prepare_item(root, config, driver, skip=(), allow=None, name_for=None):
    from .itemwork import Item
    from .itemwork import pick_next
    pick = pick_next(root, config, skip=skip, allow=allow)
    if not pick:
        return None
    if name_for:
        driver = name_for(pick["record_id"])
    item = Item(root, config, pick)
    got = item.claim(lease_minutes=ITEM_LEASE_MINUTES)
    if not got:
        return {"skip": pick["record_id"]}
    client, rows, row, notes = got
    try:
        return {**_item_job(root, config, driver, item, client, rows, row, notes), "driver_name": driver}
    except BaseException:
        item.release()   # claimed but not handed out: back to the queue now, not when the lease runs out
        raise


def _item_job(root, config, driver, item, client, rows, row, notes):
    from .itemwork import _changes, first_prompt, resume_prompt
    from .workenv import project
    from .prework import add_note
    add_note(root, item.rid, f"{driver} 开始处理")
    item.prepare_folder(client, rows, row, notes)
    with project(root, config, client=False, wait=60) as (_, state):
        first = state.get("work:first", [])
        if item.rid in first:
            state.set("work:first", [x for x in first if x != item.rid])
    changes = _changes(item.workdir, row)
    history = _history_text(root, item.rid)
    from .eventbrief import build as brief
    told = brief(root, item.rid, rows)
    stage = _stage(root, item, rows, row)   # 交付规格 and 做事方式: what this round produces
    base = (told + "\n---\n\n" if told else "") + first_prompt(root, row) + "\n\n" + stage   # the brief first: the AI need not dig for it
    extra = []
    if history:
        extra += ["", "## 这件事之前的对话（心响保存；可能是别的 AI 做的，接着往下，不要从头来）", history]
    if changes:
        extra += ["", "## 本人新补充的要求（这一轮必须照做）", *[f"- {c}" for c in changes]]
    return {"id": f"item-{item.rid}-{item.version[:8]}", "kind": "item", "title": item.title, "n": int(str(item.n)) if item.n else None,
            "folder": str(item.workdir), "ref": {"record_id": item.rid, "version": item.version},
            "output": ["交付.md", "结果.json"], "lease_until": _lease(ITEM_LEASE_MINUTES),
            "instructions": base + "\n".join(extra),
            # the built-in GPT resumes its own conversation instead of re-reading everything
            "builtin_prompt": resume_prompt(changes, (item.workdir / ".timed_out").is_file()) + "\n\n" + stage if any((item.workdir / f).is_file() for f in (".thread", ".claude-session")) else base}


def _stage(root, item, rows, row):
    """Which kind of deliverable this is, how settled its spec is, and what this round does (specs.plan), kept in the
    folder for the check after it and for the page."""
    from . import specs
    from .background import _one, parent_of
    old = specs.read_plan(item.workdir) or {}
    parent = parent_of(row, rows)
    kind = old.get("kind") or specs.guess(_one(row.get("标题")), _one(row.get("下一步动作")), _one(parent.get("标题")) if parent else "")
    p = specs.plan(root, item.rid, kind, outline_done=(item.workdir / "提纲.md").is_file())
    p["workdir"] = str(item.workdir)
    p["checklist"] = specs.checklist(specs.text(root, kind)) or specs.checklist(specs.expert(root, kind)) if kind else []
    specs.keep_plan(item.workdir, p)
    return specs.work_text(root, p)


SORTING = ("triage", "meeting", "chat", "learn", "donecheck")   # 整理: judging new items, meetings, chats, learning rules, done yet


def item_runner(root):
    """Who does the items (设置 → AI 执行 → 做事用哪个): "cindy" (Cindy's own schedule takes them) or "local"."""
    from .thread import setting
    chosen = setting(Path(root), "engine", "codex")
    return "cindy" if chosen == "cindy" else "driver" if str(chosen).startswith("driver:") else "local"


def sort_runner(root):
    """Who sorts (judging new items, meetings, chats, learning rules): "cindy" or "local"."""
    from .thread import setting
    chosen = setting(Path(root), "engine_sort", "")
    return "cindy" if chosen == "cindy" else "driver" if str(chosen).startswith("driver:") else "local"


def cindy_kinds(root):
    """What Cindy's schedule takes: the items and/or the sorting, as the owner chose (设置 → 谁来做事)."""
    return [*(SORTING if sort_runner(root) == "cindy" else ()), *(("item",) if item_runner(root) == "cindy" else ())]


def local_kinds(root):
    """What the built-in driver takes: the sorting unless it was handed to someone else, and the items that are its own
    (all of them when a local AI is chosen in settings, else only those handed to a local AI one by one)."""
    from .aidrivers import LOCAL
    from .thread import setting
    from .aidrivers import standing_in
    sorts = sort_runner(root) == "local" or setting(Path(root), "engine_sort", "") in standing_in(root)   # standing in
    return [*(("profile", "organize", "tidy") if setting(Path(root), "engine", "codex") in LOCAL else ()), *(SORTING if sorts else ()), "item"]


def kinds_for(root, driver):
    """What this AI takes: the sorting if it was handed to it, and items (only its own: see item_filter)."""
    from .aidrivers import is_mine
    from .thread import setting
    work, sort = setting(Path(root), "engine", "codex"), setting(Path(root), "engine_sort", "")
    return [*(("setup", "profile", "organize", "tidy") if is_mine(work, driver) else ()), *(SORTING if is_mine(sort, driver) else ()), "item"]


def item_filter(root, driver, *, builtin=False):
    """Which items this AI may take: those handed to it, and, unless handed to someone else, those of the AI chosen
    in settings when that is this one."""
    from . import aidrivers
    from .thread import setting
    from .aidrivers import standing_in
    table, usual = aidrivers.assignments(root), setting(Path(root), "engine", "codex")
    stood = set(standing_in(root)) if builtin else set()   # the built-in driver also takes the work of an AI it stands in for
    return lambda rid: aidrivers.is_mine(table.get(rid) or usual, driver, builtin=builtin) or (table.get(rid) or usual) in stood


def has_sorting_for(root, config):
    """Is there a meeting, a round of chats or new items to judge waiting? Cheap: a schedule's pre-run check asks."""
    from . import thread as conversation
    try:
        taken = {j["ref"].get("meeting_id") for j in open_jobs(root) if j["kind"] == "meeting"}
        # with the listener running it fetches the transcript first: wake the AI only for one already here
        fetched = {"prepared"} if _listener_alive(root) else {"ready", "prepared"}
        if any(m.get("status") in fetched and m["meeting_id"] not in taken for m in _ready_meetings(root, config)):
            return "meeting"
        if _chat_due(root, config):
            return "chat"
        if conversation.setting(Path(root), "auto_assign", False) and not _busy(root, "triage"):
            from .itemwork import _pending_new
            from .snapshot import _snapshot
            if _pending_new(_snapshot(root).get("rows") or [], conversation.judgments(root)):
                return "triage"
        if not _busy(root, "donecheck"):
            from . import donecheck
            from .snapshot import _snapshot
            if donecheck.due(root, _snapshot(root).get("rows") or [], now()):
                return "donecheck"
    except Exception as exc:
        activity.swallowed(root, "drive.has_sorting_for", exc)
    return None


def next_job(root, config, driver, *, skip=(), kinds=None, builtin=False, only=None):
    """The next piece of work for this driver, already prepared; None when there is nothing to do.
    kinds limits what it takes: e.g. Cindy takes only items, the built-in driver then only sorting work.
    builtin: the built-in driver asks (it takes the items of the local AIs, each run with its own AI).
    only: one item by its record id, that the owner or another AI handed to whoever got the message."""
    root = Path(root)
    from . import aidrivers
    allow, name_for = None, None
    if only:
        owner = aidrivers.assigned(root, only)
        if owner and owner != aidrivers.HANDOFF and not aidrivers.is_mine(owner, driver):
            raise MatrixError(f"这件事交给了 {aidrivers.label(owner)}，不是你。")
        if any(j["kind"] == "item" and j["ref"].get("record_id") == only for j in open_jobs(root)):
            raise MatrixError("这件事正在被别的 AI 做。")
        kinds, allow = ["item"], (lambda rid: rid == only)
    elif builtin:
        allow = item_filter(root, driver, builtin=True)
        name_for = lambda rid: aidrivers.LOCAL.get(aidrivers.owner_of(root, rid), driver)
    elif "cindy" in str(driver).lower() or aidrivers.is_known(root, driver):
        # Cindy's schedule and every AI that said hello take what was handed to them, whatever their prompt asks for
        kinds, allow = kinds_for(root, driver), item_filter(root, driver)
    wanted = set(kinds or (*SORTING, "item"))
    if "setup" in wanted:   # the owner is waiting on this one: it goes before anything else
        job = _prepare_setup(root, config, driver)
        if job:
            return _open(root, job, driver)
    if "profile" in wanted:
        job = _prepare_profile(root, config, driver)
        if job:
            return _open(root, job, driver)
    if "organize" in wanted:
        job = _prepare_organize(root, config, driver)
        if job:
            return _open(root, job, driver)
    for kind, prepare in zip(SORTING, (_prepare_triage, _prepare_meeting, _prepare_chat, _prepare_learn, _prepare_donecheck)):
        if kind not in wanted:
            continue
        if kind in {"chat", "learn"}:
            from .state import exclusive_lock
            try:
                with exclusive_lock(root / "state" / (kind + "-claim"), wait=0):
                    job = prepare(root, config, driver)
                    if job:
                        return _open(root, job, driver)
            except MatrixBusy:
                pass
            continue
        job = prepare(root, config, driver)
        if job:
            return _open(root, job, driver)
    tidy_now = lambda: (_open(root, job, driver) if "tidy" in wanted and (job := _prepare_tidy(root, config, driver)) else None)
    if "item" not in wanted:
        return tidy_now()   # tidying the memory is for idle time: after the owner's own work, never before it
    tried = set(skip)
    for _ in range(5):
        job = _prepare_item(root, config, driver, skip=tried, allow=allow, name_for=name_for)
        if not job:
            return tidy_now()
        if "skip" in job:
            tried.add(job["skip"])
            continue
        if only and aidrivers.assigned(root, only) == aidrivers.HANDOFF:
            aidrivers.assign(root, only, aidrivers.setting_of(aidrivers.clean(driver)))   # whoever got the message, has it now
        # Preparing background may take time; the owner may have cancelled meanwhile.
        # Publish the lease under the same lock used by owner commands.
        from .prework import current
        from .workenv import project
        with project(root, config) as (client, state):
            current(config, client, state, job["ref"]["record_id"], job["ref"]["version"])
            return _open(root, job, job.pop("driver_name", None) or driver)
    return None


def _open(root, job, driver):
    # A restored/retried item gets a fresh lease identity. Old callbacks cannot target the new run.
    db = _db(root)
    try:
        if db.execute("SELECT 1 FROM jobs WHERE id=?", (job["id"],)).fetchone():
            job["id"] += "-" + uuid4().hex[:12]
    finally:
        db.close()
    try:  # 记忆层: every AI reads the owner's standing rules before any work
        from .memory import profile_text
        from .library import memory_text
        project_id, version = None, ""
        if job["kind"] == "item" and (job.get("ref") or {}).get("record_id"):
            from . import projects, versions
            rid = job["ref"]["record_id"]
            project_id = projects.chosen(root).get(rid)
            from .snapshot import _snapshot
            version = versions.context_text(root, rid, {r.get("record_id"): r for r in _snapshot(root).get("rows") or []})
        head = memory_text(root, job.get("folder")) + profile_text(root) + common_text(root) + version
        job["instructions"] = head + job["instructions"]
        if job.get("builtin_prompt"):
            job["builtin_prompt"] = head + job["builtin_prompt"]
    except Exception as exc:
        activity.swallowed(root, "drive._open", exc)
    if job["kind"] == "item" and "cindy" in str(driver).lower():
        job["instructions"] = cindy_header(root, job) + job["instructions"]
    job.update(driver=driver, status="open", rounds=0, step="领到了")
    _save(root, job)
    keep_prompt(root, job)
    return job


def prompt_path(root, record_id):
    return Path(root) / "state" / "prompts" / f"{record_id}.md"


def keep_prompt(root, job):
    """The exact words this item's AI was given, kept so the owner can read them (事项详情 → 查看交给 AI 的说明).
    The latest run only; the built-in GPT gets builtin_prompt, every other AI the instructions."""
    rid = (job.get("ref") or {}).get("record_id")
    if job.get("kind") != "item" or not rid:
        return
    try:
        path = prompt_path(root, rid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(job.get("builtin_prompt") or job.get("instructions") or "", encoding="utf-8")
    except OSError as exc:
        activity.swallowed(root, "drive.keep_prompt", exc)


def common_text(root):
    """The rules every job shares (prompts/_common.md), right after the owner's own rules and 关于你: one place, so the
    job descriptions only say what is particular to them."""
    try:
        from .config import load
        owner = load(Path(root) / "config.yaml").owner_name
    except Exception:
        owner = ""
    try:
        text = promptfile.read(root, "_common.md")
    except OSError:
        return ""
    return text.replace("（{owner}）", f"（{owner}）" if owner else "").replace("{owner}", owner or "本人")


def cindy_header(root, job):
    """Cindy works inside the owner's own project: say so, so it follows that project's rules and memory and does not
    mistake 心响 (which only hands out work and takes deliverables) for the product it is working on."""
    from .thread import setting
    project = setting(Path(root), "cindy_project", "") or ""
    name = Path(project).name if project else "当前"
    return (f"## 先说清楚：你在哪、在做什么\n"
            f"- 你在本人的「{name}」项目里（Cindy 的当前工作目录）。这件事是这个项目的工作：按这个项目自己的规范、模板和记忆来做。\n"
            f"- 心响只是给你派活、收交付的系统，不是你要做需求的产品；不要把心响的东西写进交付。\n"
            f"- 回复第一行写「心响 #{job.get('n') or ''} {job.get('title') or ''}」，方便本人在心响里找到这段对话。\n"
            f"- 交付文件写到下面说的工作文件夹（folder）里，做完按末尾的命令交给心响。\n\n")


def work_for(root, config, driver):
    """What this AI would get if it asked now, checked in code before it is woken (a schedule's pre-run check):
    "setup", "profile", "organize", "tidy", "item", "meeting", "chat", "triage", or None. No model, no job opened."""
    from . import memory, setupasks, tidy, versions
    kinds = set(kinds_for(root, driver))
    for kind, asked in (("setup", setupasks.pending), ("profile", memory.scan_asked), ("organize", versions.organize_asked),
                        ("tidy", tidy.due)):
        if kind in kinds and not _busy(root, kind) and asked(root):
            return kind
    if has_items_for(root, config, driver):
        return "item"
    if kinds & set(SORTING):
        return has_sorting_for(root, config)
    return None


WORK_WORDS = {"setup": "接入设置", "profile": "「认识你」", "organize": "整理项目和版本", "tidy": "整理记忆",
              "item": "交给 AI 的事", "meeting": "整理会议", "chat": "整理聊天", "triage": "判断新进来的事",
              "donecheck": "看你的事办完没有"}


def has_items_for(root, config, driver="Cindy"):
    """Is there an item for this AI waiting that nobody is working on? Cheap enough for a schedule's pre-run check."""
    from .workenv import _check
    code, checked = _check(root, "agent_matrix.prework")
    if code != 0:
        return False
    taken = {j["ref"].get("record_id") for j in open_jobs(root) if j["kind"] == "item"}
    mine = item_filter(root, driver)
    return any(r.get("status") in {"queued", "running", "prepared"} and r["record_id"] not in taken and mine(r["record_id"])
               for r in checked.get("ready") or [])


def public(job):
    """What an outside AI gets back from `next`."""
    out = {k: job.get(k) for k in ("id", "kind", "title", "n", "folder", "output")}
    out["job"] = out.pop("id")
    out["instructions"] = job["instructions"] + "\n" + protocol_tail(job)
    return out


# ---- progress, words, finishing ------------------------------------------------------------------------

def step(root, config, job_id, text, driver=None):
    job = get(root, job_id)
    if closed := _closed(job):
        return closed
    text = re.sub(r"\s+", " ", str(text or "")).strip()[:15]
    job.update(step=text, lease_until=_lease(ITEM_LEASE_MINUTES if job["kind"] == "item" else OTHER_LEASE_MINUTES))
    progress_at = job.get("progress_at")
    write = job["kind"] == "item" and (not progress_at or now() - datetime.fromisoformat(progress_at) > timedelta(seconds=60))
    if write:
        job["progress_at"] = now().isoformat(timespec="seconds")
    if not _save(root, job):
        return _closed(get(root, job_id))
    if write:
        try:
            from .prework import report_progress
            from .workenv import project
            with project(root, config, wait=20) as (client, state):
                if closed := _closed(get(root, job_id)):
                    return closed
                report_progress(config, client, state, job["ref"]["record_id"], text)
        except (MatrixError, MatrixBusy, OSError):
            pass
    return {"ok": True, "step": text}


def say(root, job_id, text, driver=None):
    job = get(root, job_id)
    if closed := _closed(job):
        return closed
    if job["kind"] == "setup":   # shown where the owner clicked 接入, until the next thing is said
        from . import setupasks
        setupasks.note(root, job["ref"]["what"], said=str(text or "").strip()[:300], said_at=now().isoformat(timespec="seconds"))
        return {"ok": True}
    if job["kind"] != "item":
        return {"ok": False, "error": "只有事项有对话。"}
    from . import thread as conversation
    conversation.add(root, job["ref"]["record_id"], "ai", "say", str(text)[:3000], driver=driver or job.get("driver"))
    return {"ok": True}


def fail(root, job, problem):
    """A job the AI could not finish is said out loud: the panel shows it and 心响 gets a message."""
    from . import activity, inbox
    job = get(root, job["id"]) if isinstance(job, dict) and "id" in job else job
    job.update(status="failed", lease_until=None)
    if not _save(root, job):
        return _closed(get(root, job["id"]))
    if job["kind"] == "learn":
        activity.log(root, "learn", "学规矩已延后，后台会再试", detail=str(problem)[:300], status="deferred", ref=job["id"])
        return {"ok": False, "status": "failed", "problem": str(problem)[:300]}
    if job["kind"] == "donecheck":   # nothing waits on it: the same items are looked at again next round, quietly
        activity.log(root, "detect", "看你的事办完没有：这一轮没做成，下一轮再看", detail=str(problem)[:300], status="deferred", ref=job["id"])
        return {"ok": False, "status": "failed", "problem": str(problem)[:300]}
    activity.log(root, "error", f"{job.get('title') or job['kind']}：没做成", detail=str(problem)[:300], status="failed")
    try:
        key = f"job-failed:{job['id']}:{job.get('rounds', 0)}"
        if job["kind"] == "chat":
            key = "chat-failed:" + Path(job["ref"]["source"]).name
        inbox.add(root, key,
                  f"⚠ {job.get('title') or 'AI 的活'}没做成\n原因：{str(problem)[:200]}\n系统会在 30 分钟后自动再试一次；"
                  f"急的话在「系统」页点「立即检查」。", kind="system")
    except Exception as exc:
        activity.swallowed(root, "drive.fail", exc)
    return {"ok": False, "status": "failed", "problem": str(problem)[:300]}


def _close(root, job, status):
    job.update(status=status, lease_until=None)
    _save(root, job)


def _item(root, config, job):
    from .itemwork import Item
    item = Item(root, config, {"record_id": job["ref"]["record_id"], "input_version": job["ref"]["version"], "title": job.get("title")})
    item.n, item.finished, item.job_id = job.get("n"), False, job["id"]
    return item


def submit(root, config, job_id, driver=None):
    """Check the output; publish it, or say exactly what to fix (the job stays open for the fix)."""
    job = get(root, job_id)
    if job["status"] == "cancelled":
        return _closed(job)
    if job["status"] != "open":
        return {"ok": True, "status": job["status"], "note": "这件活已经结束了。"}
    os.environ.setdefault("XINXIANG_DRIVER", job.get("driver") or driver or "AI")
    kind = job["kind"]
    if kind == "item":
        return _submit_item(root, config, job)
    if kind == "triage":
        return _submit_triage(root, job)
    if kind == "donecheck":
        return _submit_donecheck(root, job)
    if kind == "learn":
        return _submit_learn(root, job)
    if kind == "setup":
        return _submit_setup(root, job)
    if kind == "profile":
        return _submit_profile(root, job)
    if kind == "organize":
        return _submit_organize(root, job)
    if kind == "tidy":
        return _submit_tidy(root, job)
    from . import activity
    from .workenv import _cli
    folder, source = Path(job["folder"]), Path(job["ref"]["source"])
    if (folder / "review.json").is_file():
        shutil.copyfile(folder / "review.json", source / "review.json")
    if not (source / "review.json").is_file():
        code, out = 1, {"error": "AI 还没有生成 review.json，请在当前工作目录写好这份文件再提交。"}
    else:
        code, out = _cli(root, *job["ref"]["validate"])
    if code != 0 or not out.get("ok"):
        job["rounds"] += 1
        problem = str(out.get("error") or "review.json 不符合格式")[:300]
        if job["rounds"] > MAX_FIX_ROUNDS:
            return fail(root, job, problem)
        _save(root, job)
        return {"ok": False, "status": "fix", "problem": problem + "\n只修正有依据的字段；没有承接字眼的条目直接删掉。"}
    code, out = _cli(root, *job["ref"]["publish"])
    if not out.get("ok"):
        return fail(root, job, "入表没成功：" + str(out.get("error"))[:200])
    _close(root, job, "done")
    if out.get("repaired"):
        activity.log(root, "detect", f"{job['title']}：自动修正 {len(out['repaired'])} 处", detail="；".join(out["repaired"])[:300])
    return {"ok": True, "status": "done", "created": out.get("created") or len(out.get("task_ids") or []),
            "repaired": out.get("repaired") or []}


def _submit_item(root, config, job):
    from .itemwork import _remember, check_result
    item = _item(root, config, job)
    result, problem = check_result(item.workdir, None, root)
    if closed := _closed(get(root, job["id"])):
        return closed
    if problem:
        job["rounds"] += 1
        if job["rounds"] > ITEM_FIX_ROUNDS:
            item.block(f"自检没通过：{problem}"[:28], {"note": problem}, 0)
            _close(root, job, "blocked")
            return {"ok": False, "status": "blocked", "problem": problem}
        _save(root, job)
        return {"ok": False, "status": "fix", "problem": problem}
    _remember(item.workdir, _row_of(root, item.rid))
    if save_drafts(root, result.get("memory_drafts") or [], job["id"]):
        from . import activity
        activity.log(root, "detect", f"#{item.n} 做事时提了经验草稿，等你在「记忆更新」里点「用」")
    if result["outcome"] == "blocked":
        item.block(str(result["question"]), result, 0)
        _close(root, job, "blocked")
        return {"ok": True, "status": "blocked"}
    if result["outcome"] == "handled":
        return _handled(root, item, job, result)
    _keep_spec(root, item)
    if result["outcome"] == "outline":
        item.keep_files()
        question = str(result.get("question") or "按这个提纲写吗？").strip()
        item.block(f"提纲好了：{question}"[:60], {"tried": "调研完，提纲写在 提纲.md", "note": str(result.get("summary") or "")}, 0)
        _close(root, job, "blocked")
        return {"ok": True, "status": "blocked", "outline": True}
    published = item.publish(result, 0)
    _close(root, job, "done")
    return {"ok": True, "status": "done", "url": published.get("url")}


def _keep_spec(root, item):
    """规格.md from the AI: the first spec of its kind is used from now on; under review it waits for the owner's 用."""
    from . import specs
    p = specs.read_plan(item.workdir) or {}
    try:
        body = (item.workdir / "规格.md").read_text(encoding="utf-8")
    except OSError:
        return
    if p.get("form") and specs.form(root, p.get("kind"), body):
        activity.log(root, "learn", f"#{item.n} 做事时形成了「{p['kind']}」的交付规格（草稿），在「记忆 → 交付规格」里能看能改")
    elif p.get("revise") and specs.propose(root, p.get("kind"), body):
        activity.log(root, "learn", f"「{p['kind']}」的交付规格有一份修订等你确认")


def save_drafts(root, drafts, job_id):
    """AI drafts for the 经验库 become 记忆更新 waiting for the owner's 用. Returns how many are new."""
    from . import library, tidy
    proposals = []
    for d in drafts:
        try:
            _, summary, data = library.check_draft(root, d)
        except ValueError:
            continue
        proposals.append(("memory", summary, data["why"], data))
    return tidy.save(root, proposals, job_id) if proposals else 0


def _reply_command(root, command):
    """Apply one owner command ("12 完成") as the AI would type it; True when it went through."""
    run = subprocess.run([sys.executable, "-m", "agent_matrix.reply", "--text", command, "--source", "ai"], cwd=root, capture_output=True,
                         text=True, encoding="utf-8", timeout=180)
    return "✓" in (run.stdout or "")


def _handled(root, item, job, result):
    """Before doing it, the AI found it already done (or called off) in the source conversation: close it the way the
    owner would, keeping the line that shows it. Not closed (the command failed): it waits for the owner instead."""
    from . import thread as conversation
    _close(root, job, "done")   # first: the 完成 below would otherwise stop this very run
    item.release()
    url = str(result.get("url") or "")
    url = url if url.startswith("http") else ""
    skip = result.get("close_as") == "skip"
    commands = [f"{item.n} 不做"] if skip else [f"{item.n} 完成", f"{item.n} + {url}"] if url else [f"{item.n} 无交付"]
    ok = bool(item.n) and all([_reply_command(root, c) for c in commands])
    reason, evidence = str(result["reason"])[:60], str(result["evidence"])[:300]
    if ok:
        conversation.judge(root, item.rid, "skip" if skip else "done", reason)
    conversation.add(root, item.rid, "ai", "say", f"动手前核对：{reason}\n依据：{evidence}" +
                     ("" if ok else "\n（没能自动关掉，你看一眼：确实办完了就点完成）"), driver=job.get("driver"))
    item.done_status("已有人办完" if ok else "待你确认", reason, 0)
    return {"ok": True, "status": "handled", "closed": ok}


def _record_of(root, n):
    from .snapshot import _snapshot
    return next((r["record_id"] for r in _snapshot(root).get("rows") or [] if str(r.get("编号")) == str(n)), None)


def _row_of(root, record_id):
    from .snapshot import _snapshot
    return next((r for r in _snapshot(root).get("rows") or [] if r.get("record_id") == record_id), {})


def _submit_setup(root, job):
    """心响 checks for itself whether it is really set up; the ask goes away only then."""
    from . import inbox, setupasks
    from .workenv import _cli
    what = job["ref"]["what"]
    code, out = _cli(root, "agent_matrix.setupcheck", "status", *([] if what == "fix" else ["--only", what]), timeout=120)
    if not out:
        return {"ok": False, "status": "fix", "problem": "心响没查到配置情况，过一会儿再交一次。"}
    if not out.get("ready"):
        job["rounds"] += 1
        if job["rounds"] > 40:
            return fail(root, job, "改了很多轮还是没配好：" + "、".join(out.get("left") or []))
        _save(root, job)
        return {"ok": False, "status": "fix", "problem": "还没好：" + "、".join(out.get("left") or []) + "\n" + str(out.get("text") or "")[-1500:]}
    setupasks.done(root, what)
    _close(root, job, "done")
    activity.log(root, "setup", f"{job['title']}：好了", detail=job.get("driver") or "")
    try:
        inbox.add(root, f"setup-done:{job['id']}", f"✓ {job['title']}，{job.get('driver') or 'AI'} 配好了。", kind="system")
    except Exception as exc:
        activity.swallowed(root, "drive._submit_setup", exc)
    return {"ok": True, "status": "done"}


def _submit_triage(root, job):
    from . import activity, thread as conversation
    from .itemwork import TRIAGE_LABEL
    try:
        verdicts = json.loads((Path(job["folder"]) / "triage.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        job["rounds"] += 1
        _save(root, job)
        return {"ok": False, "status": "fix", "problem": "没有找到有效的 triage.json（格式见说明里的例子）。"}
    by_n = {i["n"]: i for i in job["ref"]["items"]}
    applied = []
    for v in verdicts if isinstance(verdicts, list) else []:
        item = by_n.get(v.get("n") if isinstance(v, dict) else None)
        decision = (v or {}).get("decision")
        if not item or decision not in TRIAGE_LABEL:
            continue
        if decision == "prep" and item["who"] == "AI":
            decision = "ai"
        reason = str(v.get("reason") or "")[:60]
        evidence = str(v.get("evidence") or "")[:300]
        if decision in {"done", "skip"} and not (evidence if decision == "done" else reason):
            continue  # closing something needs a reason (and, for done, the words that prove it)
        url = str(v.get("url") or "") if str(v.get("url") or "").startswith("http") else ""
        n = item["n"]
        # from a meeting: talk, not a handoff. The AI only suggests; the item stays 待确认 until the owner says 「AI做」.
        held = decision in {"ai", "prep"} and item.get("source") == "会议"
        commands = [] if held else {"ai": [f"{n} AI做"], "prep": [f"{n} AI备"], "skip": [f"{n} 不做"],
                                    "done": [f"{n} 完成", f"{n} + {url}"] if url else [f"{n} 无交付"]}.get(decision, [])
        ok = all([_reply_command(root, c) for c in commands])
        if not ok:
            continue  # not applied: judged again next time
        conversation.judge(root, item["record_id"], decision, reason)
        links = expected = 0
        if decision not in {"done", "skip"}:   # only for work still to do: what comes before, after and is expected
            try:
                links, expected = _triage_relations(root, job, item, v)
            except Exception as exc:
                activity.swallowed(root, "drive._submit_triage", exc)
        extra = "、".join(x for x in (f"串上了 {links} 件前后的事" if links else "", f"记下 {expected} 件这个版本预计会遇到的事" if expected else "") if x)
        label = ("建议 AI 做，会议里提的事等你点「AI做」才开工" if decision == "ai" else
                 "建议你来做、AI 先备材料，等你点「AI备」才开工") if held else TRIAGE_LABEL[decision]
        conversation.add(root, item["record_id"], "ai", "say", f"判断：{label}。{reason}" +
                         (f"\n依据：{evidence}" if evidence else "") + (f"\n{extra}。" if extra else ""), driver=job.get("driver"))
        applied.append({"n": item["n"], "decision": decision, **({"held": True} if held else {}), **({"links": links} if links else {}), **({"expected": expected} if expected else {})})
    if applied:
        activity.log(root, "detect", "AI 判断新进来的事：" + "、".join(f"#{a['n']} {'等你点：建议' + a['decision'] if a.get('held') else TRIAGE_LABEL[a['decision']][:6]}" for a in applied))
    _close(root, job, "done")
    return {"ok": True, "status": "done", "applied": applied}


def ask(root, config, job_id, question, tried):
    job = get(root, job_id)
    if closed := _closed(job):
        return closed
    if job["kind"] != "item":
        return {"ok": False, "error": "只有事项可以问本人；提取类的活做不了就 release。"}
    if not str(question or "").strip() or not str(tried or "").strip():
        return {"ok": False, "error": "要写清楚问题（--question）和已经试过什么（--tried）。"}
    _item(root, config, job).block(str(question).strip(), {"tried": str(tried).strip()}, 0)
    _close(root, job, "blocked")
    return {"ok": True, "status": "blocked"}


def release(root, config, job_id, reason="驱动方放回"):
    job = get(root, job_id)
    if job["status"] != "open":
        return {"ok": True, "status": job["status"]}
    if job["kind"] == "item":
        _item(root, config, job).release()
    if job["kind"] == "profile":   # the AI could not do it: the ask goes away (else it comes straight back); the owner can ask again
        from . import memory
        if not job["ref"].get("requested_at") or (memory.scan_asked(root) or {}).get("at") == job["ref"]["requested_at"]:
            memory.cancel_scan(root)
    if job["kind"] == "organize":
        from . import versions
        versions.organize_done(root)
    if job["kind"] == "setup":   # the AI could not do it: the owner sees why, and can ask again
        from . import setupasks, thread as conversation
        what = job["ref"]["what"]
        said = (setupasks.pending(root).get(what) or {}).get("said") or ""
        setupasks.done(root, what)
        conversation.set_setting(Path(root), "setup:gave_up:" + what,
                                 {"by": job.get("driver"), "why": said, "at": now().isoformat(timespec="seconds")})
    _close(root, job, "released")
    return {"ok": True, "status": "released"}


# ---- one item to another AI -------------------------------------------------------------------------------

def handoff_message(n, title, value):
    """What is sent to the AI that takes over one item: it says who it is (without taking everything else over),
    takes that one item, and carries on from where the last one stopped."""
    from .aidrivers import name_in
    from .setupshim import AI_RULES, work_cmd
    xin, name = work_cmd(), name_in(value) or "<你的名字>"
    return f"""请你来做我电脑上「心响」里的一件事：#{n} {title}。心响是本机的待办工具；这件事之前可能有别的 AI 做过，任务单、背景、之前的对话和上一版都在它给你的工作台里，接着做，不要从头来。

1. 告诉心响你是谁：
{xin} hello --name "{name}" --app "<你所在的客户端>" --model "<你现在用的模型>" --helper
2. 领这件事：
{xin} next --driver "{name}" --n {n}
3. 进入返回的 folder，按 instructions 做，用里面写的命令报进度、交活。
4. 交完再运行一次 {xin} next --driver "{name}"：还有交给你的事就接着做，返回 idle 就结束。

{AI_RULES}"""


def hand_over(root, config, record_id, value, *, by, why="", n=None, title="", job=None):
    """Give one item to one AI (value as in aidrivers; empty = back to the AI chosen in settings).

    A run in progress stops: the AI that handed it over closes its own job; when the owner does it, whoever is
    working on it is told to stop. The item goes straight back to the queue for the new AI."""
    from . import aidrivers
    from .prework import add_note
    from .thread import setting as conversation_setting
    root = Path(root)
    usual = conversation_setting(root, "engine", "codex")
    to = aidrivers.label(value or usual)
    if value == usual:
        value = ""   # handed to the one chosen in settings anyway: it follows the settings again
    if job is not None:
        _close(root, job, "handed")
        _item(root, config, job).release()
    else:
        for jid in stop_record(root, record_id, why=f"本人把这件事交给了 {to}"):
            _item(root, config, get(root, jid)).release()
    aidrivers.assign(root, record_id, value)
    add_note(root, record_id, f"{by} 把这件事交给了 {to}" + (f"：{why}" if why else ""))
    out, target = {"ok": True, "status": "handed", "to": to}, value or usual
    if target in aidrivers.LOCAL:
        from .workenv import kick
        kick(root, config, force=True)
        out["note"] = f"{to} 会自己接手，不用发消息。"
    elif target == "cindy":
        out["note"] = "Cindy 下次来领活时会接手。"
    else:
        out["message"] = handoff_message(n, title, target)
        known = {d["name"]: d for d in aidrivers.known(root)}
        if (known.get(aidrivers.name_in(target) or "") or {}).get("online"):
            out["note"] = f"{to} 会按它的定时任务来领，会自己接手；它一直不来时再把 message 发给它。"
    return out


def handoff(root, config, job_id, to, why, driver):
    """The AI working on an item gives that one item to another AI."""
    from . import aidrivers
    job = get(root, job_id)
    if closed := _closed(job):
        return closed
    if job["kind"] != "item":
        return {"ok": False, "error": "只有事项能交给别的 AI；整理类的活做不了就 release。"}
    if not str(to or "").strip():
        return {"ok": False, "error": "要写交给谁：--to \"它的名字\"（比如 Claude Code、本机 Gemini、Cindy）。"}
    value = aidrivers.resolve(root, to)
    me = job.get("driver") or driver
    os.environ["XINXIANG_DRIVER"] = me   # the note says who handed it on
    if aidrivers.is_mine(value, me) or aidrivers.label(value) == me:
        return {"ok": False, "error": "这就是你自己。"}
    return hand_over(root, config, job["ref"]["record_id"], value, by=me, why=str(why or "").strip()[:200],
                     n=job.get("n"), title=job.get("title") or "", job=job)


# ---- command line ----------------------------------------------------------------------------------------

GUIDE = """# 接入心响：任何 AI 都可以驱动

你是本人的 AI 员工。

0. 先说你是谁（只用一次）：`{cmd} hello --name "<你的名字>" --app "<你所在的客户端>" --model "<你用的模型>"`。
   从这一刻起，心响的事和整理都交给你；本人把这段话发给另一个 AI，就换成那个 AI。
1. 领一件活：`{cmd} next --driver "<你的名字>" --session "<你这段对话的编号或链接>"`。
   `--session` 让本人在心响里看到你在做哪件事，并能直接打开你这段对话；没有就省略。
2. 返回的 JSON 里有 `folder`（工作文件夹）、`instructions`（这件活的完整说明）、`output`（要写出的文件）。
   进入 folder，按 instructions 做；需要飞书资料时用 `lark-cli`（只读）。
3. 按 instructions 末尾的命令报进度、交活：`{cmd} submit --job <job>`。返回 fix 就按 problem 改好再交。
4. 回到第 1 步，直到返回 `{{"idle": true}}`：这一轮就结束。

以后怎么来领活：
{rounds}

规则：飞书里的东西只读；建文档、改表格、通知本人都由心响做。删数据、停服、花钱、对外发消息（本人之外有人会收到的都算，包括 GitHub 等公开平台的评论）、提交审批、发布、改产品方向、新建或轮换密钥、修改权限只准备材料，不执行。
每件事的对话历史由心响保存，next 会把它交给你：接着上一个 AI 往下做，不要从头来。
"""


def guide_text(cmd):
    """The guide any AI reads: how to ask for work, and how to come by on a schedule instead of waiting."""
    from .setupshim import rounds
    return GUIDE.format(cmd=cmd, rounds=rounds(cmd))


def main(argv=None):
    from .config import load
    p = argparse.ArgumentParser(prog="xinxiang", description="心响驱动协议：任何 AI 都能领活、交活")
    p.add_argument("command", choices=["hello", "next", "step", "say", "submit", "ask", "release", "handoff", "guide", "status", "gate",
                                        "wake-hook", "context"])
    p.add_argument("--url", help="wake-hook：心响有活时请求这个地址来叫醒你（Standard Webhooks 签名）；空 = 取消")
    p.add_argument("--secret", help="wake-hook：签名密钥（不给就用心响生成的那串）")
    p.add_argument("--name", help="hello：你是谁（Antigravity、Gemini、Claude Code、Cursor …）")
    p.add_argument("--app", default="", help="hello：你所在的客户端")
    p.add_argument("--model", default="", help="hello：你现在用的模型")
    p.add_argument("--wait", type=int, default=0, help="next：没活时最多等这么多秒再回来（期间有活立刻给你）")
    p.add_argument("--helper", action="store_true", help="hello：只来做交给你的事，不接手其余的")
    p.add_argument("--part", choices=["all", "work", "sort"], default="all", help="hello：接手全部，或只接「做事」/「整理」")
    p.add_argument("--n", type=int, help="next：领交给你的这一件（编号）")
    p.add_argument("--to", help="handoff：交给哪个 AI"); p.add_argument("--why", default="", help="handoff：为什么")
    p.add_argument("--kinds", help="只领这几类活，逗号分隔：item,triage,meeting,chat,learn,tidy")
    p.add_argument("--driver", default=os.environ.get("XINXIANG_DRIVER") or "外部 AI")
    p.add_argument("--source", type=Path, help="context：事项文件夹里的 来源.json")
    p.add_argument("--hours", type=int, default=3, help="context：从原话前多少小时取到现在（最多 168）")
    p.add_argument("--job"); p.add_argument("--text"); p.add_argument("--question"); p.add_argument("--tried")
    p.add_argument("--session", default=os.environ.get("XINXIANG_SESSION"), help="你这段对话的编号或能打开它的链接")
    p.add_argument("--config", type=Path, default=Path("config.yaml"))
    args = p.parse_args(argv)
    path = args.config.resolve()
    root = path.parent
    os.environ["XINXIANG_DRIVER"] = args.driver
    install_shim(root)
    try:
        if args.command == "guide":
            print(guide_text(_cmd()))
            return 0
        if args.command == "context":   # read-only and lock-free: it runs inside the AI's sandbox
            from .background import context
            from .feishu import Feishu
            from .workenv import now
            row = json.loads(args.source.read_text(encoding="utf-8")) if args.source and args.source.is_file() else None
            if not isinstance(row, dict):
                print("要 --source：事项文件夹里的 来源.json（背景.md 第一节给了完整命令）。", file=sys.stderr)
                return 2
            print(context(root, Feishu(load(path), root), row, now(), args.hours))
            return 0
        if args.command == "status":
            print(json.dumps({"open": [{k: j[k] for k in ("id", "kind", "title", "driver", "step")} for j in open_jobs(root)],
                              "drivers": drivers(root)}, ensure_ascii=False))
            return 0
        from . import aidrivers
        if args.command == "hello":
            # an AI says who it is: from now on it does the items and the sorting, until another one says hello
            try:
                take = False if args.helper else True if args.part == "all" else args.part
                me = aidrivers.hello(root, args.name or args.driver, app=args.app, model=args.model, take=take)
            except ValueError as exc:
                raise MatrixError(str(exc)) from None
            print(json.dumps({"ok": True, "you": me["name"],
                              "now": "记下了，交给你的事你来做" if args.helper else {"all": "心响的事和整理都交给你了", "work": "心响的「做事」交给你了",
                                                                                   "sort": "心响的「整理」交给你了"}[args.part],
                              "next": f'{_cmd()} next --driver "{me["name"]}"',
                              "later": ("" if args.helper else
                                        f'每 5 分钟来一趟：前置检查 {_cmd()} gate --driver "{me["name"]}"（0 = 有活，2 = 没活跳过），'
                                        f'有活就运行 next 领到 idle 为止；不要用 --wait 守着。完整说明：{_cmd()} guide')}, ensure_ascii=False))
            return 0
        if args.command == "wake-hook":
            # an AI tells 心响 how to wake it: without --url it gets the signing secret to put on its side first
            name = "Cindy" if args.driver.lower() == "cindy" else aidrivers.clean(args.driver)
            value = "cindy" if name == "Cindy" else aidrivers.setting_of(name)
            if value != "cindy" and not aidrivers.is_known(root, name):
                raise MatrixError(f"{name} 还没报到：先运行 hello。")
            if args.url is None:
                print(json.dumps({"ok": True, "secret": aidrivers.pending_secret(root, value),
                                  "next": f'在你那边的 Webhook 里选 Standard Webhooks、填上这串签名密钥，再运行 {_cmd()} wake-hook --driver "{name}" --url "<接收地址>"'},
                                 ensure_ascii=False))
                return 0
            try:
                hook = aidrivers.set_hook(root, value, args.url, args.secret)
            except ValueError as exc:
                raise MatrixError(str(exc)) from None
            if not hook:
                print(json.dumps({"ok": True, "now": f"不再主动叫醒 {name}，等它自己的定时任务来"}, ensure_ascii=False))
                return 0
            tried = aidrivers.send_wake(hook, "test")   # one real wake-up: the round it starts finds nothing and ends
            print(json.dumps({"ok": tried.get("ok", False), "now": f"心响有活时会马上叫醒 {name}" if tried.get("ok") else tried.get("why")},
                             ensure_ascii=False))
            return 0 if tried.get("ok") else 1
        aidrivers.seen(root, args.driver)
        config = load(path)
        if args.command == "gate":
            # a schedule's pre-run check: exit 0 = there is work for this AI, 2 = nothing (skip this round, no tokens).
            # Cindy's own pickup schedule calls it without a name (only a name given here counts, not one inherited).
            named = any(a == "--driver" or a.startswith("--driver=") for a in (sys.argv[1:] if argv is None else argv))
            me = args.driver if named else "Cindy"
            work = work_for(root, config, me)
            print(json.dumps({"ok": True, "work": work}, ensure_ascii=False))
            return 0 if work else 2
        if args.command == "next":
            kinds = [k.strip() for k in (args.kinds or "").split(",") if k.strip()] or None
            only = _record_of(root, args.n) if args.n else None
            if args.n and not only:
                raise MatrixError(f"找不到 #{args.n}。")
            job = next_job(root, config, args.driver, kinds=kinds, only=only)
            if args.n and not job:
                raise MatrixError(f"#{args.n} 现在不在 AI 的待做里：可能已经交付、在等本人，或刚被叫停。")
            until = time.monotonic() + max(0, min(args.wait, 3600))
            while not job and time.monotonic() < until:   # nothing yet: stay on the line, so the AI need not poll
                time.sleep(5)
                aidrivers.seen(root, args.driver)
                job = next_job(root, config, args.driver, kinds=kinds)
            if job and args.session:
                from .sessions import bind
                bind(root, job["id"], args.session)
            out = public(job) if job else {"idle": True}
        elif not args.job:
            raise MatrixError("需要 --job。")
        elif args.command == "step":
            if args.session:
                from .sessions import bind
                bind(root, args.job, args.session)
            out = step(root, config, args.job, args.text)
        elif args.command == "say":
            out = say(root, args.job, args.text, args.driver)
        elif args.command == "submit":
            out = submit(root, config, args.job, args.driver)
        elif args.command == "ask":
            out = ask(root, config, args.job, args.question, args.tried)
        elif args.command == "handoff":
            out = handoff(root, config, args.job, args.to, args.why, args.driver)
        else:
            out = release(root, config, args.job)
        print(json.dumps(out, ensure_ascii=False))
        return 0
    except (MatrixError, MatrixBusy) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
