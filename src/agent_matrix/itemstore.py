"""心响 · 本地事项库：事项存在本机，点了立刻生效；飞书多维表格在后台做备份。

    rows      every item, in exactly the shape the Feishu table export gives, so callers do not change
    update    patch one item in one local transaction, then queue the change for backup
    create    add one item with the next number, then queue it for backup
    backup    push queued changes to the Feishu table in order (the listener runs it every few seconds)

Turned on by `item_store: local` in config.yaml. The first read imports the whole Feishu table once and
keeps its record ids, so conversations, deliverables and links stay attached. After that the table is
only written to, never read from: an edit made directly in Feishu is overwritten by the next backup of
that item. Items created here get a local id (recL…) and the Feishu id is remembered after backup.
"""
from datetime import datetime
import json
from pathlib import Path
import secrets
import sqlite3
import string

from .models import MatrixError
from .timeutil import TZ

SELECTS = {"方向", "状态", "优先级", "预做策略", "来源", "执行方", "验收结果", "类型", "交付情况", "完成判定"}
PEOPLE = {"负责人"}
LINKS = {"父事项", "等待事项"}
DATES = {"截止日", "首次截止日", "完成时间"}
URLS = {"交付物链接", "AI草稿链接", "来源链接", "关联任务链接"}
INTS = {"顺延次数", "延期次数", "预计耗时(分钟)"}
COMPUTED = {"编号", "创建时间", "更新时间"}   # Feishu fills these itself; never sent in a backup
BACKUP_GIVE_UP = 5


def enabled(config):
    return getattr(config, "item_store", "feishu") in ("local", "notion", "device")


def feishu_free(config):
    """Items in Notion or only on this computer: nothing here needs Feishu."""
    return getattr(config, "item_store", "feishu") in ("notion", "device")


def is_items_table(config, args):
    args = list(args)
    table = args[args.index("--table-id") + 1] if "--table-id" in args else None
    return bool(table) and table == (getattr(config, "tables", {}) or {}).get("事项表")


def _db(root):
    path = Path(root) / "state/items.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS items (record_id TEXT PRIMARY KEY, n INTEGER, fields TEXT NOT NULL, updated TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, op TEXT NOT NULL, record_id TEXT NOT NULL,
            payload TEXT NOT NULL, created TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, error TEXT,
            state TEXT NOT NULL DEFAULT 'pending');
        CREATE TABLE IF NOT EXISTS idmap (local_id TEXT PRIMARY KEY, feishu_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    return db


def _now():
    return datetime.now(TZ)


def _stamp(moment):
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{moment.microsecond // 1000:03d}+08:00"


def _meta(db, key, default=None):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _set_meta(db, key, value):
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, json.dumps(value, ensure_ascii=False)))


