"""经验库：本人审过的做事经验，一个主题一份 Markdown（照数字分身的记忆库）。

    state/memory/<名字>.md     开头两行 `name：` `description：`，正文按 `## 主题` 分节，每节写做法、**Why:**、**How to apply:**
    index_text / memory_text   派给 AI 的只有目录（`- 名字 — 什么时候打开`），正文复制进工作文件夹，AI 按要做的动作自己读
    check_draft / apply_draft  AI 只能提草稿（新建、追加、修改、删除、改描述、挪一节），本人在「记忆更新」里点「用」才写进库；
                               修改、删除、挪动要逐字抄原文，并且在点「用」的那一刻重新对文件匹配（中间本人可能手改过）
    ensure                     第一次用时，把旧的一句话规矩（preferences 表）转成经验文件

只存经验，不存事实：事实（代码、数据、聊天、会上定了什么）去现场查。
"""
import json
from pathlib import Path
import re
import shutil

MAX_DESCRIPTION = 120
INDEX_MAX_ENTRIES = 100
MAX_FILE = 40_000
NAME = re.compile(r"[^\s/\\:*?\"<>|.][^/\\:*?\"<>|]{0,39}")
HEADER = re.compile(r"^(name|description)\s*[:：]\s*(.*)$", re.I)
GENERAL = "做事通用规矩"


def folder(root):
    return Path(root) / "state" / "memory"


def _path(root, name):
    if not isinstance(name, str) or not NAME.fullmatch(name.strip()) or name != name.strip():
        raise ValueError("名字 1–40 个字，不能有 / \\ : * ? \" < > | 这些符号，也不能以点开头。")
    return folder(root) / f"{name}.md"


def sections(body):
    """[(title, text)] of the `## ` sections, text including its heading line."""
    out, title, lines = [], None, []
    for line in body.split("\n"):
        if line.startswith("## "):
            if title is not None:
                out.append((title, "\n".join(lines).strip()))
            title, lines = line[3:].strip(), [line]
        elif title is not None:
            lines.append(line)
    if title is not None:
        out.append((title, "\n".join(lines).strip()))
    return out


def parse(text, name=None):
    """{"name", "description", "body", "sections"} or ValueError saying what is wrong, in words the AI or owner can fix."""
    lines = str(text).replace("\r\n", "\n").lstrip("﻿").split("\n")
    head, at = {}, 0
    while at < len(lines):
        line = lines[at].strip()
        if not line and not head:
            at += 1
            continue
        m = HEADER.match(line)
        if not m:
            break
        head[m.group(1).lower()] = m.group(2).strip()
        at += 1
    if not head.get("name") or not head.get("description"):
        raise ValueError("开头两行要是「name：名字」和「description：做哪些动作时打开」。")
    if name is not None and head["name"] != name:
        raise ValueError(f"name 要和文件名一致：{name}。")
    if len(head["description"]) > MAX_DESCRIPTION:
        raise ValueError(f"description {len(head['description'])} 字，最多 {MAX_DESCRIPTION} 字。")
    body = "\n".join(lines[at:]).strip()
    found = sections(body)
    if not found:
        raise ValueError("正文至少要有一个「## 主题」小节。")
    titles = [t for t, _ in found]
    if len(set(titles)) != len(titles):
        raise ValueError("同一份里小节标题不能重复：" + "、".join(sorted({t for t in titles if titles.count(t) > 1})))
    return {"name": head["name"], "description": head["description"], "body": body, "sections": found}


def render(name, description, body):
    return f"name：{name}\ndescription：{description}\n\n{body.strip()}\n"


def entries(root):
    """Every valid memory, by name. A file that does not parse is left out (and listed by `problems`)."""
    ensure(root)
    out = []
    for path in sorted(folder(root).glob("*.md")):
        try:
            out.append({**parse(path.read_text(encoding="utf-8"), path.stem), "raw": path.read_text(encoding="utf-8")})
        except (ValueError, OSError):
            continue
    return out


def problems(root):
    out = []
    for path in sorted(folder(root).glob("*.md")) if folder(root).is_dir() else []:
        try:
            parse(path.read_text(encoding="utf-8"), path.stem)
        except (ValueError, OSError) as exc:
            out.append(f"{path.name}：{exc}")
    return out


