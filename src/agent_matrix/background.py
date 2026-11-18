"""Background pack: what an employee would look up before starting, gathered by code (no model).

For one item it collects, cheapest first:
  1. the task itself: goal, done criterion, the owner's additions and rejection notes;
  2. the meeting transcript around the promise (and the path to the full transcript);
  3. the chat around the message the item came from;
  4. Feishu documents whose titles match the subject;
  5. similar items, open or finished, with their deliverables;
  6. the item's own notebook and the previous draft.
The model reads this first and digs further itself (commands listed at the end) until it has enough.
"""
from datetime import datetime, timedelta
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


def _number(row):
    try:
        return int(str(row.get("编号")).strip())
    except (TypeError, ValueError):
        return None


def task_sheet(row, rows=()):
    """任务单.md: the order as the owner gave it. The owner's later words win over the title."""
    by_id = {r.get("record_id"): r for r in rows}
    parent = None
    for p in row.get("父事项") or []:
        pid = p.get("id") or p.get("record_id") if isinstance(p, dict) else p
        if pid in by_id:
            parent = by_id[pid]
    lines = [f"# #{_number(row)} {_one(row.get('标题'))}", ""]
    for label, key in [("要做什么", "下一步动作"), ("做到什么程度算完", "完成标准"), ("截止", "截止日"),
                       ("来源", "来源"), ("来源链接", "来源链接"), ("原话", "原话引用"), ("关联飞书任务", "关联任务链接")]:
        value = _one(row.get(key)).strip()
        if value:
            lines.append(f"- **{label}**：{value[:600]}")
    if parent:
        lines.append(f"- **这是为 #{_number(parent)}「{_one(parent.get('标题'))}」备的材料**，做出来要能直接拿去用。")
    if not _one(row.get("完成标准")).strip():
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


def transcript_excerpt(root, row):
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
        return "\n".join(lines[:TRANSCRIPT_BEFORE + TRANSCRIPT_AFTER])[:TRANSCRIPT_CAP], path
    start, end = max(0, anchor - TRANSCRIPT_BEFORE), min(len(lines), anchor + TRANSCRIPT_AFTER)
    text = "\n".join(lines[start:end])
    if len(text) > TRANSCRIPT_CAP:  # keep the promise in the middle when trimming
        mid_at = len("\n".join(lines[start:anchor]))
        text = text[max(0, mid_at - TRANSCRIPT_CAP // 2): mid_at + TRANSCRIPT_CAP // 2]
    return text, path


# ---- chat around the source message -----------------------------------------------------------------

def _chat_id(row):
    m = re.search(r"openChatId=(oc_[A-Za-z0-9]+)", _one(row.get("来源链接")))
    return m.group(1) if m else None


def chat_excerpt(client, row, now):
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
    start, end = at - timedelta(hours=CHAT_WINDOW_HOURS), min(now, at + timedelta(hours=CHAT_WINDOW_HOURS))
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
    return text[-CHAT_CAP:] if text else None


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


# ---- the pack ---------------------------------------------------------------------------------------

DIG_MORE = """## 不够就自己继续扒（只读，扒到够为止）

- 读飞书文档全文：`lark-cli docs +fetch --doc <链接或token> --doc-format markdown --as user`
- 搜文档：`lark-cli docs +search --query "<关键词>" --page-size 10 --as user --format json`
- 搜聊天记录：`lark-cli im +messages-search --query "<关键词>" --as user --format json`
- 读某个群一段时间的消息：`lark-cli im +chat-messages-list --chat-id <oc_…> --start <ISO时间> --end <ISO时间> --as user --format json`
- 读多维表格：`lark-cli base --help` 查看用法
- 其他飞书能力：`lark-cli --help`
"""


def build(root, client, rows, row, now, *, notes="", previous=None, skip_urls=(), copy_to=None):
    """Return the 背景.md text for one item. Every section is optional; missing ones are simply left out."""
    parts = ["# 背景（程序替你找好的，先读完再动手）", ""]
    links = [(k, u) for k, u in given_links(row, notes) if not any(x and x in u for x in skip_urls)]
    if links:
        parts += ["## 本人给过的链接（先读这些；里面有上一版就在它基础上改）",
                  *[f"- {u}（出自{k}）" for k, u in links[:10]], ""]
    excerpt, transcript = transcript_excerpt(root, row)
    if excerpt:
        where = str(transcript)
        if copy_to is not None:  # the model's folder gets its own copy of the full transcript
            import shutil
            shutil.copyfile(transcript, Path(copy_to) / "逐字稿.md")
            where = "逐字稿.md（当前文件夹）"
        parts += ["## 会议原话前后文", f"完整逐字稿：`{where}`", "", "```", excerpt, "```", ""]
    chat = chat_excerpt(client, row, now)
    if chat:
        parts += ["## 来源聊天前后文", "```", chat, "```", ""]
    known = {u for _, u in links}
    docs = [d for d in related_docs(client, _one(row.get("标题")), exclude=skip_urls) if d["url"] not in known]
    if docs:
        parts += ["## 可能相关的飞书文档（按标题相近程度排）", *[f"- {d['title']} {d['url']} {d['updated']}" for d in docs], ""]
    try:  # 记忆层: earlier meetings on the same topic (decisions, open questions, what it was waiting for)
        from .memory import recall_lines
        before = recall_lines(root, _one(row.get("标题")) + " " + _one(row.get("下一步动作")), limit=5,
                              exclude_source_id=_meeting_id(row))
    except Exception:
        before = []
    if before:
        parts += ["## 这个话题以前讨论过（按相关程度排）", *before, ""]
    similar = related_items(rows, row)
    if similar:
        parts += ["## 以前和现在的同类事项（做过的交付可以直接参考）", *similar, ""]
    if notes:
        parts += ["## 这件事的工作笔记（之前做到哪、卡在哪）", notes, ""]
    if previous:
        parts += ["## 上一版交付", f"`{previous}`：在它的基础上改，不要从头写。", ""]
    if len(parts) == 2:
        parts += ["程序没找到现成的背景。按下面的办法自己去扒，至少查一次文档和一次聊天再下结论。", ""]
    parts.append(DIG_MORE)
    return "\n".join(parts)


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
    by_chat = {}
    for m in candidates.get("messages") or []:
        by_chat.setdefault(m.get("chat_id"), []).append(m)
    for cid, msgs in by_chat.items():
        if not cid:
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
