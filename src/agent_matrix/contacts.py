"""通讯录: everyone the owner deals with, colleagues and clients, found in what 心响 already keeps locally (the
chats it read, the meeting transcripts, the items), with what 心响 understands about each one, kept so the work can
use it ("who is asking, what do they care about, how do they like things").

    directory(root, self_names)   [{name, kind, guessed, messages, chats, meetings, last, understood, note}]
    meetings(root)                [{id, topic, at, url, people}], newest first
    understanding / note          the AI's text (state/people/<name>.md) and the owner's own words (the owner's win)
    due(root, people, now)        who is due a (re)write: enough dealings, never written, or much new since, or asked
    evidence(root, name, rows)    what the AI reads to write it: their words in chats and meetings, the items with them
    accept(root, name, text, now) keep the AI's text if it holds up

Nothing here calls Feishu or any other service: it only reads the local records.
"""
from datetime import datetime, timedelta
from pathlib import Path
import json
import re

from . import db as database

FOLDER = "state/people"
META, NOTES, ASKED = "people:meta", "people:notes", "people:asked"
SECTIONS = ("是谁", "在意什么", "怎么跟TA打交道", "最近和本人在做的事")
MIN_DEALINGS = 5          # a person needs this much (a message counts 1, a meeting together 3) before 心响 writes about them
NEW_DEALINGS = 6          # and this much new since the last time before it writes again
REWRITE_AFTER = timedelta(days=3)
RETRY_AFTER = timedelta(hours=6)   # a round that did not come back: the same person is tried again after this long
PER_ROUND = 3
# 「<cite user-name="X"></cite> 00:01:02」 is X speaking; 「说话人 1 用 <cite user-name="X"></cite> 的设备」 is someone in
# the room on X's device: X was there, but the words are not X's
CITE = re.compile(r'(说话人\s*\d+\s*用\s*)?<cite[^>]*user-name="([^"]+)"[^>]*>\s*</cite>(\s*的设备)?\s*(?:\d{2}:\d{2}:\d{2})?')
_TRANSCRIPTS = {}   # path -> (mtime, [(speaker, words)])


def _one(value):
    return (value[0] if value else "") if isinstance(value, list) else (value or "")


def _name(value):
    return " ".join(str(value or "").split())[:60]


def _file(root, name):
    safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", _name(name)).strip(". ") or "_"
    return Path(root) / FOLDER / f"{safe}.md"


def self_names(root):
    """The owner's own names: never a contact."""
    try:
        from .config import load
        cfg = load(Path(root) / "config.yaml")
        return {"我", "本人", cfg.owner_name, *cfg.owner_aliases} - {"", None}
    except Exception:
        return {"我", "本人"}


def file_name(name):
    return _file(".", name).stem


def _get(root, key):
    from .thread import setting
    value = setting(Path(root), key, {}) or {}
    return value if isinstance(value, dict) else {}


def _put(root, key, value):
    from .thread import set_setting
    set_setting(Path(root), key, value)


# ---- what is known locally -------------------------------------------------------------------------

def _chat_rows(root):
    try:
        db = database.read(Path(root) / "state/state.db")
    except Exception:
        return [], set()
    try:
        rows = db.execute("SELECT chat_id, chat_name, chat_mode, sender_name, sent_at, text FROM chat_candidates").fetchall()
        row = db.execute("SELECT value FROM kv WHERE key='chats:external'").fetchone()
        external = set(json.loads(row[0])) if row else set()
        return rows, external
    except Exception:
        return [], set()
    finally:
        db.close()


def meetings(root):
    """The meetings 心响 has a transcript of, newest first, with who spoke."""
    try:
        db = database.read(Path(root) / "state/state.db")
        try:
            meta = {str(mid): json.loads(m or "{}") for mid, m in db.execute("SELECT meeting_id, metadata FROM meeting_queue")}
        finally:
            db.close()
    except Exception:
        meta = {}
    out = []
    base = Path(root) / "state/meeting-transcripts"
    for path in base.glob("*/transcript.md") if base.is_dir() else ():
        mid = path.parent.name
        m = meta.get(mid) or {}
        speakers = []
        for who, _ in _speech(path):
            if who and who not in speakers:
                speakers.append(who)
        topic = re.sub(r"^\d{1,2}-\d{1,2}\s*\|\s*", "", str(m.get("topic") or "")).strip() or "会议"
        out.append({"id": mid, "topic": topic, "at": str(m.get("start_time") or "")[:16], "url": str(m.get("source_url") or ""),
                    "people": speakers})
    return sorted(out, key=lambda x: x["at"], reverse=True)


