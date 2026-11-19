from datetime import date, datetime, timedelta
import re
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")

WEEKDAYS = "一二三四五六日"


def parse_date(text, today):
    """Owner wording → date. Returns None when the text is not a date."""
    t = text.strip()
    if t in {"今天", "今日"}:
        return today
    if t in {"明天", "明日"}:
        return today + timedelta(days=1)
    if t == "后天":
        return today + timedelta(days=2)
    m = re.fullmatch(r"(下下|下|本|这)?(?:周|星期|礼拜)([一二三四五六日天])", t)
    if m:
        target = WEEKDAYS.index(m[2].replace("天", "日"))
        base = today - timedelta(days=today.weekday())
        offset = {"下": 7, "下下": 14}.get(m[1] or "", 0)
        found = base + timedelta(days=target + offset)
        if not m[1] and found < today:
            found += timedelta(days=7)  # "周五" said on Saturday means next Friday
        return found
    m = re.fullmatch(r"(?:(\d{4})[-/.年])?(\d{1,2})[-/.月](\d{1,2})[日号]?", t)
    if m:
        year = int(m[1]) if m[1] else today.year
        try:
            found = date(year, int(m[2]), int(m[3]))
        except ValueError:
            return None
        if not m[1] and found < today - timedelta(days=60):
            found = date(year + 1, found.month, found.day)
        return found
    return None
