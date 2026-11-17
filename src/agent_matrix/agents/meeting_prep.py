"""Before a meeting: what was left open last time, and open items about the same topic.

Pure facts from the task table, the meeting queue and the calendar; no model.
Sent once per meeting, 15–45 minutes before it starts. Nothing to show → nothing sent.
"""
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import re

from ..brief_migration import number, single
from ..meeting_watch import MeetingQueue, parse_time

STOPWORDS = {"会议", "讨论", "确认", "同步", "沟通", "例会", "项目", "需求", "准备", "整理", "方案", "优化",
             "问题", "团队", "日常", "周一", "周二", "周三", "周四", "周五",
             "复盘会", "评估", "调研", "版本", "用户", "完成", "安排"}


def _keywords(title):
    """Meaningful pieces of a meeting title: version numbers and 2–4 char Chinese runs."""
    title = str(title or "")
    out = set(re.findall(r"\d+\.\d+", title))
    for run in re.findall(r"[一-鿿]{2,}", title):
        for size in (4, 3, 2):
            for i in range(len(run) - size + 1):
                piece = run[i:i + size]
                if piece not in STOPWORDS and not any(w in piece for w in ("会", "的", "及", "与", "和")):
                    out.add(piece)
    return out


def _related(item_title, keys):
    return any(k in str(item_title or "") for k in keys)


def _owner_names(root, meeting_id):
    path = Path(root) / "state/meeting-transcripts" / meeting_id / "review.json"
    try:
        actions = json.loads(path.read_text()).get("actions", [])
    except (OSError, ValueError):
        return {}
    return {a.get("title"): a.get("owner_name") for a in actions}


def previous_meeting(queue, topic, before):
    same = [r for r in queue.list() if r["metadata"].get("topic") == topic and r["status"] == "done"
            and parse_time(r["metadata"]["start_time"]) < before]
    return max(same, key=lambda r: parse_time(r["metadata"]["start_time"]), default=None)


def build(config, rows, event, queue, root, now):
    from .. import brief_v1
    from ..workflow import day
    start = datetime.fromisoformat(event["start_time"]["datetime"])
    topic = event.get("summary") or "会议"
    blocks, listed = [], set()
    owner = config.owner_name

    prev = previous_meeting(queue, topic, start)
    if prev:
        names = _owner_names(root, prev["meeting_id"])
        mine_from_prev = [r for r in rows if str(r.get("去重键") or "").startswith("meeting:" + prev["meeting_id"] + ":")
                          and single(r.get("状态")) != "取消"]
        done = [r for r in mine_from_prev if single(r.get("状态")) == "完成"]
        open_ = [r for r in mine_from_prev if single(r.get("状态")) != "完成"]
        items = []
        # Things to raise in the meeting first; results already delivered to the owner last.
        for r in sorted(open_, key=lambda r: (single(r.get("状态")) == "待验收", number(r["编号"])))[:5]:
            who = names.get(r.get("标题")) or ("你" if single(r.get("执行方")) in {"我", "AI"} else "对方")
            who = "你" if who == owner else who
            head, hint, color, url = brief_v1.describe(r, now.date(), {})
            status = single(r.get("状态"))
            hint = f"{who} · {status}" + (f" · {hint}" if hint and status != "待验收" else "")
            items.append(brief_v1._row_item((head, hint, color, None), url))
            listed.add(r["record_id"])
        if done:
            items.append({"tag": "markdown", "content": f"<font color='green'>已完成 {len(done)} 件</font>", "text_size": "notation"})
        if items:
            when = parse_time(prev["metadata"]["start_time"])
            blocks.append(brief_v1._block(f"上次（{when:%m-%d}）这个会留下的事", "blue", items,
                                          max(0, len(open_) - 5), fill="blue-50"))

    # Brand or company words that appear in many titles are configured locally (config.topic_stopwords).
    keys = {k for k in _keywords(topic) if not any(w in k for w in config.topic_stopwords)}
    related = [r for r in rows if r["record_id"] not in listed and brief_v1.visible(r)
               and single(r.get("状态")) not in {"完成", "取消"} and _related(r.get("标题"), keys)]
    if related:
        related.sort(key=lambda r: (single(r.get("状态")) != "待验收", number(r["编号"])))
        items = [brief_v1._row_item(brief_v1.describe(r, now.date(), {}), brief_v1.describe(r, now.date(), {})[3])
                 for r in related[:4]]
        blocks.append(brief_v1._block("和这个会相关、还没完成的事", "blue", items, max(0, len(related) - 4)))

    if not blocks:
        return None
    minutes = max(1, round((start - now).total_seconds() / 60))
    link = (event.get("vchat") or {}).get("meeting_url") or event.get("app_link")
    footer = "会后待办会自动从逐字稿里提取，不用手记。"
    card = brief_v1.card(f"{start:%H:%M} {topic}", f"{minutes} 分钟后开始", "wathet", blocks, footer, None,
                         summary=f"{start:%H:%M} {topic}：会前看一眼")
    card["header"]["icon"] = {"tag": "standard_icon", "token": "calendar_colorful"}
    if link:
        card["body"]["elements"].append({"tag": "button", "text": {"tag": "plain_text", "content": "进入会议"},
                                         "type": "primary_filled", "width": "fill",
                                         "behaviors": [{"type": "open_url", "default_url": link}]})
    return card


def upcoming(client, now, horizon=45, lead=5):
    data = client._exec(["calendar", "+agenda", "--start", now.isoformat(),
                         "--end", (now + timedelta(minutes=horizon)).isoformat(), "--as", "user", "--format", "json"])
    events = data if isinstance(data, list) else data.get("events") or data.get("items") or []
    out = []
    for e in events:
        st = (e.get("start_time") or {}).get("datetime")
        if not st or (e.get("self_rsvp_status") or "").lower() in {"decline", "declined"}:
            continue
        start = datetime.fromisoformat(st)
        if now + timedelta(minutes=lead) < start <= now + timedelta(minutes=horizon):
            out.append(e)
    return out


def run(config, client, state, now, *, preview_event=None, horizon=45, lead=5):
    """Send prep cards for meetings starting soon. Returns what was (or would be) sent."""
    from ..delivery import send_once
    from ..workflow import Gateway
    events = [preview_event] if preview_event else upcoming(client, now, horizon=horizon, lead=lead)
    if not events:
        return []
    rows = Gateway(client, state).rows()
    queue = MeetingQueue(state)
    sent = []
    for event in events:
        key = "prep:" + event["event_id"]
        if not preview_event and (state.operation(key) or state.get(key + ":skipped")):
            continue
        try:
            detail = client._exec(["calendar", "+get", "--event-id", event["event_id"], "--as", "user", "--format", "json"])
            event = {**event, **(detail.get("event", detail) if isinstance(detail, dict) else {})}
        except Exception:
            pass  # the card still works without the join link
        card = build(config, rows, event, queue, client.root, now)
        if card is None:
            if not preview_event:
                state.set(key + ":skipped", now.isoformat())
            sent.append({"event": event.get("summary"), "sent": False, "reason": "nothing_to_prepare"})
            continue
        if preview_event:
            sent.append({"event": event.get("summary"), "card": card})
            continue
        receipt = send_once(state, key, lambda c=card, k=key: client.send_card(c, "prep-" + sha256(k.encode()).hexdigest()[:32]))
        sent.append({"event": event.get("summary"), "sent": True, "message_id": receipt.get("message_id")})
    return sent
