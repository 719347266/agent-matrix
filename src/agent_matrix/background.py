"""Background pack: what an employee would look up before starting, gathered by code (no model).

For one item it collects, cheapest first:
  1. the task itself: goal, done criterion, the owner's additions and rejection notes;
  2. the meeting transcript around the promise (and the path to the full transcript);
  3. the chat around the message the item came from;
  4. Feishu documents whose titles match the subject;
  5. similar items, open or finished, with their deliverables;
  6. the item's own notebook and the previous draft;
  7. what the items before it delivered, and deliverables of the same kind the owner passed (pastwork).
The whole pack is kept under BUDGET characters (fit). The model reads this first and digs further itself
(commands listed at the end) until it has enough.
"""
from datetime import datetime, timedelta
import json
from pathlib import Path
import re

from .merge import similarity, subject
from .models import MatrixError

TRANSCRIPT_BEFORE, TRANSCRIPT_AFTER, TRANSCRIPT_CAP = 60, 40, 7000
CHAT_WINDOW_HOURS, CHAT_CAP = 3, 4000


def _one(value):
    if isinstance(value, list):
        if len(value) == 1:
            return _one(value[0])
        return "、".join(str(_one(x)) for x in value) if value else ""
    if isinstance(value, dict):
        return value.get("text") or value.get("name") or value.get("url") or value.get("id") or ""
    return "" if value is None else str(value)


def plain_quote(text):
    """Feishu's transcript marks a speaker as <cite … user-name="张三"></cite>: keep the name, drop the markup and the
    blank lines between turns, so what the AI reads is who said what."""
    text = re.sub(r'<cite[^>]*user-name="([^"]*)"[^>]*>\s*</cite>\s*', r"\1 ", str(text or ""))
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


SOURCE_KEYS = ("来源", "来源链接", "原话引用", "去重键")


def parent_of(row, rows):
    by_id = {r.get("record_id"): r for r in rows}
    for p in row.get("父事项") or []:
        pid = (p.get("id") or p.get("record_id")) if isinstance(p, dict) else p
        if pid in by_id:
            return by_id[pid]
    return None


def with_parent_source(row, rows):
    """A 备材料 made from another item has no source of its own (来源 = 手动): it works from its parent's meeting, chat
    and words, or it would start from nothing but a title and guess."""
    parent = parent_of(row, rows)
    if not parent or _one(row.get("来源链接")).strip() or _one(row.get("原话引用")).strip():
        return row
    return {**row, **{k: parent.get(k) for k in SOURCE_KEYS if parent.get(k)}}


def prep_means(parent):
    """What 备材料 is, said once the same way in the brief and the order."""
    return (f"这是为 #{_number(parent)}「{_one(parent.get('标题'))}」备的材料。那件事由本人亲自做；你备的是本人动手时要用的东西："
            "做这件事需要的信息和数据（每条带出处）、能直接改了用的草稿、本人要先决定的问题和你的建议。不替本人做决定，没有依据的内容不写。")


def circular(done_when):
    """「能直接用于：<标题>」 says nothing about when it is done."""
    return str(done_when or "").strip().startswith("能直接用于")


def _number(row):
    try:
        return int(str(row.get("编号")).strip())
    except (TypeError, ValueError):
        return None


