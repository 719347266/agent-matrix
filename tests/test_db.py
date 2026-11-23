import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path

from agent_matrix import db


def shape(conn):
    return sorted(conn.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_every_file_is_created_at_the_latest_version_with_wal(self):
        for name in db.FILES:
            with closing(db.open(self.root, name)) as conn:
                self.assertEqual(db.version(conn), len(db.SCHEMAS[name]), name)
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal", name)

    def test_upgrading_twice_changes_nothing(self):
        for name in db.FILES:
            with closing(db.open(self.root, name)) as conn:
                first = shape(conn)
            with closing(db.open(self.root, name)) as conn:
                self.assertEqual(shape(conn), first, name)

    def test_a_database_from_before_versions_keeps_its_rows_and_gains_missing_columns(self):
        path = self.root / "state/xinxiang.db"
        path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(path)) as old, old:
            old.execute("""CREATE TABLE jobs (id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT, n INTEGER, ref TEXT,
                folder TEXT, driver TEXT, status TEXT NOT NULL, step TEXT, rounds INTEGER NOT NULL DEFAULT 0,
                lease_until TEXT, created TEXT NOT NULL, updated TEXT NOT NULL, progress_at TEXT)""")
            old.execute("INSERT INTO jobs (id, kind, status, created, updated) VALUES ('j1','item','open','t','t')")
        with closing(db.open(self.root, "xinxiang")) as conn:
            self.assertEqual(conn.execute("SELECT id, session, link FROM jobs").fetchall(), [("j1", None, None)])
            self.assertEqual(db.version(conn), len(db.SCHEMAS["xinxiang"]))

    def test_old_learning_alerts_are_archived_without_hiding_other_failures(self):
        with closing(db.open(self.root, "inbox")) as conn, conn:
            keys = ["job-failed:learn-123:2", "job-failed:item-123:2"]
            conn.executemany("INSERT INTO messages(key,at,kind,title,blocks) VALUES (?,'t','system','test','{}')", [(k,) for k in keys])
            conn.execute(f"PRAGMA user_version={len(db.SCHEMAS['inbox'])-1}")
        with closing(db.open(self.root, "inbox")) as conn:
            self.assertEqual(conn.execute("SELECT read,superseded FROM messages ORDER BY id").fetchall(), [(1, 1), (0, 0)])

    def test_a_failed_step_leaves_the_file_at_its_old_version(self):
        with closing(db.open(self.root, "inbox")) as conn:
            before = shape(conn)
        db.SCHEMAS["inbox"].append(["CREATE TABLE extra (a TEXT)", "THIS IS NOT SQL"])
        self.addCleanup(db.SCHEMAS["inbox"].pop)
        with self.assertRaises(sqlite3.Error):
            db.open(self.root, "inbox")
        with closing(db.read(self.root / "state/inbox.db")) as conn:
            self.assertEqual(shape(conn), before)
            self.assertEqual(db.version(conn), len(db.SCHEMAS["inbox"]) - 1)

    def test_processes_starting_together_upgrade_once(self):
        errors = []

        def start():
            try:
                db.open(self.root, "state").close()
            except Exception as exc:
                errors.append(exc)

        for name in ("state", "xinxiang"):
            threads = [threading.Thread(target=start) for _ in range(16)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            self.assertEqual(errors, [])
            self.root = Path(tempfile.mkdtemp(dir=self.tmp.name))

    def test_a_file_held_open_by_an_older_version_still_opens_and_keeps_its_rows(self):
        path = self.root / "state/state.db"
        path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(path)) as old:  # the older listener: connected, not in WAL, idle
            old.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            old.execute("INSERT INTO kv VALUES ('a', '1')")
            old.commit()
            with closing(db.connect(path, "state", timeout=5)) as new:
                self.assertEqual(new.execute("SELECT value FROM kv WHERE key='a'").fetchone(), ("1",))
                self.assertEqual(db.version(new), len(db.SCHEMAS["state"]))
            self.assertEqual(old.execute("SELECT value FROM kv WHERE key='a'").fetchone(), ("1",))

    def test_switching_to_wal_gives_up_quietly_while_someone_is_reading(self):
        path = self.root / "state/state.db"
        path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(path)) as old, closing(sqlite3.connect(path)) as new:
            old.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            old.commit()
            old.execute("BEGIN")
            old.execute("SELECT * FROM kv").fetchall()  # holds a read lock
            started = time.monotonic()
            db._wal(new, 30)
            self.assertLess(time.monotonic() - started, 5)
            self.assertEqual(new.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            old.rollback()
            db._wal(new, 30)
            self.assertEqual(new.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_read_never_creates_a_file(self):
        with self.assertRaises(sqlite3.Error):
            db.read(self.root / "state/missing.db").execute("SELECT 1")
        self.assertFalse((self.root / "state/missing.db").exists())


if __name__ == "__main__":
    unittest.main()