def _speech(path):
    """[(speaker, words)] of one transcript, read once per change of the file. Words said on someone's device by an
    unnamed speaker come with speaker "" after the device's owner with no words (they were there)."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    hit = _TRANSCRIPTS.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    text = path.read_text(encoding="utf-8", errors="ignore")
    marks, out = list(CITE.finditer(text)), []
    for k, m in enumerate(marks):
        end = marks[k + 1].start() if k + 1 < len(marks) else len(text)
        words = " ".join(re.sub(r"说话人\s*\d+\s*用\s*$", "", text[m.end():end].strip()).split())
        who = _name(m.group(2))
        if m.group(1) or m.group(3):
            out += [(who, ""), ("", words)]
        elif words:
            out.append((who, words))
    _TRANSCRIPTS[str(path)] = (mtime, out)
    return out


def speaker_at(root, meeting_id, quote):
    """Who said the words an item was taken from in a meeting: the quote starts with the time ("[00:01:32] …"), the
    transcript has the speaker just before that time. "" when it was someone on another's device, or not found."""
    at = re.match(r"\[?(\d{2}:\d{2}:\d{2})", str(quote or "").strip())
    if not at or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", str(meeting_id or "")):
        return ""
    path = Path(root) / "state/meeting-transcripts" / meeting_id / "transcript.md"
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    where = text.find(at.group(1))
    marks = [m for m in CITE.finditer(text[:where + 9])] if where >= 0 else []
    if not marks or marks[-1].group(1) or marks[-1].group(3):
        return ""
    return _name(marks[-1].group(2))


def directory(root, self_names, extra=()):
    """Everyone the owner dealt with. extra: names the items know of (an item's chat partner) with no chat kept."""
    from . import people
    tags, notes, meta = people.tags(root), _get(root, NOTES), _get(root, META)
    found = {}

    def one(name):
        name = _name(name)
        if not name or name in self_names:
            return None
        return found.setdefault(name, {"name": name, "messages": 0, "chats": [], "meetings": [], "last": "", "client": False})

    rows, external = _chat_rows(root)
    for cid, chat, mode, sender, at, _ in rows:
        mine = _name(sender) in self_names
        p = one(chat) if (mode == "p2p" and mine) else (None if mine else one(sender))
        if not p:
            continue
        p["messages"] += 1
        p["last"] = max(p["last"], str(at or "")[:16])
        if mode != "p2p" and chat and chat not in p["chats"]:
            p["chats"].append(chat)
        if cid in external:
            p["client"] = True
    for m in meetings(root):
        for who in m["people"]:
            p = one(who)
            if p:
                p["meetings"].append(m["id"])
                p["last"] = max(p["last"], m["at"])
    for name in extra:
        one(name)
    out = []
    for p in found.values():
        tag = tags.get(p["name"])
        kind = tag if tag in people.KINDS else ("客户" if p["client"] else "同事")
        m = meta.get(p["name"]) or {}
        out.append({"name": p["name"], "kind": kind, "guessed": tag not in people.KINDS, "messages": p["messages"],
                    "chats": p["chats"][:8], "meetings": p["meetings"], "last": p["last"],
                    "understood": m.get("at") if _file(root, p["name"]).is_file() else None,
                    "note": str(notes.get(p["name"]) or "")})
    return sorted(out, key=lambda x: x["last"], reverse=True)


def dealings(person):
    return int(person.get("messages") or 0) + 3 * len(person.get("meetings") or ())


# ---- what 心响 understands ---------------------------------------------------------------------------

def understanding(root, name):
    try:
        return _file(root, name).read_text(encoding="utf-8")
    except OSError:
        return ""


def note(root, name):
    return str(_get(root, NOTES).get(_name(name)) or "")


def set_note(root, name, text):
    """The owner's own words about a person (their role, what to watch out for). They win over the AI's."""
    name, text = _name(name), str(text or "").strip()[:1000]
    if not name:
        raise ValueError("要写是谁。")
    notes = _get(root, NOTES)
    if text:
        notes[name] = text
    else:
        notes.pop(name, None)
    _put(root, NOTES, notes)
    return text


def ask(root, name, now):
    """The owner wants it written (again) now: the next round takes this person first."""
    name = _name(name)
    if not name:
        raise ValueError("要写是谁。")
    asked = _get(root, ASKED)
    asked[name] = now.isoformat(timespec="seconds")
    _put(root, ASKED, asked)


def _when(value):
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def due(root, people, now):
    """Who to write about this round, at most PER_ROUND: asked by the owner first, then the most dealt-with."""
    meta, asked = _get(root, META), _get(root, ASKED)
    picked = []
    for p in people:
        m, score = meta.get(p["name"]) or {}, dealings(p)
        tried = _when(m.get("tried"))
        if tried and now - tried < RETRY_AFTER and not (p["name"] in asked and (_when(asked[p["name"]]) or now) > tried):
            continue
        if p["name"] in asked:
            picked.append((0, -score, p))
            continue
        if score < MIN_DEALINGS:
            continue
        written = _when(m.get("at")) if _file(root, p["name"]).is_file() else None
        if not written:
            picked.append((1, -score, p))
        elif score - int(m.get("score") or 0) >= NEW_DEALINGS and now - written >= REWRITE_AFTER:
            picked.append((2, -score, p))
    return [p for *_, p in sorted(picked, key=lambda x: x[:2])][:PER_ROUND]


