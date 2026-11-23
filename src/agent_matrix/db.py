"""The one place that opens SQLite and the one place that defines every table.

    open(root, "items")            a read-write connection to state/items.db, created and upgraded if needed
    connect(path, "state")         the same for an explicit file (tests, State(path))
    read(path)                     a read-only connection; fails with sqlite3.Error when the file is missing

Every read-write connection uses WAL and waits for a busy database instead of failing at once. Each file
carries its schema version in PRAGMA user_version. To change a table, append one step to its list in
SCHEMAS; never edit a step that has shipped. Steps run once, in order, inside one transaction, so two
processes starting together cannot half-upgrade a file.
"""
from pathlib import Path
import sqlite3
import time

FILES = {
    "state": "state/state.db",
    "items": "state/items.db",
    "xinxiang": "state/xinxiang.db",
    "inbox": "state/inbox.db",
    "activity": "state/activity.db",
    "commands": "state/app-commands.db",
    "pickup": "state/cindy-pickup.sqlite",
}


def _add_columns(table, *columns):
    def step(db):
        have = {c[1] for c in db.execute(f"PRAGMA table_info({table})")}
        for column in columns:
            if column.split()[0] not in have:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
    return step


# One list per file. Each entry is one upgrade step: SQL statements, or a function taking the connection.
SCHEMAS = {
    "state": [[
        "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS operations (key TEXT PRIMARY KEY, status TEXT NOT NULL, receipt TEXT, at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, command TEXT, started TEXT, ended TEXT, status TEXT, error_type TEXT)",
        """CREATE TABLE IF NOT EXISTS meeting_queue (
            meeting_id TEXT PRIMARY KEY, metadata TEXT NOT NULL, status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, next_check TEXT NOT NULL,
            transcript_path TEXT, transcript_sha256 TEXT, delivery_receipt TEXT)""",
        """CREATE TABLE IF NOT EXISTS prework_queue (
            record_id TEXT NOT NULL, input_version TEXT NOT NULL, candidate TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('queued','running','prepared','done','blocked','stale')),
            datekey TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            next_attempt_at TEXT, lease_until TEXT, attempts INTEGER NOT NULL DEFAULT 0,
            claim_count INTEGER NOT NULL DEFAULT 0, result_path TEXT, result_sha256 TEXT,
            prepared_at TEXT, receipt TEXT, delivered_at TEXT, reason TEXT,
            PRIMARY KEY(record_id,input_version))""",
        "CREATE INDEX IF NOT EXISTS prework_queue_ready ON prework_queue(status,next_attempt_at,lease_until)",
        """CREATE TABLE IF NOT EXISTS chat_candidates (
            message_id TEXT PRIMARY KEY, chat_id TEXT, chat_name TEXT, chat_mode TEXT, sender_id TEXT,
            sender_name TEXT, sent_at TEXT, text TEXT, context TEXT, kind TEXT, status TEXT, batch TEXT)""",
    ]],
    "items": [[
        "CREATE TABLE IF NOT EXISTS items (record_id TEXT PRIMARY KEY, n INTEGER, fields TEXT NOT NULL, updated TEXT NOT NULL)",
        """CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, op TEXT NOT NULL, record_id TEXT NOT NULL,
            payload TEXT NOT NULL, created TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, error TEXT,
            state TEXT NOT NULL DEFAULT 'pending')""",
        "CREATE TABLE IF NOT EXISTS idmap (local_id TEXT PRIMARY KEY, feishu_id TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
        "CREATE TABLE IF NOT EXISTS notion_pages (record_id TEXT PRIMARY KEY, page_id TEXT NOT NULL)",
    ]],
    "xinxiang": [[
        """CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL, at TEXT NOT NULL,
            sender TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, url TEXT, driver TEXT, dedupe TEXT UNIQUE)""",
        "CREATE INDEX IF NOT EXISTS messages_item ON messages(record_id, id)",
        """CREATE TABLE IF NOT EXISTS deliverables (id INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL, version INTEGER NOT NULL,
            at TEXT NOT NULL, summary TEXT, url TEXT, confirm TEXT, assumptions TEXT, sources TEXT, UNIQUE(record_id, version))""",
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
        "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)",
        "CREATE TABLE IF NOT EXISTS judgments (record_id TEXT PRIMARY KEY, at TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT)",
        """CREATE TABLE IF NOT EXISTS closures (id INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL, at TEXT NOT NULL,
            by TEXT NOT NULL, how TEXT, note TEXT)""",
        """CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT, n INTEGER, ref TEXT,
            folder TEXT, driver TEXT, status TEXT NOT NULL, step TEXT, rounds INTEGER NOT NULL DEFAULT 0, lease_until TEXT,
            created TEXT NOT NULL, updated TEXT NOT NULL, progress_at TEXT)""",
        _add_columns("jobs", "session TEXT", "link TEXT"),
        """CREATE TABLE IF NOT EXISTS moments (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, source_id TEXT NOT NULL,
            source_title TEXT, at TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, quote TEXT, stamp TEXT,
            wait_for TEXT, due TEXT, status TEXT NOT NULL DEFAULT 'open', created TEXT NOT NULL,
            UNIQUE(source, source_id, kind, text))""",
        """CREATE TABLE IF NOT EXISTS preferences (id INTEGER PRIMARY KEY AUTOINCREMENT, rule TEXT NOT NULL UNIQUE, why TEXT,
            source TEXT, created TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1)""",
        "CREATE TABLE IF NOT EXISTS recalls (waiting_id INTEGER NOT NULL, source_id TEXT NOT NULL, PRIMARY KEY (waiting_id, source_id))",
        "CREATE TABLE IF NOT EXISTS local_session_items (key TEXT PRIMARY KEY, rid TEXT NOT NULL, data TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS local_session_skips (key TEXT PRIMARY KEY)",
    ], [
        # items the owner strung together: `later` continues `earlier` (itemlinks.py)
        """CREATE TABLE IF NOT EXISTS item_links (earlier TEXT NOT NULL, later TEXT NOT NULL, created TEXT NOT NULL,
            by TEXT NOT NULL DEFAULT '本人', PRIMARY KEY (earlier, later))""",
    ], [
        # a rule the AI learned waits for the owner to say it is right (memory.py)
        _add_columns("preferences", "confirmed INTEGER NOT NULL DEFAULT 0"),
    ], [
        # the owner's own sorting of items into projects (projects.py)
        """CREATE TABLE IF NOT EXISTS projects (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, color TEXT,
            created TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1)""",
        "CREATE TABLE IF NOT EXISTS item_projects (record_id TEXT PRIMARY KEY, project_id INTEGER NOT NULL, created TEXT NOT NULL)",
    ], [
        # what went wrong while using 心响, written down until it is handed in (bugs.py)
        """CREATE TABLE IF NOT EXISTS bugs (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, text TEXT NOT NULL,
            diag TEXT, status TEXT NOT NULL DEFAULT 'open')""",
    ], [
        # the owner's verdict on each deliverable: what passed, what came back and why (memory.py)
        """CREATE TABLE IF NOT EXISTS outcomes (id INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL, at TEXT NOT NULL,
            verdict TEXT NOT NULL, reason TEXT, version INTEGER, template TEXT, project_id INTEGER)""",
        # a rule holds for everything, one project or one kind of deliverable, and keeps count of how it fared
        _add_columns("preferences", "scope TEXT NOT NULL DEFAULT ''", "wins INTEGER NOT NULL DEFAULT 0", "losses INTEGER NOT NULL DEFAULT 0"),
        # project → version → items, and what each version is expected to bring (versions.py)
        """CREATE TABLE IF NOT EXISTS versions (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'planned', period TEXT, note TEXT, source TEXT, created TEXT NOT NULL, UNIQUE(project_id, name))""",
        "CREATE TABLE IF NOT EXISTS item_versions (record_id TEXT PRIMARY KEY, version_id INTEGER NOT NULL, by TEXT NOT NULL, created TEXT NOT NULL)",
        """CREATE TABLE IF NOT EXISTS expected (id INTEGER PRIMARY KEY AUTOINCREMENT, version_id INTEGER NOT NULL, title TEXT NOT NULL,
            why TEXT, due TEXT, status TEXT NOT NULL DEFAULT 'open', created TEXT NOT NULL, UNIQUE(version_id, title))""",
    ], [
        # 记忆更新: what the daily tidy proposes, waiting for the owner's 用 / 不要 (tidy.py)
        """CREATE TABLE IF NOT EXISTS memory_updates (id INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT, kind TEXT NOT NULL,
            content TEXT NOT NULL, why TEXT, data TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created TEXT NOT NULL, decided TEXT)""",
    ], [
        # 记忆增强: what waits to be written into Hy Memory, written in the background by the listener (hymem.py)
        """CREATE TABLE IF NOT EXISTS hymem_queue (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, kind TEXT NOT NULL,
            text TEXT NOT NULL, workspace TEXT, ref TEXT, tries INTEGER NOT NULL DEFAULT 0, error TEXT, done TEXT, UNIQUE(kind, ref))""",
    ]],
    "inbox": [[
        """CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE,
            at TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
            blocks TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0)""",
    ]],
    "activity": [[
        "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, at TEXT, kind TEXT, title TEXT, detail TEXT, status TEXT, ref TEXT)",
        "CREATE TABLE IF NOT EXISTS beats (component TEXT PRIMARY KEY, at TEXT, summary TEXT)",
    ]],
    "commands": [[
        """CREATE TABLE IF NOT EXISTS commands (
            id TEXT PRIMARY KEY, payload TEXT NOT NULL, numbers TEXT NOT NULL,
            status TEXT NOT NULL, message TEXT NOT NULL, created REAL NOT NULL,
            updated REAL NOT NULL, dismissed INTEGER NOT NULL DEFAULT 0)""",
    ]],
    "pickup": [[
        """CREATE TABLE IF NOT EXISTS pickup (
            seq INTEGER PRIMARY KEY, request_key TEXT UNIQUE NOT NULL, session TEXT NOT NULL,
            rid TEXT NOT NULL, message TEXT NOT NULL, receipt TEXT, error TEXT)""",
    ]],
}