def task_sheet(row, rows=()):
    """任务单.md: the order as the owner gave it. The owner's later words win over the title."""
    parent = parent_of(row, rows)
    row = with_parent_source(row, rows)
    lines = [f"# #{_number(row)} {_one(row.get('标题'))}", ""]
    for label, key in [("要做什么", "下一步动作"), ("做到什么程度算完", "完成标准"), ("截止", "截止日"),
                       ("来源", "来源"), ("来源链接", "来源链接"), ("原话", "原话引用"), ("关联飞书任务", "关联任务链接")]:
        value = _one(row.get(key)).strip()
        if key == "原话引用":
            value = plain_quote(value)
        if key == "完成标准" and circular(value):
            continue
        if value:
            lines.append(f"- **{label}**：{value[:600]}")
    if parent:
        lines.append(f"- **备材料**：{prep_means(parent)}")
    if not _one(row.get("完成标准")).strip() or circular(_one(row.get("完成标准"))):
        lines.append("- **做到什么程度算完**：没写。按标题和背景自己定一个合理的标准，写进交付的「我做的假设」。")
    extra = _one(row.get("补充材料")).strip()
    if extra:
        lines += ["", "## 本人补充（以这里为准，比标题新）", extra[-3000:]]
    rejected = _one(row.get("退回意见")).strip()
    if rejected:
        lines += ["", "## 本人退回意见（必须逐条改到）", rejected[-2000:]]
    return "\n".join(lines) + "\n"


# ---- meeting transcript ---------------------------------------------------------------------------

def _meeting_id(row):
    key = _one(row.get("去重键"))
    return key.split(":")[1] if key.startswith("meeting:") and key.count(":") >= 2 else None