def _number(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# ---- value shapes: whatever a caller writes is stored the way the Feishu export reads it back --------

def _moment(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, TZ)
    at = datetime.fromisoformat(str(value).strip().replace(" ", "T") if len(str(value).strip()) > 10 else str(value).strip())
    return (at if at.tzinfo else at.replace(tzinfo=TZ)).astimezone(TZ)


def to_read(key, value, old=None):
    if value in (None, "", []):
        return None
    if key in SELECTS:
        if isinstance(value, str):
            return [value]
        return [x.get("name", x.get("text")) if isinstance(x, dict) else x for x in value]
    if key in PEOPLE:
        names = {x.get("id"): x.get("name") for x in old or [] if isinstance(x, dict)}
        out = []
        for x in value if isinstance(value, list) else [value]:
            pid = x.get("id") if isinstance(x, dict) else str(x)
            name = (x.get("name") if isinstance(x, dict) else None) or names.get(pid)
            out.append({"id": pid, **({"name": name} if name else {})})
        return out
    if key in LINKS:
        return [{"id": x.get("id") or x.get("record_id")} if isinstance(x, dict) else {"id": str(x)}
                for x in (value if isinstance(value, list) else [value])]
    if key in DATES:
        moment = _moment(value)
        if key != "完成时间" and isinstance(value, str) and len(value.strip()) <= 16:
            moment = moment.replace(second=0, microsecond=0)
        return _stamp(moment)
    if key in URLS:
        if isinstance(value, dict):
            link = value.get("link") or value.get("url") or ""
            return f"[{value.get('text') or link}]({link})" if link else None
        text = str(value).strip()
        return text if text.startswith("[") and "](" in text else f"[{text}]({text})"
    if key in INTS:
        return int(value)
    if key == "编号":
        return str(value)
    return value


# ---- import once ----------------------------------------------------------------------------------------

def imported(root):
    db = _db(root)
    try:
        return bool(_meta(db, "imported_at"))
    finally:
        db.close()


def import_rows(root, rows, fields):
    """The whole Feishu table, once. Record ids are kept so everything attached to an item stays attached."""
    db = _db(root)
    try:
        with db:
            if _meta(db, "imported_at"):
                return 0
            now = _stamp(_now())
            for r in rows:
                rid = r.get("record_id")
                if not rid:
                    raise MatrixError("飞书表格里有记录缺少 ID，未导入。")
                row = {f: r.get(f) for f in fields}
                db.execute("INSERT OR REPLACE INTO items VALUES (?,?,?,?)",
                           (rid, _number(r.get("编号")), json.dumps(row, ensure_ascii=False), now))
            top = max([_number(r.get("编号")) or 0 for r in rows] or [0])
            _set_meta(db, "fields", list(dict.fromkeys(fields)))
            _set_meta(db, "next_number", top + 1)
            _set_meta(db, "imported_at", now)
        return len(rows)
    finally:
        db.close()


def ensure(client, state=None):
    """Import the Feishu table the first time the local store is used."""
    root = Path(client.root)
    if state is not None and feishu_free(client.config) and state.get("brief-v1:migration-stage") != "v1_active":
        # Items kept only here are always in the current shape (执行方, 补充材料…); there is no Feishu table to migrate.
        state.set("brief-v1:migration-stage", "v1_active")
    if imported(root):
        return
    from .workflow import FIELDS
    if feishu_free(client.config):
        import_rows(root, [], list(FIELDS))  # nothing to import: start empty
        return
    from .records import read_remote
    from .state import State
    names = list(FIELDS)
    try:
        listed = client.call("+field-list", "--base-token", client.config.base_token,
                             "--table-id", client.config.tables["事项表"])
        from .bitable import items as listed_items
        found = [f.get("field_name") or f.get("name") for f in listed_items(listed)]
        names = list(dict.fromkeys([*names, *[n for n in found if n]]))
    except Exception:
        pass
    own = state is None
    state = state or State(root / "state/state.db")
    try:
        rows = read_remote(client, state, "事项表", names)
    finally:
        if own:
            state.close()
    import_rows(root, rows, names)


# ---- read and write -------------------------------------------------------------------------------------

def rows(root, fields=None):
    db = _db(root)
    try:
        data = [(rid, json.loads(f)) for rid, f in db.execute("SELECT record_id, fields FROM items ORDER BY n, record_id")]
    finally:
        db.close()
    if fields is None:
        return [{"record_id": rid, **row} for rid, row in data]
    names = list(dict.fromkeys(fields))
    return [{"record_id": rid, **{f: row.get(f) for f in names}} for rid, row in data]


def update(root, record_id, patch):
    db = _db(root)
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            found = db.execute("SELECT fields FROM items WHERE record_id=?", (record_id,)).fetchone()
            if not found:
                raise MatrixError("原事项不存在，未写入。")
            row = json.loads(found[0])
            for key, value in patch.items():
                if key not in COMPUTED:
                    row[key] = to_read(key, value, row.get(key))
            row["更新时间"] = _stamp(_now())
            db.execute("UPDATE items SET fields=?, updated=? WHERE record_id=?",
                       (json.dumps(row, ensure_ascii=False), row["更新时间"], record_id))
            db.execute("INSERT INTO outbox (op, record_id, payload, created) VALUES ('update',?,?,?)",
                       (record_id, json.dumps(patch, ensure_ascii=False, default=str), row["更新时间"]))
    finally:
        db.close()
    return record_id


def _new_id():
    return "recL" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12))