def version(db):
    return db.execute("PRAGMA user_version").fetchone()[0]


def upgrade(db, schema):
    """Bring this file up to the latest step. Safe to call on every connection and from several processes."""
    steps = SCHEMAS[schema]
    if version(db) >= len(steps):
        return
    if db.in_transaction:
        db.commit()
    db.execute("BEGIN IMMEDIATE")
    try:
        for index in range(version(db), len(steps)):
            for step in steps[index]:
                step(db) if callable(step) else db.execute(step)
            db.execute(f"PRAGMA user_version={index + 1}")
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _wal(db, timeout):
    """Switch the file to WAL when it is free. WAL only makes readers and the writer wait less; if another
    process is using the file right now (an older version still running during an update), carry on in the
    current mode and let a later connection switch it."""
    try:
        if db.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
            return
        db.execute("PRAGMA busy_timeout=200")
        db.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    finally:
        db.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")


def connect(path, schema, *, timeout=30):
    """A read-write connection to this file, created and upgraded to `schema` if needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=timeout)
    _wal(db, timeout)
    deadline = time.monotonic() + timeout
    while True:
        try:
            # Two processes starting together both want to upgrade. SQLite does not always wait for the
            # other one on its own, so wait here.
            upgrade(db, schema)
            return db
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc) or time.monotonic() >= deadline:
                db.close()
                raise
            time.sleep(0.05)


def open(root, schema, *, timeout=30):
    return connect(Path(root) / FILES[schema], schema, timeout=timeout)


def read(path, *, timeout=5):
    """A read-only connection. Never creates the file; raises sqlite3.Error when it cannot be read."""
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=timeout)
