"""Rules for one item's work folder: the first and follow-up instructions, what changed since last time,
checking the result before it is handed in, and picking what to do next."""
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

from . import activity
from .models import MatrixBusy, MatrixError
from .state import State, exclusive_lock
from .timeutil import TZ
from . import jobstore
from .engines import engine
from .workenv import _check, now, project, read_status, scratch, write_status

MAX_FIX_ROUNDS = 2

SECTIONS_ALWAYS = ["用到的资料", "我做的假设"]
TEMPLATE_HINT = {"需求文档": "要解决什么问题 / 需求（按功能分节，每条规则写清条件和结果） / 待确认", "周报": "本周完成 / 进行中 / 下周计划 / 风险", "数据汇总": "结论 / 数据表 / 口径",
                 "方案": "推荐 / 选项对比 / 需要你拍板", "会议材料": "议程 / 需要决策 / 背景数据",
                 "回复草稿": "草稿 / 需要你确认", "其他": "按完成标准逐条覆盖"}
TRIAGE_LABEL = {"ai": "AI 能独立做，已接手", "prep": "需要你来做，AI 先备材料", "me": "需要你自己来",
                "skip": "不用做，已放下", "done": "你已经办完了，已关掉"}


def _plain(markdown):
    return "".join(re.sub(r"[#*`>|_\-\[\]()!~]", "", markdown).split())


def _section_names(markdown):
    return [re.sub(r"\s+", "", m.group(1)) for m in re.finditer(r"^#{1,6}\s*(.+?)\s*$", markdown, re.M)]