def create(root, fields):
    db = _db(root)
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            n = _meta(db, "next_number", 1)
            top = db.execute("SELECT max(n) FROM items").fetchone()[0] or 0
            n = max(int(n), top + 1)
            rid = _new_id()
            now = _stamp(_now())
            row = {f: None for f in _meta(db, "fields", [])}
            for key, value in fields.items():
                if key not in COMPUTED:
                    row[key] = to_read(key, value)
            row.update({"编号": str(n), "创建时间": now, "更新时间": now})
            db.execute("INSERT INTO items VALUES (?,?,?,?)", (rid, n, json.dumps(row, ensure_ascii=False), now))
            _set_meta(db, "next_number", n + 1)
            db.execute("INSERT INTO outbox (op, record_id, payload, created) VALUES ('create',?,?,?)",
                       (rid, json.dumps(fields, ensure_ascii=False, default=str), now))
    finally:
        db.close()
    return rid


def apply_call(client, command, args):
    """What `base +record-batch-update/-create` on the item table does, done locally (instant)."""
    args = list(args)
    body = json.loads(args[args.index("--json") + 1])
    ensure(client)
    root = Path(client.root)
    if command == "+record-batch-update":
        done = [update(root, rid, patch) for rid, patch in (body.get("update_records") or {}).items()]
        return {"updated": done, "store": "local"}
    if command == "+record-batch-create":
        return {"record_id_list": [create(root, fields) for fields in body.get("create_records") or []], "store": "local"}
    raise MatrixError("本地事项库不支持这个操作。")


# ---- backup to the Feishu table -------------------------------------------------------------------------

def _feishu_ids(db, value):
    out = []
    for x in value if isinstance(value, list) else [value]:
        rid = x.get("id") or x.get("record_id") if isinstance(x, dict) else str(x)
        mapped = db.execute("SELECT feishu_id FROM idmap WHERE local_id=?", (rid,)).fetchone()
        if not mapped and rid.startswith("recL"):
            return None  # the linked item has not reached Feishu yet
        out.append({"id": mapped[0] if mapped else rid})
    return out


def _payload(db, raw):
    data = {}
    for key, value in json.loads(raw).items():
        if key in COMPUTED:
            continue
        if key in LINKS and value:
            value = _feishu_ids(db, value)
            if value is None:
                return None
        data[key] = value
    return data


WAIT = "wait"


def feishu_push(client):
    """One queued change → the Feishu table. Returns WAIT while a linked item has not reached Feishu yet."""
    coords = ["--base-token", client.config.base_token, "--table-id", client.config.tables["事项表"]]

    def push(db, op, rid, raw):
        payload = _payload(db, raw)
        target = db.execute("SELECT feishu_id FROM idmap WHERE local_id=?", (rid,)).fetchone()
        target = target[0] if target else (None if rid.startswith("recL") else rid)
        if payload is None or (op == "update" and not target):
            return WAIT
        if op == "update":
            if payload:
                receipt = client.call("+record-batch-update", *coords, "--json",
                                      json.dumps({"update_records": {target: payload}}, ensure_ascii=False), write=True)
                if receipt.get("ignored_fields"):
                    raise MatrixError(f"飞书忽略了字段：{receipt['ignored_fields']}")
        else:
            receipt = client.call("+record-batch-create", *coords, "--json",
                                  json.dumps({"create_records": [payload]}, ensure_ascii=False), write=True)
            ids = receipt.get("record_id_list") or []
            if len(ids) != 1:
                raise MatrixError("飞书没有返回新记录的 ID")
            with db:
                db.execute("INSERT OR REPLACE INTO idmap VALUES (?,?)", (rid, ids[0]))
    return push


