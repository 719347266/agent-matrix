"""The job table (state/xinxiang.db): one row per piece of work an AI has picked up.

The protocol on top of it (next / step / submit) is drive.py."""
from datetime import datetime, timedelta
import json

from . import db as database
from .models import MatrixError
from .timeutil import TZ


def now():
    return datetime.now(TZ)


def _db(root):
    return database.open(root, "xinxiang")


def _save(root, job):
    db = _db(root)
    try:
        with db:
            cursor = db.execute("""INSERT INTO jobs (id, kind, title, n, ref, folder, driver, status, step, rounds, lease_until,
                created, updated, progress_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET status=excluded.status, step=excluded.step, rounds=excluded.rounds,
                    lease_until=excluded.lease_until, updated=excluded.updated, progress_at=excluded.progress_at
                WHERE jobs.status='open'""",
                       (job["id"], job["kind"], job.get("title"), job.get("n"), json.dumps(job.get("ref") or {}, ensure_ascii=False),
                        job.get("folder"), job.get("driver"), job["status"], job.get("step"), job.get("rounds", 0),
                        job.get("lease_until"), job.get("created") or now().isoformat(timespec="seconds"),
                        now().isoformat(timespec="seconds"), job.get("progress_at")))
        return cursor.rowcount > 0
    finally:
        db.close()


def get(root, job_id):
    db = _db(root)
    try:
        row = db.execute("SELECT id, kind, title, n, ref, folder, driver, status, step, rounds, lease_until, created, updated, progress_at "
                         "FROM jobs WHERE id=?", (job_id,)).fetchone()
    finally:
        db.close()
    if not row:
        raise MatrixError("找不到这件活：" + str(job_id))
    keys = ("id", "kind", "title", "n", "ref", "folder", "driver", "status", "step", "rounds", "lease_until", "created", "updated", "progress_at")
    job = dict(zip(keys, row))
    job["ref"] = json.loads(job["ref"] or "{}")
    return job


def open_jobs(root):
    """Jobs someone is working on right now (lease still valid), newest first."""
    db = _db(root)
    try:
        rows = db.execute("SELECT id FROM jobs WHERE status='open' AND lease_until>? ORDER BY updated DESC",
                          (now().isoformat(timespec="seconds"),)).fetchall()
    finally:
        db.close()
    return [get(root, r[0]) for r in rows]


def drivers(root, days=7):
    """Who has been driving lately: name, last seen, jobs finished."""
    db = _db(root)
    try:
        since = (now() - timedelta(days=days)).isoformat()
        return [{"driver": d, "last": last[5:16].replace("T", " "), "done": done, "open": opened}
                for d, last, done, opened in db.execute(
                    "SELECT driver, max(updated), sum(status='done'), sum(status='open') FROM jobs WHERE updated>=? AND driver IS NOT NULL "
                    "GROUP BY driver ORDER BY max(updated) DESC", (since,))]
    finally:
        db.close()


def stop_record(root, record_id, why="本人叫停"):
    """The owner marked an item 不做 / 完成 while an AI works on it: its open jobs stop here."""
    db = _db(root)
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            stopped = [jid for jid, ref in db.execute("SELECT id, ref FROM jobs WHERE kind='item' AND status='open'")
                       if json.loads(ref or "{}").get("record_id") == record_id]
            db.executemany("UPDATE jobs SET status='cancelled',step=?,lease_until=NULL,updated=? WHERE id=?",
                           [(why, now().isoformat(timespec="seconds"), jid) for jid in stopped])
        return stopped
    finally:
        db.close()
