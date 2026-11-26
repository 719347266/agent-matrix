"""A deliverable kept on this computer, for an owner who has not connected Feishu.

The AI's 交付.md is stored once per content under state/deliveries/<item>/<digest>.md and opened through 心响's own
address (/doc/<item>/<digest>), so an item's 交付 link works the same way with or without a document service.
"""
import html
import re
from hashlib import sha256
from pathlib import Path

_RID, _NAME = re.compile(r"[A-Za-z0-9_-]{1,80}"), re.compile(r"[0-9a-f]{12}")


def save(root, record_id, data, port):
    name = sha256(data).hexdigest()[:12]
    path = Path(root) / "state/deliveries" / record_id / (name + ".md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return f"http://127.0.0.1:{port}/doc/{record_id}/{name}"


def read(root, record_id, name):
    """The stored text, or None: only names this module made up itself are ever looked for."""
    if not _RID.fullmatch(record_id or "") or not _NAME.fullmatch(name or ""):
        return None
    path = Path(root) / "state/deliveries" / record_id / (name + ".md")
    return path.read_text(encoding="utf-8") if path.is_file() else None


def is_local(url, port):
    return str(url or "").startswith(f"http://127.0.0.1:{port}/doc/")


def _inline(text):
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    return re.sub(r"\[([^\]]+)\]\((https?://[^\s()]+)\)", r'<a href="\2" target="_blank" rel="noopener">\1</a>', text)


def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def body(text):
    """Enough Markdown for a deliverable: headings, lists, tables, code blocks, paragraphs."""
    out, lines, i = [], text.splitlines(), 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("```"):
            j = i + 1
            while j < len(lines) and not lines[j].lstrip().startswith("```"):
                j += 1
            out.append("<pre>" + html.escape("\n".join(lines[i + 1:j]), quote=False) + "</pre>")
            i = j + 1
        elif m := re.match(r"(#{1,4})\s+(.*)", line):
            out.append(f"<h{len(m[1])}>{_inline(m[2])}</h{len(m[1])}>")
            i += 1
        elif line.lstrip().startswith("|") and i + 1 < len(lines) and re.fullmatch(r"\s*\|?[\s:|-]+\|?\s*", lines[i + 1]) and "-" in lines[i + 1]:
            rows, j = [], i + 2
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                rows.append(_cells(lines[j]))
                j += 1
            head = "".join(f"<th>{_inline(c)}</th>" for c in _cells(line))
            rest = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rows)
            out.append(f"<div class=t><table><tr>{head}</tr>{rest}</table></div>")
            i = j
        elif re.match(r"\s*([-*]|\d+[.)])\s+", line):
            tag, j, items = ("ol" if re.match(r"\s*\d", line) else "ul"), i, []
            while j < len(lines) and (m := re.match(r"\s*([-*]|\d+[.)])\s+(.*)", lines[j])):
                items.append(f"<li>{_inline(m[2])}</li>")
                j += 1
            out.append(f"<{tag}>{''.join(items)}</{tag}>")
            i = j
        elif line.strip():
            j = i
            while j < len(lines) and lines[j].strip() and not re.match(r"\s*(#{1,4}\s|[-*]\s|\d+[.)]\s|\||```)", lines[j]):
                j += 1
            j = max(j, i + 1)
            out.append("<p>" + "<br>".join(_inline(x) for x in lines[i:j]) + "</p>")
            i = j
        else:
            i += 1
    return "\n".join(out)


def page(text):
    first = next((x.lstrip("# ").strip() for x in text.splitlines() if x.strip()), "交付")
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(first[:60])}</title>
<style>
:root{{color-scheme:light dark}}
body{{margin:0;font:15px/1.7 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;background:Canvas;color:CanvasText}}
main{{max-width:760px;margin:0 auto;padding:40px 24px 80px}}
h1{{font-size:24px;line-height:1.35;margin:0 0 20px}}h2{{font-size:17px;margin:32px 0 8px}}h3,h4{{font-size:15px;margin:22px 0 6px}}
p,ul,ol{{margin:8px 0}}ul,ol{{padding-left:22px}}
.t{{overflow-x:auto;margin:12px 0}}table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid color-mix(in srgb,CanvasText 16%,transparent);padding:7px 10px;text-align:left;vertical-align:top}}
th{{background:color-mix(in srgb,CanvasText 5%,transparent);font-weight:600;white-space:nowrap}}
code,pre{{font:13px/1.6 ui-monospace,Menlo,Consolas,monospace;background:color-mix(in srgb,CanvasText 6%,transparent);border-radius:5px}}
code{{padding:1px 5px}}pre{{padding:12px 14px;overflow-x:auto}}a{{color:LinkText}}
</style><main>{body(text)}</main></html>"""
