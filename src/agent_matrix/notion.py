"""Items → a Notion database. 心响 keeps the items in state/items.db; this mirrors each changed item to Notion
in the background (same queue as the Feishu backup). One way only: an edit made in Notion is overwritten by
the next change to that item.

Connect: create an internal integration at notion.so/profile/integrations, share one page with it
(··· → Connections), paste the secret and the page link. 心响 creates the "心响事项" database under that page.
"""
import json
import re
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .models import MatrixError

API = "https://api.notion.com/v1/"
VERSION = "2022-06-28"
TITLE = "标题"
CHECKBOX = {"排除本人提醒"}
TEXT_MAX = 2000


def call(tok, method, path, body=None):
    req = Request(API + path, method=method, data=None if body is None else json.dumps(body).encode(),
                  headers={"Authorization": "Bearer " + tok, "Notion-Version": VERSION, "Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except HTTPError as exc:
        try:
            err = json.load(exc)
        except ValueError:
            err = {}
        code = err.get("code") or exc.code
        if code == "unauthorized":
            raise MatrixError("Notion 不认这个密钥：在 notion.so/profile/integrations 复制 Internal Integration Secret。") from None
        if code == "object_not_found":
            raise MatrixError("Notion 找不到这个页面：在页面右上角 ··· → Connections 里加上你的 integration。") from None
        raise MatrixError(f"Notion {path.split('/')[0]} 失败：{code}") from None  # error code only, never the token


def page_id(link):
    """The 32-hex id at the end of a Notion page link (or the id itself)."""
    last = re.split(r"[?#]", (link or "").strip())[0].rstrip("/").rsplit("/", 1)[-1]
    found = re.search(r"[0-9a-f]{32}$", last.replace("-", "").lower())  # My-Page-<id> or a dashed uuid
    if not found:
        raise MatrixError("没认出 Notion 页面链接：在页面右上角 Share → Copy link 粘过来。")
    return found.group()


def schema(fields):
    from .itemstore import DATES, INTS, SELECTS, URLS
    def kind(name):
        if name == TITLE:
            return {"title": {}}
        if name in SELECTS:
            return {"select": {}}
        if name in DATES or name in {"创建时间", "更新时间"}:
            return {"date": {}}
        if name in URLS:
            return {"url": {}}
        if name in INTS or name == "编号":
            return {"number": {}}
        if name in CHECKBOX:
            return {"checkbox": {}}
        return {"rich_text": {}}
    return {f: kind(f) for f in fields}


def _text(v):
    v = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return [{"text": {"content": v[:TEXT_MAX]}}] if v else []


def properties(row, kinds, numbers):
    """One local item (in the shape itemstore stores it) → Notion page properties."""
    out = {}
    for name, kind in kinds.items():
        v = row.get(name)
        k = next(iter(kind))
        if k == "title":
            out[name] = {"title": _text(v or "")}
        elif k == "select":
            first = (v[0] if isinstance(v, list) and v else v) or None
            out[name] = {"select": {"name": str(first).replace(",", "，")[:100]} if first else None}  # Notion forbids commas
        elif k == "date":
            out[name] = {"date": {"start": v} if v else None}
        elif k == "url":
            link = re.search(r"\]\((.*)\)$", v or "")
            out[name] = {"url": (link.group(1) if link else v) or None}
        elif k == "number":
            try:
                out[name] = {"number": int(v) if v not in (None, "") else None}
            except (TypeError, ValueError):
                out[name] = {"number": None}
        elif k == "checkbox":
            out[name] = {"checkbox": bool(v)}
        elif isinstance(v, list) and v and isinstance(v[0], dict):  # people → names, linked items → #number
            out[name] = {"rich_text": _text("、".join(
                x.get("name") or (f"#{numbers[x['id']]}" if x.get("id") in numbers else x.get("id") or "") for x in v))}
        else:
            out[name] = {"rich_text": _text("" if v is None else v)}
    return out


def push(config, *, api=call):
    """itemstore.backup push: whatever the queued change was, write the item's current state to its page."""
    tok, database = config.notion_token, config.notion_database_id
    if not tok or not database:
        raise MatrixError("Notion 还没连接。")

    def one(db, op, rid, raw):
        found = db.execute("SELECT fields FROM items WHERE record_id=?", (rid,)).fetchone()
        if not found:
            return
        numbers = dict(db.execute("SELECT record_id, n FROM items"))
        row = json.loads(found[0])
        props = properties(row, schema(row.keys() | {TITLE}), numbers)
        page = db.execute("SELECT page_id FROM notion_pages WHERE record_id=?", (rid,)).fetchone()
        if page:
            api(tok, "PATCH", "pages/" + page[0], {"properties": props})
        else:
            made = api(tok, "POST", "pages", {"parent": {"database_id": database}, "properties": props})
            with db:
                db.execute("INSERT OR REPLACE INTO notion_pages VALUES (?,?)", (rid, made["id"]))
    return one


def connect(root, tok, link, *, api=call):
    """Check the secret, create the 心响事项 database under the shared page, switch items to Notion and queue
    every existing item for the first copy. Never echoes the secret back."""
    from . import itemstore
    from .config import load, save
    from .workflow import FIELDS
    tok = (tok or "").strip()
    if not tok.startswith(("ntn_", "secret_")):
        raise MatrixError("要 Internal Integration Secret（ntn_ 或 secret_ 开头）。")
    parent = page_id(link)
    path = root / "config.yaml"
    made = api(tok, "POST", "databases", {
        "parent": {"type": "page_id", "page_id": parent},
        "title": [{"text": {"content": "心响事项"}}],
        "properties": schema(FIELDS)})
    config = load(path)
    config.notion_token, config.notion_database_id = tok, made["id"]
    save(path, config)
    itemstore.choose(root, "notion")
    from .state import State
    state = State(root / "state/state.db")
    try:
        state.set("notion:url", made.get("url"))
    finally:
        state.close()
    return {"url": made.get("url"), "tail": tok[-4:]}