def get(root, name):
    return next((e for e in entries(root) if e["name"] == name), None)


def save(root, name, text, old_name=None):
    """The owner's own edit (or an applied draft): validate, write, rename if the name changed."""
    entry = parse(text, name)
    path = _path(root, name)
    if len(text) > MAX_FILE:
        raise ValueError(f"一份最多 {MAX_FILE} 字，拆成两份。")
    if old_name and old_name != name:
        if path.exists():
            raise ValueError(f"已经有一份叫「{name}」了。")
        _path(root, old_name).unlink(missing_ok=True)
    elif old_name is None and path.exists():
        raise ValueError(f"已经有一份叫「{name}」了。")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(entry["name"], entry["description"], entry["body"]), encoding="utf-8")
    return entry


def delete(root, name):
    _path(root, name).unlink(missing_ok=True)


def index_text(items):
    return "\n".join(f"- {e['name']} — {e['description']}" for e in items)


def snapshot(root, dest):
    """Copy the library into a job folder (the AI may not read state/ from its sandbox, and the copy is what this round
    saw). Returns the copy's folder."""
    target = Path(dest) / "记忆"
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    items = entries(root)
    for e in items:
        (target / f"{e['name']}.md").write_text(e["raw"], encoding="utf-8")
    (target / "目录.md").write_text("# 目录（名字 — 什么时候打开）\n\n" + index_text(items) + "\n", encoding="utf-8")
    return target


RECALL = """## 经验库（本人审过的做事经验；这里只有目录，正文在 `{where}`）

只存经验，不存客观事实：现在是什么（代码、数据、需求、聊天）去现场查，不要在经验里找。你的基座（Claude / GPT 等）自带的记忆没经过本人审核，和这里冲突时以这里为准。

下面每一条是 `名字 — 什么时候打开`。破折号后面写的不是这份讲什么话题，而是**你在做哪个动作的时候要读它**。这是目录，不是内容；正文要打开 `{where}/<名字>.md` 读，读过目录不算读过经验。

{index}

### 怎么召回

召回不是按话题找，是**按你要做的动作取**。一个讲鉴权的任务，管「发消息」的那份经验里一个鉴权字都没有，换多少个词都搜不中，因为它压根不是按话题存的。

1. 先列这一轮你打算做哪些动作，用动词写：读代码 / 改代码 / 发 PR / 查线上数据 / 写 SQL / 回复同事 / 出方案 / 写周报 / 交付……
2. 逐个拿动作去比目录里的「什么时候打开」，比中就打开那份读全文（或者读相关的那几节）。一个动作可能比中好几份，都读；一份都没比中就是这一轮真用不上，不要硬凑。
3. 有些动作开头判断不了要不要做：查完才知道要不要回同事，看完才知道要不要动代码。**在你决定做它的那一刻回到第 2 步再比一次**。
4. 走完 1–3 还拿不准，用你自己归纳的业务词（不是消息原话）在 `{where}` 里搜一下兜底。
5. 召回有预算：列动作、比目录、读比中的那几份，正常几步就走完。不要把每份都挨个读一遍。

用户提到以前做过的事、要在几个方案里选、被挡住（命令报错、链接打不开、某条路走不通）时：先召回再动手，经验里常写着另一条路。

### 留痕

结果里的 `memory` 写这一轮实际读了、并照着做了的经验，格式 `名字` 或 `名字#小节标题`；一份都没用上写 `[]`。这是本人事后看出哪份经验管用、哪份该读没读的唯一依据。

### 发现值得补的经验

做的过程中发现一条以后别的事也用得上的经验（或者某份经验错了、过时了），写进结果的 `memory_drafts`，格式见结果说明。只能提草稿，本人点「用」才进库；不要直接改 `{where}` 里的文件，改了也不算数。
"""


def memory_text(root, dest=None):
    """The block every job gets: the index and how to recall. The bodies go into `dest`/记忆 when there is a folder."""
    items = entries(root)
    if not items:
        return ""
    where = str(snapshot(root, dest)) if dest and Path(dest).is_dir() else str(folder(root))
    return RECALL.format(where=where, index=index_text(items)) + "\n"


