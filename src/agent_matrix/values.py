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