def check_result(workdir, row, root=None):
    """(result, problem). problem is a short sentence the model can act on, or None when it passes."""
    from .prework import PLACEHOLDERS, TEMPLATES
    try:
        result = json.loads((Path(workdir) / "结果.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "没有找到有效的 结果.json。按说明写好再结束。"
    if not isinstance(result, dict) or result.get("outcome") not in {"done", "blocked", "handled"}:
        return result, "结果.json 的 outcome 只能是 done、blocked 或 handled。"
    problem = draft_problem(result, root) if root else None
    if problem:
        return result, problem
    if result["outcome"] == "handled":
        if not str(result.get("evidence") or "").strip() or not str(result.get("reason") or "").strip():
            return result, "handled 必须写 evidence（原样抄出说明已经办完或不用做的那一句）和 reason；抄不出那一句就不是 handled，接着做。"
        return result, None
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
    if result.get("reply") is not None and (not isinstance(result["reply"], str) or len(result["reply"]) > 2000):
        return result, "reply 是要发给对方的原话，文字，不超过 2000 字；不用回复就不写这一项。"
    notice = result.get("notify")
    if notice is not None and not (isinstance(notice, dict) and re.fullmatch(r"[CG][A-Z0-9]{6,}", str(notice.get("channel") or ""))
                                   and isinstance(notice.get("text"), str) and 0 < len(notice["text"].strip()) <= 2000):
        return result, 'notify 是群通知：{"channel": "Slack 频道 ID（C 开头）", "label": "#频道名", "text": "原话，不超过 2000 字"}；不用通知就不写这一项。'
    if len(str(result.get("confirmation") or "")) > 30:
        return result, "confirmation 不超过 30 字。"
    if not result.get("sources"):
        return result, "sources 要列出实际用到的资料。"
    if not (isinstance(result.get("memory"), list) and all(isinstance(x, str) for x in result["memory"])):
        return result, "memory 要写这一轮读了并照着做的经验（如 [\"发 PR 经验#先跑测试\"]）；一份都没用上就写 []。"
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
        return result, f"正文 {length} 字，「{template}」上限 {cap} 字。删掉重复和铺垫，不要压缩成看不懂的短语。"
    if length < 80:
        return result, "交付.md 正文太短，像没做完。"
    return result, None


def draft_problem(result, root):
    """memory_drafts (optional): each must be a valid draft for the library; the first problem, or None."""
    from . import library
    drafts = result.get("memory_drafts")
    if drafts is None:
        return None
    if not isinstance(drafts, list) or len(drafts) > 3:
        return "memory_drafts 是草稿数组，一轮最多 3 份；没有就不写这一项。"
    for i, d in enumerate(drafts, 1):
        try:
            library.check_draft(root, d)
        except ValueError as exc:
            return f"memory_drafts 第 {i} 份：{exc}"
    return None



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
    from .promptfile import read as prompt_text
    template = prompt_text(root, "worker.md")
    hints = "；".join(f"{k}：{v}" for k, v in TEMPLATE_HINT.items())
    return template.format(item=f"#{row.get('编号')} {_one(row.get('标题'))}", templates=hints)


def resume_prompt(changes, timed_out):
    if changes:
        lines = "\n".join("- " + c for c in changes)
        return ("本人有新的要求，事件.md 和任务单.md 已更新：\n" + lines +
                "\n\n在上一版（上一版.md）基础上改，逐条改到；每一条都用同样的办法把同类全部复查，一次补齐。规则不变：先补背景，写 step.txt，"
                "最后重写 交付.md 和 结果.json 再结束。")
    if timed_out:
        return "上一轮时间到了，没做完。接着做，不要从头来；最后写好 交付.md 和 结果.json 再结束。"
    return "接着上次往下做，最后写好 交付.md 和 结果.json 再结束。"


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


def pick_next(root, config, skip=(), allow=None):
    """Next AI item: the owner's "先做" first, then by due date (prework.check already orders by due date).
    allow: only items this AI may take (an item handed to one AI is that AI's alone)."""
    code, checked = _check(root, "agent_matrix.prework")
    ready = [r for r in checked.get("ready") or [] if r.get("status") in {"queued", "running", "prepared"}
             and r["record_id"] not in skip and (allow is None or allow(r["record_id"]))]
    with project(root, config, client=False, wait=60) as (_, state):
        first = state.get("work:first", [])
    ready.sort(key=lambda r: (r["record_id"] not in first, first.index(r["record_id"]) if r["record_id"] in first else 0))
    write_status(root, queue=[{"record_id": r["record_id"], "title": str(r.get("title") or "")[:26]} for r in ready[1:]])
    return ready[0] if ready else None


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
        for name in (".thread", ".claude-session", ".gemini-session", "交付.md", ".seen.json"):  # carry the conversation over if the folder moved
            if (keep / name).is_file() and not (self.workdir / name).is_file():
                shutil.copyfile(keep / name, self.workdir / name)
        previous = None
        if (self.workdir / "交付.md").is_file():
            shutil.copyfile(self.workdir / "交付.md", self.workdir / "上一版.md")
            previous = "上一版.md"
        for name in ("结果.json", "step.txt", ".last.txt"):
            (self.workdir / name).unlink(missing_ok=True)
        from . import eventbrief   # the brief the AI reads first: what to do, how it came up, what is settled, what is next
        (self.workdir / "事件.md").write_text(eventbrief.build(self.root, self.rid, rows), encoding="utf-8")
        (self.workdir / "任务单.md").write_text(background.task_sheet(row, rows), encoding="utf-8")
        self.show("找背景", "程序在拉背景")
        own_base = "/base/" + self.config.base_token if self.config.base_token else ""
        (self.workdir / "背景.md").write_text(background.build(self.root, client, rows, background.with_parent_source(row, rows), now(), notes=notes, previous=previous,
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
            if jobstore.get(self.root, job_id)["status"] != "open":
                raise MatrixError("这件活已经结束，旧运行不能再写入。")

    def release(self):
        """Give the item back to the queue at once (interrupted, or the driver let it go)."""
        from .prework_store import WorkQueue
        try:
            with project(self.root, self.config, client=False, wait=30) as (_, state):
                if any(j["kind"] == "item" and j["id"] != getattr(self, "job_id", None)
                       and j["ref"].get("record_id") == self.rid and j["ref"].get("version") == self.version
                       for j in jobstore.open_jobs(self.root)):
                    return  # a restored run already owns this lease
                WorkQueue(state).release(self.rid, self.version)
        except Exception as exc:
            activity.swallowed(self.root, "itemwork.release", exc)

    def mirror_reply(self):
        """What the AI said at the end of its turn goes into the item's conversation in 心响."""
        try:
            text = (self.workdir / ".last.txt").read_text(encoding="utf-8").strip()
        except OSError:
            return
        if text:
            from . import thread as conversation
            conversation.add(self.root, self.rid, "ai", "say", text[:3000], driver=engine(self.root).NAME)
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
        for name in ("交付.md", "结果.json", "事件.md", "任务单.md", "背景.md", ".thread", ".claude-session", ".gemini-session", ".seen.json"):
            # On a plain-ASCII project path the AI works in the project's own copy: nothing to copy then.
            if (self.workdir / name).is_file() and (self.workdir / name).resolve() != (keep / name).resolve():
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
        if str(result.get("reply") or "").strip():   # waits in 心响 for the owner's 发送; never sent from here
            from . import replies
            replies.save(self.root, self.rid, result["reply"])
        if isinstance(result.get("notify"), dict):   # a group notice (e.g. PR is up): also waits for 发送
            from . import replies
            replies.save_notice(self.root, self.rid, result["notify"])
        try:   # 记忆增强: what was delivered and its conclusions, for later work to build on
            from . import hymem
            hymem.remember(self.root, "deliverable", f"交付「{self.title}」第 {version} 版：{result.get('summary', '')}\n{doc[:1500]}",
                           ref=f"deliverable:{self.rid}:{version}")
        except Exception as exc:
            activity.swallowed(self.root, "itemwork.publish", exc)
        self.done_status("已交付", result["summary"], tokens, url=published["url"])
        return {"record_id": self.rid, "result": "published", "url": published["url"]}

    def done_status(self, outcome, text, tokens, url=None):
        from . import activity
        self.finished = True
        activity.log(self.root, "prework", f"#{self.n} {self.title[:20]} · {outcome}", detail=text, ref=url or "")
        write_status(self.root, last={"n": self.n, "title": self.title[:26], "outcome": outcome, "text": text[:40],
                                      "tokens": tokens, "at": now().isoformat(timespec="seconds")},
                     stage="空闲", step="", record_id=None, n=None, title="")
