"""心响 · 事件简报：一件事交给 AI 时，先由代码把它写成一份独立、完整的简报，AI 拿到就能做，不用自己再去翻。

The brief stands on its own: what to do and by when, how it came up (who asked, in which meeting or chat), what
was already settled there (the meeting's own decisions and requirements, each with who said it), the owner's later
words (they win over the title), where the work stands (the last version, the AI's open question, the owner's
answer) and what to do next. Raw material (the whole transcript, the earlier version) is listed at the end, only
for checking. Built in code from what 心响 already keeps; no model call.

    build(root, record_id)   the brief as Markdown; written to 事件.md and put first in every message to an AI
"""
from datetime import datetime
from pathlib import Path
import re

from .background import _number, _one

CITE = re.compile(r'<cite[^>]*?user-name="([^"]*)"[^>]*></cite>\s*(\d{1,2}:\d{2}:\d{2})?\s*', re.S)
STAMP = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})")
MEETING = re.compile(r"meetingId=(\d{10,30})")
CLOSE_SECONDS = 5 * 60      # a meeting's decisions this close to the promise belong to the same piece of work
NEAR_SECONDS = 15 * 60      # a little further away: only when they are about the same thing
KINDS = {"decision": "定了", "requirement": "要求", "open_question": "没定", "waiting": "等条件"}


def _seconds(stamp):
    m = STAMP.search(str(stamp or ""))
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) if m else None


def said_lines(raw, limit=1600):
    """A transcript excerpt as "谁 时间：话" lines: speakers kept, markup and empty lines gone."""
    text = str(raw or "")
    out = []
    parts = CITE.split(text)
    if len(parts) == 1:   # no speaker markup: just tidy the text
        lines = [" ".join(l.split()) for l in text.splitlines() if l.strip()]
        return "\n".join(lines)[:limit]
    for i in range(1, len(parts) - 2, 3):
        who, stamp, words = parts[i], parts[i + 1], " ".join(parts[i + 2].split())
        words = re.sub(r"^\[\d{1,2}:\d{2}:\d{2}\]\s*", "", words)
        if words:
            out.append(f"{who}（{stamp}）：{words}" if stamp else f"{who}：{words}")
    return "\n".join(out)[:limit]


def _speakers(root, meeting_id):
    """[(seconds, who)] from the saved transcript of one meeting, in order."""
    path = Path(root) / "state/meeting-transcripts" / str(meeting_id) / "transcript.md"
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    return [(s, who) for who, stamp in re.findall(r'user-name="([^"]*)"[^>]*></cite>\s*(\d{1,2}:\d{2}:\d{2})', text)
            if (s := _seconds(stamp)) is not None]


def _who_at(speakers, stamp):
    at = _seconds(stamp)
    if at is None or not speakers:
        return ""
    before = [who for s, who in speakers if s <= at]
    return before[-1] if before else speakers[0][1]


def _row(root, record_id, rows=None):
    if rows is None:
        from .snapshot import _snapshot
        rows = _snapshot(Path(root)).get("rows") or []
    return next((r for r in rows if r.get("record_id") == record_id), None), rows


def _owner(root):
    try:
        from .config import load
        return load(Path(root) / "config.yaml").owner_name or "本人"
    except Exception:
        return "本人"


def _settled(root, meeting_id, around, topic=""):
    """What that meeting settled about this work: its decisions, requirements, open questions near the promise (close
    by, or a little further when about the same thing), each with who said it."""
    from . import memory
    from .textmatch import similarity
    if not meeting_id:
        return []
    speakers = _speakers(root, meeting_id)
    out = []
    for m in memory.moments(Path(root), limit=800):
        if str(m.get("source_id")) != str(meeting_id) or m.get("status") == "stale" or m.get("kind") not in KINDS:
            continue
        at = _seconds(m.get("stamp"))
        if around is not None and at is not None:
            gap = abs(at - around)
            if gap > NEAR_SECONDS or (gap > CLOSE_SECONDS and max(similarity(topic, m["text"]), similarity(m["text"], topic)) < 0.3):
                continue
        who = _who_at(speakers, m.get("stamp"))
        label = KINDS[m["kind"]]
        out.append((at if at is not None else 10 ** 9,
                    f"- {label}：{m['text']}" + (f"（{who} {m.get('stamp')}）" if who else f"（{m.get('stamp')}）" if m.get("stamp") else "")))
    return [line for _, line in sorted(out)]