# ---- drafts: the only way an AI changes the library -----------------------------------------------------------

MODES = {"create": "新建一份", "append": "追加小节", "revise": "修改", "remove": "删除一段", "describe": "改目录描述", "move": "挪一节"}
DRAFT_KEYS = {"mode", "name", "description", "body", "old_text", "to", "why", "evidence"}


def _unique(text, old, where):
    count = text.count(old)
    if count == 0:
        raise ValueError(f"old_text 在「{where}」里找不到（要逐字抄原文；本人可能已经改过这份）。")
    if count > 1:
        raise ValueError(f"old_text 在「{where}」里出现了 {count} 次，多抄几行让它只对上一处。")


def check_draft(root, d):
    """(mode, summary, data) of a valid draft, or ValueError with what to fix."""
    if not isinstance(d, dict) or d.get("mode") not in MODES:
        raise ValueError("mode 只能是 " + " / ".join(MODES) + "。")
    if d.keys() - DRAFT_KEYS:
        raise ValueError("不认识这些字段：" + "、".join(sorted(d.keys() - DRAFT_KEYS)) + "。")
    mode, name = d["mode"], str(d.get("name") or "").strip()
    why = " ".join(str(d.get("why") or "").split())[:200]
    if not why:
        raise ValueError("要写 why：为什么要这么改，引用本人的话或具体哪件事。")
    _path(root, name)
    body, old = str(d.get("body") or "").strip(), str(d.get("old_text") or "")
    have = get(root, name)
    data = {"mode": mode, "name": name, "why": why, **({"evidence": str(d["evidence"])[:300]} if d.get("evidence") else {})}
    if mode == "create":
        if have:
            raise ValueError(f"已经有一份叫「{name}」了，往里加用 append。")
        description = " ".join(str(d.get("description") or "").split())
        parse(render(name, description, body), name)
        return mode, f"新建「{name}」— {description}", {**data, "description": description, "body": body}
    if not have:
        raise ValueError(f"经验库里没有「{name}」；新的一份用 create。")
    if mode == "append":
        new = sections(body)
        if not new or not body.startswith("## "):
            raise ValueError("body 要以「## 主题」开头，可以有几个小节。")
        parse(render(name, have["description"], have["body"] + "\n\n" + body), name)
        return mode, f"「{name}」加上：" + "、".join(t for t, _ in new), {**data, "body": body}
    if mode == "describe":
        description = " ".join(str(d.get("description") or "").split())
        parse(render(name, description, have["body"]), name)
        return mode, f"「{name}」的目录描述改成：{description}", {**data, "description": description}
    if not old.strip():
        raise ValueError("revise / remove / move 要逐字抄出 old_text（要改、删或挪走的那一段原文）。")
    _unique(have["body"], old, name)
    if mode == "revise":
        if not body:
            raise ValueError("revise 要写 body：改好后的那一段；整段删掉用 remove。")
        parse(render(name, have["description"], have["body"].replace(old, body)), name)
        return mode, f"「{name}」里改一段：{old.strip().splitlines()[0][:40]}", {**data, "old_text": old, "body": body}
    if mode == "remove":
        left = have["body"].replace(old, "").strip()
        if left:
            parse(render(name, have["description"], left), name)
        return mode, f"「{name}」里删掉一段" + ("（删完这份就空了，整份删掉）" if not left else ""), {**data, "old_text": old}
    to = str(d.get("to") or "").strip()     # move: a whole section (or several) goes to another memory
    if not old.lstrip().startswith("## "):
        raise ValueError("move 的 old_text 要从「## 主题」那一行开始，挪的是整节。")
    target = get(root, to)
    if to == name or (not target and not d.get("description")):
        raise ValueError("to 写挪去的那一份；还没有这一份就同时写 description，会新建。")
    _path(root, to)
    title = old.lstrip()[3:].split("\n", 1)[0].strip()
    return mode, f"「{title}」从「{name}」挪到「{to}」", {**data, "old_text": old, "to": to,
                                                    **({} if target else {"description": " ".join(str(d["description"]).split())})}


