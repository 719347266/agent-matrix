"""Pair recorded owner messages with the first subsequent response on the same item."""
from datetime import datetime, timedelta
from statistics import median


def measure(history, now):
    latest, pickups, replies = {}, [], []
    for rid, entry in history.items():
        turn = None
        for m in entry.get("messages", []):
            try:
                at = datetime.fromisoformat(m["iso_at"])
                if at.tzinfo is None or at > now:
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            who, kind, text = m["from"], m["kind"], m.get("text") or ""
            if who == "system" and kind == "closed":
                if turn and turn["reply_at"] is None:
                    turn = None
                continue
            if who == "me" and kind in {"say", "feedback", "material"}:
                if text.startswith(("改期到", "交给", "（整理自")):
                    turn = None  # administrative events are not questions to the AI
                else:
                    turn = {"sent_at": at, "pickup_at": None, "reply_at": None}
                continue
            if not turn or turn["reply_at"] is not None or at < turn["sent_at"]:
                continue
            if ((who == "ai" and kind == "step") or
                    (who == "system" and kind == "say" and text.rstrip().endswith("开始处理"))):
                if turn["pickup_at"] is None:
                    turn["pickup_at"] = at
            if who == "ai" and kind in {"say", "deliver", "ask"}:
                turn["reply_at"] = at
                if turn["sent_at"] >= now - timedelta(days=30):
                    replies.append((at - turn["sent_at"]).total_seconds())
                    if turn["pickup_at"] is not None:
                        pickups.append((turn["pickup_at"] - turn["sent_at"]).total_seconds())
        if turn:
            latest[rid] = {key: value.isoformat() if value else None for key, value in turn.items()}
    return latest, {"days": 30, "replies": len(replies), "pickups": len(pickups),
                    "median_reply_seconds": median(replies) if replies else None,
                    "median_pickup_seconds": median(pickups) if pickups else None}