def mark_tried(root, names, now):
    meta = _get(root, META)
    for name in names:
        meta.setdefault(name, {})["tried"] = now.isoformat(timespec="seconds")
    _put(root, META, meta)


def evidence(root, person, rows=(), cap=9000):
    """What the AI reads about one person: their words in chats, what they said in meetings, the items with them,
    what 心响 wrote last time and the owner's own words. Newest kept when it is too long."""
    name = person["name"]
    chats = []
    for cid, chat, mode, sender, at, text in _chat_rows(root)[0]:
        if _name(sender) == name or (mode == "p2p" and _name(chat) == name):
            where = "私聊" if mode == "p2p" else f"群「{chat}」"
            chats.append(f"[{str(at)[:16]} {where} {sender}] {' '.join(str(text or '').split())[:300]}")
    chats = sorted(chats)[-60:]
    said = []
    by_id = {m["id"]: m for m in meetings(root)}
    for mid in person.get("meetings", [])[:8]:
        m = by_id.get(mid)
        path = Path(root) / "state/meeting-transcripts" / mid / "transcript.md"
        words = " / ".join(w for who, w in _speech(path) if who == name and w)
        if m and words:
            said.append(f"### {m['at'][:10]} {m['topic']}\n{words[:1200]}")
    items = []
    for r in rows:
        text = f"{_one(r.get('标题'))} {_one(r.get('原话引用'))} {_one(r.get('下一步动作'))}"
        if name in text:
            items.append(f"- #{_one(r.get('编号'))} {_one(r.get('标题'))}（{_one(r.get('状态'))}）")
    parts = [f"# {name}", f"- 心响的分组：{person.get('kind') or '同事'}" + ("（本人标的）" if not person.get("guessed", True) else "（程序猜的）")]
    if person.get("note"):
        parts += ["", "## 本人写的（以这里为准）", person["note"]]
    old = understanding(root, name)
    if old:
        parts += ["", "## 上一次写的理解（有新材料就更新，没被推翻的保留）", old]
    if items:
        parts += ["", "## 和TA有关的事项", *items[-15:]]
    if said:
        parts += ["", "## TA在会上说的", *said]
    if chats:
        parts += ["", "## TA在聊天里说的（和本人私聊里双方的话都在）", *chats]
    text = "\n".join(parts)
    return text if len(text) <= cap else text[:cap // 3] + "\n…（中间略）…\n" + text[-(cap * 2 // 3):]


def _plain(text):
    return re.sub(r"[#>*`\-\s|]", "", str(text or ""))


def check(text):
    """None when the text holds up, else what is wrong with it (said back to the AI)."""
    text = str(text or "").strip()
    heads = re.findall(r"^##\s*(.+?)\s*$", text, re.M)
    if "是谁" not in heads:
        return "要有「## 是谁」一节。"
    if len([h for h in heads if h in SECTIONS]) < 2:
        return f"至少写两节：{'、'.join(SECTIONS)}。材料里看不出来的节可以不写。"
    if not 40 <= len(_plain(text)) <= 2500:
        return "正文要在 40 到 2500 字之间。"
    return None


def accept(root, person, text, now):
    """Keep what the AI wrote about one person. Returns None when kept, else the problem."""
    problem = check(text)
    if problem:
        return problem
    name = person["name"]
    path = _file(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = re.sub(r"^#\s+.*\n+", "", str(text).strip())   # the name heading is ours
    path.write_text(f"# {name}\n\n{body}\n", encoding="utf-8")
    meta = _get(root, META)
    meta[name] = {"at": now.isoformat(timespec="seconds"), "score": dealings(person)}
    _put(root, META, meta)
    asked = _get(root, ASKED)
    if asked.pop(name, None):
        _put(root, ASKED, asked)
    return None


def about(root, names, cap=1500):
    """For the work: what 心响 understands about these people, with the owner's words first. [] when nothing."""
    out = []
    for name in dict.fromkeys(_name(n) for n in names if n):
        own, text = note(root, name), understanding(root, name)
        if not own and not text:
            continue
        body = re.sub(r"^#\s+.*\n+", "", text.strip())
        body = re.sub(r"^##\s+", "#### ", body, flags=re.M)
        out.append(f"### {name}" + (f"\n本人写的：{own}" if own else "") + (f"\n{body[:cap]}" if body else ""))
    return out


def known_names(root):
    """The people 心响 has something on (an understanding or the owner's words)."""
    folder = Path(root) / FOLDER
    names = {f.stem for f in folder.glob("*.md")} if folder.is_dir() else set()
    return names | set(_get(root, NOTES))


def mentioned(root, text, first=(), limit=3):
    """Who of the known people this text is about: the ones named first (who said it), then those mentioned."""
    known = known_names(root)
    out = [n for n in first if n in known]
    out += sorted((n for n in known if n not in out and len(n) >= 2 and n in str(text or "")), key=lambda n: str(text).find(n))
    return out[:limit]