EXPERIENCE_CAP, EARLIER_MAX = 4000, 6


def _experience(root, topic):
    """The owner's experience that bears on this work, in full, picked in code so it is not left to the AI to go and
    look: every section of the general file (it is read before any work), and the sections of the other files whose
    title or description is about the same thing. What does not fit stays in the index (记忆/目录.md)."""
    from . import library
    from .textmatch import similarity
    picked = []
    for e in library.entries(Path(root)):
        general = e["name"] == library.GENERAL
        for title, text in e["sections"]:
            score = 2.0 if general else max(similarity(topic, f"{title} {e['description']}"), similarity(title, topic))
            if general or score >= 0.3:
                picked.append((score, e["name"], text))
    by_file, used = {}, 0
    for _, name, text in sorted(picked, key=lambda x: -x[0]):
        if used + len(text) > EXPERIENCE_CAP:
            break
        by_file.setdefault(name, []).append(re.sub(r"^## ", "### ", text, flags=re.M))
        used += len(text)
    return [f"**{name}**\n\n" + "\n\n".join(texts) for name, texts in by_file.items()]


def _earlier(root, meeting_id, topic):
    """What earlier meetings settled about the same thing (this meeting's own is in 会上已经定下的): decisions,
    requirements and what was left waiting, still standing, closest first."""
    from . import memory
    from .textmatch import similarity
    found = []
    for m in memory.moments(Path(root), limit=800):
        if meeting_id and str(m.get("source_id")) == str(meeting_id):
            continue
        if m.get("status") == "stale" or m.get("kind") not in KINDS:
            continue
        score = max(similarity(topic, m["text"]), similarity(m["text"], topic))
        if score >= 0.35:
            found.append((score, f"- {KINDS[m['kind']]}：{m['text']}（{m.get('source_title') or '会议'} {str(m.get('at') or '')[5:10]}）"))
    return [line for _, line in sorted(found, key=lambda x: -x[0])[:EARLIER_MAX]]


def _later_words(row, messages):
    """The owner's words after the item came in, oldest first: notes, answers, change requests. These win."""
    out = []
    for m in messages:
        if m.get("from") == "me" and m.get("kind") not in {"closed"} and str(m.get("text") or "").strip():
            out.append(f"- {m['at']}：{' '.join(str(m['text']).split())[:600]}")
    extra = _one(row.get("补充材料")).strip()
    if extra and not out:
        out += [f"- {line.strip()}" for line in extra.splitlines() if line.strip()][-10:]
    rejected = _one(row.get("退回意见")).strip()
    if rejected:
        out.append(f"- 退回意见（必须逐条改到）：{' '.join(rejected.split())[:1500]}")
    return out


def _state(row, history):
    """(where it stands, what to do next) from the versions, the AI's last question and the owner's words after it."""
    messages, versions = history.get("messages", []), history.get("deliverables", [])
    status = _one(row.get("状态"))
    last = versions[-1] if versions else None
    asked = next((m for m in reversed(messages) if m.get("from") == "ai" and m.get("kind") == "ask"), None)
    since = (last or {}).get("at") or ""
    mine = [m for m in messages if m.get("from") == "me" and m.get("kind") != "closed"]
    newer = [m for m in mine if m.get("at", "") > since]
    answered = bool(asked) and any(m.get("at", "") > asked.get("at", "") for m in mine)
    lines = [f"- 状态：{status or '未开始'}"]
    if last:
        lines.append(f"- 已交第 {last['v']} 版（{last['at']}）：{last.get('summary') or '见交付'}" + (f"　{last['url']}" if last.get("url") else ""))
        if last.get("confirm"):
            lines.append(f"- 当时请本人拍板：{last['confirm']}")
    if asked:
        lines.append(f"- AI 上次卡住时问：{' '.join(str(asked['text']).split())[:400]}" + ("（本人已在后面回答）" if answered else "（还没回答）"))
    if _one(row.get("退回意见")).strip():
        nxt = "按退回意见逐条改，在上一版基础上出新版本。"
    elif newer and last:
        nxt = "按本人在上一版之后说的话改，在上一版基础上出新版本；没提到的部分保持不变。"
    elif asked and answered and any(m.get("at", "") > asked.get("at", "") for m in newer):
        nxt = "照本人的回答接着做完，交出新版本。"
    elif last and status in {"待验收", "完成"}:
        nxt = "已经交过，等本人验收：没有新要求就不要重做。"
    else:
        nxt = "从头做：先按上面「已经定下的」和本人的要求出第一版。"
    return lines, nxt