def transcript_excerpt(root, row, before=TRANSCRIPT_BEFORE, after=TRANSCRIPT_AFTER, cap=TRANSCRIPT_CAP):
    """Lines around the promise in the meeting transcript, plus the full transcript path."""
    mid = _meeting_id(row)
    if not mid or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", mid):
        return None, None
    path = Path(root) / "state/meeting-transcripts" / mid / "transcript.md"
    if not path.is_file():
        return None, None
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    quote = _one(row.get("原话引用"))
    stamp = re.match(r"\[([^\]]+)\]\s*(.*)", quote)
    anchor = None
    if stamp:
        for i, line in enumerate(lines):
            if stamp.group(1) and stamp.group(1) in line:
                anchor = i
                break
        if anchor is None:
            words = re.sub(r"\s+", "", stamp.group(2))[:12]
            anchor = next((i for i, line in enumerate(lines) if words and words in re.sub(r"\s+", "", line)), None)
    if anchor is None:
        return plain_quote("\n".join(lines[:before + after]))[:cap], path
    start, end = max(0, anchor - before), min(len(lines), anchor + after)
    text = "\n".join(lines[start:end])
    if len(text) > cap:  # keep the promise in the middle when trimming
        mid_at = len("\n".join(lines[start:anchor]))
        text = text[max(0, mid_at - cap // 2): mid_at + cap // 2]
    return plain_quote(text), path


# ---- chat around the source message -----------------------------------------------------------------

def _chat_id(row):
    m = re.search(r"openChatId=(oc_[A-Za-z0-9]+)", _one(row.get("来源链接")))
    return m.group(1) if m else None


def _slack(client, link, start, end=None):
    """[(ts, "[MM-DD HH:MM who] text")] from the Slack conversation `link` points to, or None if it isn't Slack."""
    from .sources import slack
    parts = slack.link_parts(link)
    if not parts or client is None:
        return None
    lines = slack.history(client.config, parts[0], str(start.timestamp()), str(end.timestamp()) if end else None, thread=parts[2])
    return [(float(ts), f"[{datetime.fromtimestamp(float(ts), start.tzinfo):%m-%d %H:%M} {who}] {text[:400]}")
            for ts, who, text in lines]


def chat_excerpt(client, row, now, hours=CHAT_WINDOW_HOURS, cap=CHAT_CAP, latest=False):
    """The source chat from `hours` before the message to `hours` after it, or (latest) up to now."""
    sent = _sent_at(row, now)
    until = now if latest else sent + timedelta(hours=hours)
    if (slack_lines := _slack(client, _one(row.get("来源链接")), sent - timedelta(hours=hours), until)) is not None:
        return "\n".join(line for _, line in slack_lines)[-cap:] or None
    cid = _chat_id(row)
    if not cid or client is None:
        return None
    sent = re.search(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:[+-]\d{2}:\d{2})?)", _one(row.get("原话引用")))
    try:
        at = datetime.fromisoformat(sent.group(1).replace(" ", "T")) if sent else None
    except ValueError:
        at = None
    if at is None:
        at = now
    if at.tzinfo is None:
        at = at.replace(tzinfo=now.tzinfo)
    start, end = at - timedelta(hours=hours), now if latest else min(now, at + timedelta(hours=hours))
    try:
        got = client._exec(["im", "+chat-messages-list", "--chat-id", cid, "--start", start.isoformat(), "--end", end.isoformat(),
                            "--order", "asc", "--no-reactions", "--as", "user", "--format", "json"])
    except MatrixError:
        return None
    from .sources.chats import _text
    out = []
    for m in got.get("messages") or got.get("items") or []:
        text = _text(m)
        if not text:
            continue
        who = (m.get("sender") or {}).get("name") or (m.get("sender") or {}).get("id", "")[-6:]
        when = str(m.get("create_time") or m.get("sent_at") or "")[11:16]
        out.append(f"[{when} {who}] {text[:400]}")
    text = "\n".join(out)
    return text[-cap:] if text else None


# ---- documents and similar items --------------------------------------------------------------------

URL = re.compile(r"https?://[^\s<>()\[\]，。；、\"']+")


def given_links(row, notes=""):
    """Links the owner (or an earlier round) already pointed at: read these first."""
    seen, out = set(), []
    for key in ("交付物链接", "补充材料", "退回意见", "原话引用", "下一步动作", "关联任务链接"):
        for url in URL.findall(_one(row.get(key))):
            url = url.rstrip(".,")
            if url not in seen:
                seen.add(url); out.append((key, url))
    for url in URL.findall(notes or ""):
        url = url.rstrip(".,")
        if url not in seen:
            seen.add(url); out.append(("工作笔记", url))
    return out


def related_docs(client, title, limit=6, exclude=()):
    if client is None:
        return []
    core = subject(title)
    queries = list(dict.fromkeys(q for q in [core[:14], core[:8], core[:5]] if len(q) >= 2))
    seen, out = set(), []
    for q in queries:
        try:
            data = client._exec(["docs", "+search", "--query", q, "--page-size", "10", "--as", "user", "--format", "json"])
        except MatrixError:
            break
        for r in data.get("results") or []:
            meta = r.get("result_meta") or {}
            url = meta.get("url")
            name = re.sub(r"<[^>]+>", "", r.get("title_highlighted") or meta.get("title") or "")
            if not url or url in seen or any(x and x in url for x in exclude):
                continue
            seen.add(url)
            out.append((similarity(title, name), name, url, (meta.get("update_time_iso") or "")[:10]))
        if len(out) >= limit * 2:
            break
    out.sort(key=lambda x: x[3], reverse=True)   # newer first among equals
    out.sort(key=lambda x: -x[0])
    # Only titles that share real words with the task; unrelated hits are noise, not background.
    return [{"title": t, "url": u, "updated": d} for s, t, u, d in out if s >= 0.25][:limit]


def related_items(rows, row, limit=5):
    me = row.get("record_id")
    scored = []
    for r in rows:
        if r.get("record_id") == me or _number(r) is None:
            continue
        s = similarity(row.get("标题"), r.get("标题"))
        if s >= 0.35:
            scored.append((s, r))
    scored.sort(key=lambda x: -x[0])
    out = []
    for s, r in scored[:limit]:
        link = _one(r.get("交付物链接"))
        out.append(f"- #{_number(r)} {_one(r.get('标题'))}（{_one(r.get('状态'))}）" + (f" 交付：{link}" if link else ""))
    return out


def linked_items(root, rows, row):
    """(before, after): the items the owner strung on one line with this one, as lines for 背景.md."""
    from . import itemlinks
    by_id = {r.get("record_id"): r for r in rows}

    def line(rid):
        r = by_id.get(rid)
        if not r or _number(r) is None:
            return None
        link, summary = _one(r.get("交付物链接")), _one(r.get("预做结果摘要"))
        return (f"- #{_number(r)} {_one(r.get('标题'))}（{_one(r.get('状态'))}）" + (f" 交付：{link}" if link else "")
                + (f" 结论：{summary}" if summary else ""))
    me = row.get("record_id")
    before = [x for x in map(line, itemlinks.upstream(root, me)) if x]
    after = [x for x in map(line, itemlinks.downstream(root, me)) if x]
    return before, after


# ---- the pack ---------------------------------------------------------------------------------------

DIG_MORE = """## 不够就自己继续扒（只读，扒到够为止）

- 读飞书文档全文：`lark-cli docs +fetch --doc <链接或token> --doc-format markdown --as user`
- 搜文档：`lark-cli docs +search --query "<关键词>" --page-size 10 --as user --format json`
- 搜聊天记录：`lark-cli im +messages-search --query "<关键词>" --as user --format json`
- 读某个群一段时间的消息：`lark-cli im +chat-messages-list --chat-id <oc_…> --start <ISO时间> --end <ISO时间> --as user --format json`
- 读多维表格：`lark-cli base --help` 查看用法
- 其他飞书能力：`lark-cli --help`
"""


HEAD = "# 背景（程序替你找好的，先读完再动手）\n"
# ponytail: halved from 24000 once the AI could fetch the source itself (context); the pack is now only the fallback
BUDGET = 12000   # characters for the whole 背景.md; past it the least needed sections are cut first
# What survives when it is too long: the owner's own pointers and the line of work > examples > documents > old talk.
MUST, EXAMPLES, DOCS, PAST = 0, 1, 2, 3


def _close_fence(lines):
    return lines + ["```"] if sum(1 for x in lines if x.startswith("```")) % 2 else lines


def fit(sections, budget=BUDGET, fixed=0):
    """Join (priority, heading, body_lines) sections into one text of at most `budget` characters (plus `fixed`
    already spent elsewhere). Over budget, the lowest priority goes first (the later section first among equals):
    its body is cut at a line end and ends with a note saying how much was left out. Headings always stay."""
    blocks = [[p, h, "\n".join(body).split("\n")] for p, h, body in sections]

    def size(b):
        return len(b[1]) + 1 + len("\n".join(b[2])) + 2
    over = fixed + len(HEAD) + sum(size(b) for b in blocks) - budget
    order = sorted(range(len(blocks)), key=lambda i: (-blocks[i][0], -i))
    for b in (blocks[i] for i in order):
        if over <= 0:
            break
        body = "\n".join(b[2])
        if not body:
            continue
        note = "（太长，省略了 {} 字，需要时用下面的命令自己去读）"
        room = max(0, len(body) - over - len(note) - 12)
        kept, used = [], 0
        for line in b[2]:
            if used + len(line) + 1 > room:
                if room - used > 200:  # one long line (a pasted transcript): keep its start
                    kept.append(line[:room - used - 1])
                break
            kept.append(line)
            used += len(line) + 1
        kept = _close_fence(kept)
        cut = len(body) - len("\n".join(kept))
        b[2] = kept + [note.format(cut)]
        over -= len(body) - len("\n".join(b[2]))
    text = [HEAD]
    for _, heading, body in blocks:
        text += [heading, *body, ""]
    return "\n".join(text)


def build(root, client, rows, row, now, *, notes="", previous=None, skip_urls=(), copy_to=None, budget=BUDGET):
    """Return the 背景.md text for one item. Every section is optional; missing ones are simply left out."""
    sections = []   # (priority, heading, lines)
    if copy_to is not None:   # a pointer first: the AI fetches the source itself, as wide and as late as it turns out to need
        sections.append((MUST, "## 来源会话：自己去取（动手前取一次，交付或起草回复前再取一次）", source_pointer(root, row, copy_to)))
    links = [(k, u) for k, u in given_links(row, notes) if not any(x and x in u for x in skip_urls)]
    if links:
        sections.append((MUST, "## 本人给过的链接（先读这些；里面有上一版就在它基础上改）",
                         [f"- {u}（出自{k}）" for k, u in links[:10]]))
    try:  # the owner strung these together: this item continues the ones before it
        before, after = linked_items(root, rows, row)
    except Exception:
        before, after = [], []
    try:
        from .pastwork import upstream_results
        done_before = upstream_results(root, rows, row)
    except Exception:
        done_before = []
    if before or done_before:   # one list: what was delivered comes with its summary and copy, the rest as before
        rich, key = {}, None   # "#11" → that item's lines with its result (an entry may run over several lines)
        for ln in done_before:
            if ln.startswith("- #"):
                key = ln.split(" ", 2)[1]
            if key:
                rich.setdefault(key, []).append(ln)
        lines = []
        for ln in before:   # nearest first, as strung
            n = ln.split(" ", 2)[1] if ln.startswith("- #") else None
            lines += rich.pop(n) if n in rich else [ln]
        lines += [x for entry in rich.values() for x in entry]
        sections.append((MUST, "## 这件事接着前面这些事做（离得最近的在前；先读它们的交付，沿用里面的口径和结论，在它们的基础上往下做）", lines))
    if after:
        sections.append((MUST, "## 后面还有这些事要接着做（这次的成果要能让它们直接用）", after))
    excerpt, transcript = transcript_excerpt(root, row)
    if excerpt:
        where = str(transcript)
        if copy_to is not None:  # the model's folder gets its own copy of the full transcript
            import shutil
            shutil.copyfile(transcript, Path(copy_to) / "逐字稿.md")
            where = "逐字稿.md（当前文件夹）"
        sections.append((MUST, "## 会议原话前后文", [f"完整逐字稿：`{where}`", "", "```", excerpt, "```"]))
    chat = chat_excerpt(client, row, now)
    if chat:
        sections.append((MUST, "## 来源聊天前后文", ["```", chat, "```"]))
    try:
        from .pastwork import example_lines
        shown = example_lines(root, rows, row)
    except Exception:
        shown = []
    if shown:
        sections.append((EXAMPLES, "## 被通过的同类交付（范例）", shown))
    known = {u for _, u in links}
    docs = [d for d in related_docs(client, _one(row.get("标题")), exclude=skip_urls) if d["url"] not in known]
    if docs:
        sections.append((DOCS, "## 可能相关的飞书文档（按标题相近程度排）", [f"- {d['title']} {d['url']} {d['updated']}" for d in docs]))
    try:  # 记忆增强 (Hy Memory, when switched on): what it recalls about this item, asked through the listener
        from . import hymem
        if hymem.on(root):
            recalled = hymem.search_via_app(root, _one(row.get("标题")) + " " + _one(row.get("下一步动作")), limit=8)
            if recalled:
                sections.append((EXAMPLES, "## 记忆增强想起来的（Hy Memory 从以往会议、交付和评审里找的，供参考）", [f"- {x}" for x in recalled]))
    except Exception:
        pass
    try:  # 记忆层: earlier meetings on the same topic (decisions, open questions, what it was waiting for)
        from .memory import recall_lines
        talked = recall_lines(root, _one(row.get("标题")) + " " + _one(row.get("下一步动作")), limit=5,
                              exclude_source_id=_meeting_id(row))
    except Exception:
        talked = []
    if talked:
        sections.append((PAST, "## 这个话题以前讨论过（按相关程度排）", talked))
    similar = related_items(rows, row)
    if similar:
        sections.append((DOCS, "## 以前和现在的同类事项（做过的交付可以直接参考）", similar))
    if notes:
        sections.append((MUST, "## 这件事的工作笔记（之前做到哪、卡在哪）", notes.splitlines()))
    if previous:
        sections.append((MUST, "## 上一版交付", [f"`{previous}`：在它的基础上改，不要从头写。"]))
    if not sections:  # nothing found at all
        return HEAD + "\n程序没找到现成的背景。按下面的办法自己去扒，至少查一次文档和一次聊天再下结论。\n\n" + DIG_MORE
    return fit(sections, budget, fixed=len(DIG_MORE) + 1) + "\n" + DIG_MORE


# ---- the source conversation, fetched by the AI itself (xinxiang context) ---------------------------------

CONTEXT_MAX_HOURS = 168


def source_pointer(root, row, folder):
    """Save the item's source fields in its folder; return the lines telling the AI how to fetch the conversation."""
    source = Path(folder) / "来源.json"
    source.write_text(json.dumps(row, ensure_ascii=False, default=str), encoding="utf-8")
    from .workenv import shim_path   # installed by 心响 at start (drive.install_shim)
    cmd = shim_path()
    return [f"`\"{cmd}\" context --source \"{source}\" --hours 3`",
            "取回从原话前 3 小时到**现在**的来源会话：会议原话、来源聊天、之后的相关聊天。下面几节是程序按标题猜着摘的，"
            "可能过时、可能不全，以这条命令取回的为准；不够就按返回末尾的提示把 --hours 翻倍再取，取到够为止。"]


def context(root, client, row, now, hours=CHAT_WINDOW_HOURS):
    """The item's source conversation from `hours` before the promise up to now (not just around it): what the AI
    reads when the pack is not enough, and again before it acts, since someone may have done or answered it meanwhile."""
    hours = max(1, min(int(hours), CONTEXT_MAX_HOURS))
    scale = min(8, max(1, hours // CHAT_WINDOW_HOURS))
    out = [f"# 来源会话（原话前 {hours} 小时 → 现在 {now:%m-%d %H:%M}）", ""]
    excerpt, path = transcript_excerpt(root, row, before=TRANSCRIPT_BEFORE * scale, after=TRANSCRIPT_BEFORE * scale,
                                       cap=TRANSCRIPT_CAP * scale)
    if excerpt:
        out += [f"## 会议原话前后文（完整逐字稿：`{path}`）", "```", excerpt, "```", ""]
    chat = chat_excerpt(client, row, now, hours=hours, cap=CHAT_CAP * scale, latest=True)
    if chat:
        out += ["## 来源聊天（一直到现在）", "```", chat, "```", ""]
    else:   # a meeting, a task, a ticket: what was said about it in chats since
        later = after_context(client, row, now, limit=15 * scale)
        if later:
            out += ["## 之后的相关聊天（按标题搜到的，不一定都相关）", *later, ""]
    if len(out) == 2:
        out += ["这件事取不回来源会话（来源不是会议或聊天，或者读不到）。按 背景.md 末尾的命令用关键词搜。", ""]
    out.append(f"不够：把 --hours 改成 {min(hours * 2, CONTEXT_MAX_HOURS)} 再取一次。" if hours < CONTEXT_MAX_HOURS
               else "已经取到上限（7 天），再往前用关键词搜。")
    return "\n".join(out)


# ---- what happened after the item was picked up (for judging new items) ---------------------------------

def _sent_at(row, now):
    sent = re.search(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:[+-]\d{2}:\d{2})?)", _one(row.get("原话引用")))
    for text in ([sent.group(1)] if sent else []) + [_one(row.get("创建时间"))]:
        try:
            at = datetime.fromisoformat(text.replace(" ", "T"))
            return at if at.tzinfo else at.replace(tzinfo=now.tzinfo)
        except (ValueError, AttributeError):
            continue
    return now - timedelta(days=2)


def after_context(client, row, now, limit=15):
    """Messages after the promise: the chat it came from, or a search of all chats since it was picked up.

    Lets the judge see that the owner already did it, the other side said never mind, or it was settled."""
    if client is None:
        return []
    from .sources.chats import _text
    start = _sent_at(row, now)
    from .sources import slack
    if parts := slack.link_parts(_one(row.get("来源链接"))):  # Slack: everything after the message itself
        said = float(parts[1])
        return [line[:230] for ts, line in _slack(client, _one(row.get("来源链接")), datetime.fromtimestamp(said - 1, now.tzinfo))
                if ts > said][:limit]
    cid = _chat_id(row)
    try:
        if cid:
            got = client._exec(["im", "+chat-messages-list", "--chat-id", cid, "--start", start.isoformat(), "--end", now.isoformat(),
                                "--order", "asc", "--no-reactions", "--as", "user", "--format", "json"])
        else:
            words = subject(_one(row.get("标题")))[:8]
            if len(words) < 2:
                return []
            got = client._exec(["im", "+messages-search", "--query", words, "--start", start.isoformat(), "--end", now.isoformat(),
                                "--page-size", "10", "--no-reactions", "--as", "user", "--format", "json"])
    except MatrixError:
        return []
    out = []
    for m in (got.get("messages") or got.get("items") or got.get("data", {}).get("messages") or [])[:limit]:
        text = _text(m) if isinstance(m, dict) else ""
        if not text:
            continue
        sender = m.get("sender") or {}
        who = sender.get("name") or ("本人" if sender.get("id") == getattr(client.config, "owner_open_id", None) else sender.get("id", "")[-6:])
        where = "" if cid else f"{m.get('chat_name') or ''} "
        out.append(f"[{str(m.get('create_time') or '')[5:16]} {where}{who}] {text[:200]}")
    return out


def _stamp(value):
    """'2026-11-17T15:02:11+08:00' / '2026-11-17 15:02' → '2026-11-17 15:02:11' for ordering."""
    return str(value or "").replace("T", " ")[:19]


def add_followups(client, candidates, now, per_message=10):
    """For chat extraction: attach the messages that came after each candidate (one fetch per chat), so the
    model sees when it was already settled ("已发""不用了") instead of turning it into a todo."""
    if client is None:
        return candidates
    from .sources.chats import _text
    from .sources import slack
    by_chat = {}
    for m in candidates.get("messages") or []:
        by_chat.setdefault(m.get("chat_id"), []).append(m)
    for cid, msgs in by_chat.items():
        if not cid or any(str(m.get("message_id") or "").startswith("wechat:") for m in msgs):
            continue   # WeChat: not read again here (slow); the two lines before each message are already attached
        slack_msgs = [(m, slack.link_parts(str(m.get("message_id")))) for m in msgs]
        if any(parts for _, parts in slack_msgs):
            later = {}  # one read per thread (None = the channel itself, with its threads)
            for m, parts in slack_msgs:
                if not parts:
                    continue
                if parts[2] not in later:
                    first = min(float(p[1]) for _, p in slack_msgs if p and p[2] == parts[2])
                    later[parts[2]] = _slack(client, m["message_id"], datetime.fromtimestamp(first - 1, now.tzinfo)) or []
                m["after"] = [line for ts, line in later[parts[2]] if ts > float(parts[1])][:per_message]
            continue
        start = min(_stamp(m.get("sent_at")) for m in msgs) or _stamp((now - timedelta(days=1)).isoformat())
        try:
            got = client._exec(["im", "+chat-messages-list", "--chat-id", cid, "--start", start.replace(" ", "T") + "+08:00"
                                if "+" not in start else start, "--end", now.isoformat(), "--order", "asc", "--no-reactions",
                                "--as", "user", "--format", "json"])
        except MatrixError:
            continue
        later = []
        for x in got.get("messages") or got.get("items") or []:
            text = _text(x)
            if text:
                sender = x.get("sender") or {}
                later.append((_stamp(x.get("create_time")), x.get("message_id"), sender.get("name") or sender.get("id", "")[-6:], text[:200]))
        for m in msgs:
            after = [f"[{t[5:16]} {who}] {text}" for t, mid, who, text in later if t >= _stamp(m.get("sent_at")) and mid != m.get("message_id")]
            m["after"] = after[:per_message]
    return candidates