def backup(root, client=None, limit=30, push=None, target="飞书"):
    """Push queued changes to the backup (the Feishu table by default, or Notion), oldest first. A change that
    keeps failing is set aside after a few tries and the owner is told once; the local item is never rolled back."""
    from . import inbox
    push = push or feishu_push(client)
    pushed = 0
    db = _db(root)
    try:
        for oid, op, rid, raw, attempts in db.execute(
                "SELECT id, op, record_id, payload, attempts FROM outbox WHERE state='pending' ORDER BY id LIMIT ?",
                (limit,)).fetchall():
            try:
                if push(db, op, rid, raw) == WAIT:
                    break  # waits for an earlier item to reach the backup first
                with db:
                    db.execute("UPDATE outbox SET state='done', error=NULL WHERE id=?", (oid,))
                    _set_meta(db, "backup_ok_at", _stamp(_now()))
                pushed += 1
            except Exception as exc:
                failed = attempts + 1 >= BACKUP_GIVE_UP
                with db:
                    db.execute("UPDATE outbox SET attempts=attempts+1, error=?, state=? WHERE id=?",
                               (str(exc)[:300], "failed" if failed else "pending", oid))
                    _set_meta(db, "backup_error", {"at": _stamp(_now()), "error": str(exc)[:200]})
                if failed:
                    inbox.add(root, f"backup-failed:{oid}", f"⚠ {target}备份有一条没写进去：{op} {rid}\n{str(exc)[:120]}\n"
                              "本机的事项不受影响；在「系统」里可以重试。", kind="system")
                    continue
                break
    finally:
        db.close()
    return {"pushed": pushed, **status(root)}


def retry_failed(root):
    db = _db(root)
    try:
        with db:
            return db.execute("UPDATE outbox SET state='pending', attempts=0 WHERE state='failed'").rowcount
    finally:
        db.close()


def status(root):
    db = _db(root)
    try:
        counts = dict(db.execute("SELECT state, count(*) FROM outbox WHERE state!='done' GROUP BY state").fetchall())
        return {"pending": counts.get("pending", 0), "failed": counts.get("failed", 0),
                "ok_at": _meta(db, "backup_ok_at"), "error": _meta(db, "backup_error"),
                "imported_at": _meta(db, "imported_at"), "items": db.execute("SELECT count(*) FROM items").fetchone()[0]}
    finally:
        db.close()


def queue_all(root):
    """Queue every item for the backup once, e.g. right after Notion is connected."""
    db = _db(root)
    try:
        with db:
            now = _stamp(_now())
            ids = [r[0] for r in db.execute("SELECT record_id FROM items ORDER BY n")]
            db.executemany("INSERT INTO outbox (op, record_id, payload, created) VALUES ('update',?,'{}',?)",
                           [(rid, now) for rid in ids])
        return len(ids)
    finally:
        db.close()


def choose(root, mode):
    """The owner picks where items are recorded: "feishu" (Feishu table backup), "notion" (connect first,
    see notion.connect) or "device" (this computer only)."""
    from .config import load, save
    root = Path(root)
    path = root / "config.yaml"
    config = load(path)
    if mode == "notion":
        if not (config.notion_token and config.notion_database_id):
            raise MatrixError("先连接 Notion。")
    elif mode == "feishu":
        if not config.base_token or config.tables.get("事项表") in (None, "local"):
            raise MatrixError("飞书还没初始化：先装好 lark-cli 并运行 setup。")
        db = _db(root)
        try:
            local_only = db.execute("SELECT count(*) FROM items WHERE record_id LIKE 'recL%' AND record_id NOT IN "
                                    "(SELECT local_id FROM idmap)").fetchone()[0]
        finally:
            db.close()
        if local_only:  # ponytail: no catch-up of items made while away from Feishu; add a create-from-row backup if needed
            raise MatrixError(f"有 {local_only} 件事项只在本机，飞书表格里没有，暂不能切回飞书。")
        mode = "local"
    elif mode != "device":
        raise MatrixError("只能选 飞书 / Notion / 本机。")
    if not imported(root) and config.item_store == "feishu" and config.base_token:
        from .feishu import Feishu  # items still live in the Feishu table: bring them home first
        client = Feishu(config, root)
        client.verify_owner(client.auth())
        config.item_store = "local"
        ensure(client)
    if not imported(root) and mode != "local":
        from .workflow import FIELDS
        import_rows(root, [], list(FIELDS))  # never used Feishu: start empty
    if mode == "notion":
        queue_all(root)
    config.item_store = mode
    save(path, config)
    return mode