def apply_draft(root, d):
    """The owner said 用: make the change against the file as it is now. Returns a problem in plain words, or ""."""
    try:
        have = get(root, d["name"])
        mode = d["mode"]
        if mode == "create":
            save(root, d["name"], render(d["name"], d["description"], d["body"]))
            return ""
        if not have:
            return f"「{d['name']}」已经不在了"
        write = lambda body, description=None: save(root, d["name"], render(d["name"], description or have["description"], body), d["name"])
        if mode == "append":
            write(have["body"] + "\n\n" + d["body"])
        elif mode == "describe":
            write(have["body"], d["description"])
        else:
            _unique(have["body"], d["old_text"], d["name"])
            if mode == "revise":
                write(have["body"].replace(d["old_text"], d["body"]))
            else:
                left = have["body"].replace(d["old_text"], "").strip()
                if mode == "move":
                    target = get(root, d["to"])
                    if target:
                        save(root, d["to"], render(d["to"], target["description"], target["body"] + "\n\n" + d["old_text"].strip()), d["to"])
                    else:
                        save(root, d["to"], render(d["to"], d["description"], d["old_text"].strip()))
                if left:
                    write(left)
                else:
                    delete(root, d["name"])
        return ""
    except ValueError as exc:
        return str(exc)


# ---- from the old one-line rules ----------------------------------------------------------------------------------

def ensure(root):
    """First use: turn the rules (preferences) into memory files once. Rules the owner had not confirmed become drafts."""
    target = folder(root)
    if (target / ".migrated").exists():
        return
    from . import memory
    try:
        rules = memory.rules(root)     # seeds the starter rules on a new install
    except Exception:
        return
    target.mkdir(parents=True, exist_ok=True)
    (target / ".migrated").write_text("from preferences", encoding="utf-8")
    kept, waiting = {}, {}    # action ("" = every job) → section texts
    for r in rules:
        title = r["rule"].rstrip("。.")[:60]
        where = f"（只在{r['scope_name']}）" if r.get("scope") else ""
        text = f"## {title}{where}\n{r['rule']}\n\n**Why:** {r.get('why') or '本人定的'}"
        (waiting if r["fresh"] else kept).setdefault(r.get("trigger") or "", []).append(text)
    name_of = lambda key: f"{key}经验" if key else GENERAL
    about = lambda key: f"要{key}时打开。" if key else "每件事动手前都读：交付怎么写、背景怎么找、哪些事不用管。"
    for key, texts in kept.items():
        try:
            save(root, name_of(key), render(name_of(key), about(key), "\n\n".join(_dedupe(texts))))
        except ValueError:
            continue
    proposals = []
    for key, texts in waiting.items():   # learned by the AI, never confirmed: they wait for 用 like any other draft
        body = "\n\n".join(_dedupe(texts))
        d = ({"mode": "append", "name": name_of(key), "body": body} if get(root, name_of(key)) else
             {"mode": "create", "name": name_of(key), "description": about(key), "body": body})
        try:
            _, summary, data = check_draft(root, {**d, "why": "AI 学的规矩，你还没确认过"})
            proposals.append(("memory", summary, data["why"], data))
        except ValueError:
            continue
    from datetime import datetime
    from . import db as database
    from .timeutil import TZ
    db = database.open(root, "xinxiang")
    try:
        with db:   # proposals about the old rules no longer apply; the unconfirmed rules wait as drafts (what tidy.save writes)
            db.execute("UPDATE memory_updates SET status='superseded' WHERE status='pending' "
                       "AND kind IN ('merge_rules','retire_rule','rule_trigger')")
            db.executemany("INSERT INTO memory_updates (job, kind, content, why, data, status, created) VALUES ('migrate',?,?,?,?,'pending',?)",
                           [(kind, content, why, json.dumps(data, ensure_ascii=False, sort_keys=True),
                             datetime.now(TZ).isoformat(timespec="seconds")) for kind, content, why, data in proposals])
    finally:
        db.close()


def _dedupe(texts):
    """Section titles must be unique within a memory: a repeated one gets a number."""
    seen, out = {}, []
    for t in texts:
        title = t.split("\n", 1)[0]
        seen[title] = seen.get(title, 0) + 1
        out.append(t if seen[title] == 1 else t.replace(title, f"{title}（{seen[title]}）", 1))
    return out
