"""Reading one value out of a table cell: a single choice, a real link."""
import re
from urllib.parse import urlparse

from .models import MatrixError


def single(value):
    if value is None or value == [] or value == "":
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], str):
        return value[0]
    raise MatrixError("单选字段出现多个值或异常结构；不推定执行方或状态。")


def draft_url(value):
    """Read one actual hyperlink, never the Markdown label or an ambiguous list."""
    if isinstance(value, dict):
        value = value.get("url")
    if isinstance(value, list):
        return draft_url(value[0]) if len(value) == 1 else None
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.startswith("["):
        link = re.fullmatch(r"\[(?:\\.|[^\]\\\r\n])*\]\((https?://[^\s<>()]+)\)", value)
        if not link:
            return None
        value = link.group(1)
    if not value or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        return None
    try:
        parsed = urlparse(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        parsed.port  # Validate a malformed port instead of accepting it as a URL.
    except ValueError:
        return None
    return value


def number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise MatrixError("编号必须是平台生成的正整数；不猜测或按列表位置重编号。")
    return int(value)


def validate_registry(rows, previous=None):
    """Preserve tombstones so deletion/recreation can never silently reuse an ID."""
    old = previous or {"by_record": {}, "by_number": {}}
    by_record, by_number = dict(old["by_record"]), dict(old["by_number"])
    seen_records, seen_numbers = set(), set()
    for row in rows:
        rid, n = row.get("record_id"), number(row.get("编号"))
        if not isinstance(rid, str) or not rid or rid in seen_records or n in seen_numbers:
            raise MatrixError("事项或编号缺失／重复；停止建立编号映射。")
        if (rid in by_record and by_record[rid] != n) or (str(n) in by_number and by_number[str(n)] != rid):
            raise MatrixError("检测到编号重置或复用；保留原编号登记，不覆盖。")
        seen_records.add(rid); seen_numbers.add(n)
        by_record[rid], by_number[str(n)] = n, rid
    return {"by_record": by_record, "by_number": by_number}