def build(root, record_id, rows=None):
    """The whole brief for one item, as Markdown. Empty string when the item is not found."""
    from . import thread as conversation
    row, rows = _row(root, record_id, rows)
    if not row:
        return ""
    from .background import circular, parent_of, prep_means, with_parent_source
    parent = parent_of(row, rows)
    n, title = _number(row), _one(row.get("标题")).strip()
    src = with_parent_source(row, rows)   # a 备材料 works from its parent's meeting, chat and words
    history = conversation.history(Path(root), [record_id]).get(record_id, {"messages": [], "deliverables": []})
    owner = _owner(root)
    link = _one(src.get("来源链接"))
    meeting = MEETING.search(link)
    meeting_id = meeting.group(1) if meeting else None
    raw_quote = _one(src.get("原话引用"))
    around = _seconds(raw_quote)
    lines = [f"# 事件 #{n} {title}", "",
             f"心响整理好的简报：这件事要的东西都在下面，照它做，不用再去翻。「本人」指 {owner}，你替本人做这件事。", "",
             "## 要做什么"]
    lines.append(f"- {(_one(row.get('下一步动作')) or title).strip()}")
    for label, key in (("做到什么程度算完", "完成标准"), ("截止", "截止日")):
        value = _one(row.get(key)).strip()
        if key == "完成标准" and circular(value):
            continue
        if value:
            lines.append(f"- {label}：{value[:10] if key == '截止日' else value}")
    if parent:
        lines.append(f"- {prep_means(parent)}")
        if _one(parent.get("下一步动作")).strip():
            lines.append(f"- 那件事要做的：{_one(parent.get('下一步动作')).strip()}")
    source = _one(src.get("来源")) or "手动添加"
    lines += ["", "## 来龙去脉", f"- 来源：{source}" + (f"（{_meeting_title(root, meeting_id)}）" if meeting_id and _meeting_title(root, meeting_id) else "")
              + (f"　{_url(link)}" if _url(link) else "")]
    created = _one(row.get("创建时间"))[:16].replace("T", " ")
    if created:
        lines.append(f"- 收进心响：{created}")
    settled = _settled(root, meeting_id, around, f"{title} {_one(row.get('下一步动作'))}")
    if settled:
        lines += ["", "## 会上已经定下的（以这些为准，不要另起炉灶）", *settled]
    topic = f"{title} {_one(row.get('下一步动作'))} {_one(parent.get('标题')) if parent else ''}"
    earlier = _earlier(root, meeting_id, topic)
    if earlier:
        lines += ["", "## 以前的会上定过的相关事（没被推翻，照用；和本次冲突时以本次为准）", *earlier]
    quote = said_lines(raw_quote)
    if quote:
        lines += ["", "## 原话（谁说的都保留）", quote]
    later = _later_words(row, history.get("messages", []))
    if later:
        lines += ["", "## 本人后来的要求（比上面都新，冲突时以这里为准）", *later]
    experience = _experience(root, topic)
    if experience:
        lines += ["", "## 你的经验里和这件事相关的（本人审过的做法，照着做；交付时在结果的 memory 里写用了哪几条）", *experience]
    state, nxt = _state(row, history)
    lines += ["", "## 做到哪了", *state, "", "## 接下来", f"- {nxt}"]
    lines += ["", "## 原始材料（只在需要核对时再看）",
              "- 任务单.md、背景.md：程序找的背景和相关事项"
              + ("；逐字稿.md：整场会的逐字稿" if meeting_id else "") + "；上一版.md：上一次交付（有的话）"]
    return "\n".join(lines) + "\n"


def _url(text):
    m = re.search(r"https?://[^\s)\]]+", str(text or ""))
    return m.group(0) if m else ""


def _meeting_title(root, meeting_id):
    """The meeting's topic and date, from the queue the listener keeps."""
    import json
    import sqlite3
    from . import db as database
    try:
        conn = database.read(Path(root) / "state/state.db")
        try:
            row = conn.execute("SELECT metadata FROM meeting_queue WHERE meeting_id=?", (str(meeting_id),)).fetchone()
        finally:
            conn.close()
        meta = json.loads(row[0]) if row else {}
    except (sqlite3.Error, ValueError):
        return ""
    topic, start = meta.get("topic") or "", str(meta.get("start_time") or "")[:16]
    return f"会议「{topic}」{start}" if topic else ""
