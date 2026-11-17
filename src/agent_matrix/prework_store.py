"""Durable prework queue. ``done`` means delivered for review, never business completion.

The caller still owns external-write idempotency via State.once and the project lock.
This queue neither sends messages nor marks the source business item complete.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
from pathlib import PurePath
import re
from zoneinfo import ZoneInfo

from .delivery import message_id, require_message_receipt
from .models import MatrixError


TZ = ZoneInfo("Asia/Shanghai")
ACTIVE = {"queued", "running", "prepared"}
TERMINAL = {"done", "blocked", "stale"}
COLUMNS = (
    "record_id", "input_version", "candidate", "status", "datekey", "created_at", "updated_at",
    "next_attempt_at", "lease_until", "attempts", "claim_count", "result_path", "result_sha256",
    "prepared_at", "receipt", "delivered_at", "reason",
)


def _clock(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        if not isinstance(result, datetime) or result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        return result.astimezone(TZ)
    except (TypeError, ValueError, OverflowError):
        raise MatrixError("预做队列时间须为带时区的 ISO 时间或 datetime。") from None


def _iso(value):
    return _clock(value).isoformat(timespec="microseconds")


def _now():
    return datetime.now(TZ)


def _key(record_id, version):
    if not all(isinstance(value, str) and value.strip() and len(value) <= 512
               and not any(ord(char) < 32 for char in value) for value in (record_id, version)):
        raise MatrixError("预做候选必须有有效的 record_id 和 input_version。")
    return record_id, version


def _json(value, label):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise MatrixError(label + "必须是可持久化的 JSON 数据。") from None


def _safe_reason(reason):
    """Save readable categories, not raw errors that can contain credentials or payloads."""
    text = str(reason or "").strip()
    lower = text.lower()
    # Preserve concrete short operator explanations (e.g. which materials are
    # missing), while arbitrary raw exception payloads still use safe categories.
    if text and len(text) <= 160 and re.fullmatch(r"[\u3400-\u9fff\s，。！？；：、（）《》“”‘’—…]+", text) and not any(word in text for word in ("密码", "密钥", "凭据")):
        return " ".join(text.split())
    categories = (
        (("timeout", "timed out", "超时", "网络", "connection"), "连接或处理超时，稍后再试。"),
        (("token", "credential", "secret", "password", "authorization", "auth", "授权", "登录", "密码", "密钥"),
         "授权或凭据状态需核对；未保存原始错误详情。"),
        (("message_id", "receipt", "回执", "送达"), "消息送达结果待核对，避免重复发送。"),
        (("missing", "material", "缺少", "缺失", "材料", "source"), "必要材料缺失或暂不可用，需要补齐后继续。"),
        (("confirmation", "confirm", "approval", "确认", "审批"), "需要本人确认后继续。"),
        (("sha256", "hash", "checksum", "validation", "校验"), "生成结果校验未通过，需要重新核对。"),
    )
    for words, safe in categories:
        if any(word in lower for word in words):
            return safe
    return "预做处理未完成，需要核对执行情况；未保存原始错误详情。"


class WorkQueue:
    def __init__(self, state):
        self.state = state
        self.db = state.db
        self.db.execute("""CREATE TABLE IF NOT EXISTS prework_queue (
            record_id TEXT NOT NULL, input_version TEXT NOT NULL, candidate TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('queued','running','prepared','done','blocked','stale')),
            datekey TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            next_attempt_at TEXT, lease_until TEXT, attempts INTEGER NOT NULL DEFAULT 0,
            claim_count INTEGER NOT NULL DEFAULT 0, result_path TEXT, result_sha256 TEXT,
            prepared_at TEXT, receipt TEXT, delivered_at TEXT, reason TEXT,
            PRIMARY KEY(record_id,input_version))""")
        self.db.execute("CREATE INDEX IF NOT EXISTS prework_queue_ready ON prework_queue(status,next_attempt_at,lease_until)")
        self.db.commit()

    @contextmanager
    def _transaction(self):
        # Serialize a read/check/write transition without committing unrelated caller work.
        # Callers should still hold the project lock across preparation and external writes.
        if self.db.in_transaction:
            self.db.execute("SAVEPOINT prework_transition")
            try:
                yield
            except Exception:
                self.db.execute("ROLLBACK TO prework_transition")
                self.db.execute("RELEASE prework_transition")
                raise
            else:
                self.db.execute("RELEASE prework_transition")
        else:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self.db.rollback()
                raise
            else:
                self.db.commit()

    def _decode(self, row):
        if row is None:
            return None
        result = dict(zip(COLUMNS, row))
        result["candidate"] = json.loads(result["candidate"])
        result["receipt"] = json.loads(result["receipt"]) if result["receipt"] else None
        result["result_hash"] = result["result_sha256"]
        result["delivered_for_review"] = result["status"] == "done"
        return result

    def get(self, record_id, version):
        key = _key(record_id, version)
        return self._decode(self.db.execute(
            "SELECT " + ",".join(COLUMNS) + " FROM prework_queue WHERE record_id=? AND input_version=?", key).fetchone())

    def _require(self, record_id, version):
        result = self.get(record_id, version)
        if result is None:
            raise MatrixError("预做候选不存在，不能跳过发现步骤修改进度。")
        return result

    def list(self):
        return [self._decode(row) for row in self.db.execute(
            "SELECT " + ",".join(COLUMNS) + " FROM prework_queue ORDER BY created_at,record_id,input_version")]

    def discover(self, candidates, now):
        stamp = _iso(now)
        if not isinstance(candidates, list):
            raise MatrixError("预做候选须为完整列表。")
        decoded, versions, unique = [], {}, set()
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise MatrixError("预做候选格式异常。")
            key = _key(candidate.get("record_id"), candidate.get("input_version"))
            if key[0] in versions and versions[key[0]] != key[1]:
                raise MatrixError("同一次扫描出现同一事项的多个输入版本，需先核对材料。")
            versions[key[0]] = key[1]
            if key not in unique:
                decoded.append((key, _json(candidate, "预做候选")))
                unique.add(key)
        with self._transaction():
            for (record_id, version), payload in decoded:
                if self.get(record_id, version):
                    # Repeated snapshots cannot revive stale work or reset a lease/backoff.
                    continue
                self.db.execute("""UPDATE prework_queue SET status='stale', updated_at=?,
                    reason='输入材料已变化，旧结果仅保留核对，不再执行。'
                    WHERE record_id=? AND input_version<>? AND status IN ('queued','running','prepared')""",
                                (stamp, record_id, version))
                self.db.execute("""INSERT INTO prework_queue
                    (record_id,input_version,candidate,status,datekey,created_at,updated_at,next_attempt_at)
                    VALUES(?,?,?,'queued',?,?,?,?)""",
                                (record_id, version, payload, _clock(now).date().isoformat(), stamp, stamp, stamp))
        return [self.get(*key) for key, _ in decoded]

    def ready(self, now):
        stamp = _iso(now)
        return [self._decode(row) for row in self.db.execute(
            "SELECT " + ",".join(COLUMNS) + """ FROM prework_queue
            WHERE status IN ('queued','running','prepared')
            AND (next_attempt_at IS NULL OR next_attempt_at<=?)
            AND (lease_until IS NULL OR lease_until<=?)
            ORDER BY next_attempt_at,created_at,record_id,input_version""", (stamp, stamp))]

    def invalidate(self, record_id, version, now):
        """A complete source scan proved this snapshot is no longer actionable."""
        with self._transaction():
            self._require(record_id, version)
            self.db.execute("""UPDATE prework_queue SET status='stale',updated_at=?,
                lease_until=NULL,next_attempt_at=NULL,reason='原事项已变化或不再允许预做。'
                WHERE record_id=? AND input_version=? AND status IN ('queued','running','prepared','blocked')""",
                (_iso(now), record_id, version))
            return self.get(record_id, version)

    def claim(self, record_id, version, now, lease_minutes=30):
        stamp = _iso(now)
        if isinstance(lease_minutes, bool) or not isinstance(lease_minutes, (int, float)) or not 0 < lease_minutes <= 1440:
            raise MatrixError("预做租约须大于零且不超过一天。")
        until = _iso(_clock(now) + timedelta(minutes=lease_minutes))
        with self._transaction():
            current = self._require(record_id, version)
            if current["status"] not in ACTIVE:
                return None
            changed = self.db.execute("""UPDATE prework_queue SET status='running',lease_until=?,
                updated_at=?,claim_count=claim_count+1 WHERE record_id=? AND input_version=?
                AND status IN ('queued','running','prepared')
                AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                AND (lease_until IS NULL OR lease_until<=?)""",
                                      (until, stamp, record_id, version, stamp, stamp)).rowcount
            return self.get(record_id, version) if changed else None

    def prepared(self, record_id, version, result_path, result_sha256, now):
        stamp = _iso(now)
        if isinstance(result_path, PurePath):
            result_path = str(result_path)
        if not isinstance(result_path, str) or not result_path.strip() or "\x00" in result_path:
            raise MatrixError("预做结果需提供有效文件路径。")
        if not isinstance(result_sha256, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", result_sha256):
            raise MatrixError("预做结果需提供完整 SHA-256 校验值。")
        digest = result_sha256.lower()
        with self._transaction():
            current = self._require(record_id, version)
            if current["status"] == "prepared":
                if (current["result_path"], current["result_sha256"]) == (result_path, digest):
                    return current
                raise MatrixError("已有预做结果不同，不能覆盖待确认的材料证据。")
            if current["status"] != "running" or not current["lease_until"] or current["lease_until"] <= stamp:
                raise MatrixError("预做租约无效或输入版本已失效，不能登记生成结果。")
            if current["result_path"] and (current["result_path"], current["result_sha256"]) != (result_path, digest):
                raise MatrixError("续接已有材料时结果发生变化，需核对版本后处理。")
            self.db.execute("""UPDATE prework_queue SET status='prepared',result_path=?,result_sha256=?,
                prepared_at=COALESCE(prepared_at,?),updated_at=? WHERE record_id=? AND input_version=?""",
                            (result_path, digest, stamp, stamp, record_id, version))
            return self.get(record_id, version)

    def done(self, record_id, version, receipt):
        require_message_receipt(receipt)
        payload = _json(receipt, "送达回执")
        with self._transaction():
            current = self._require(record_id, version)
            if current["status"] == "done":
                require_message_receipt(current["receipt"])
                if message_id(current["receipt"]) != message_id(receipt):
                    raise MatrixError("该预做结果已有不同送达回执，不覆盖原始证据。")
                return current
            if current["status"] != "prepared":
                raise MatrixError("预做结果尚未备好、已失效或已阻塞，不能标记送达待确认。")
            stamp = _iso(_now())
            self.db.execute("""UPDATE prework_queue SET status='done',receipt=?,delivered_at=?,
                updated_at=?,lease_until=NULL,next_attempt_at=NULL,reason=NULL
                WHERE record_id=? AND input_version=?""", (payload, stamp, stamp, record_id, version))
            return self.get(record_id, version)

    def block(self, record_id, version, reason):
        with self._transaction():
            current = self._require(record_id, version)
            if current["status"] == "blocked":
                return current
            if current["status"] not in ACTIVE:
                raise MatrixError("已送达或已失效的预做记录不能改为阻塞。")
            self.db.execute("""UPDATE prework_queue SET status='blocked',reason=?,updated_at=?,
                lease_until=NULL,next_attempt_at=NULL WHERE record_id=? AND input_version=?""",
                            (_safe_reason(reason), _iso(_now()), record_id, version))
            return self.get(record_id, version)

    def defer(self, record_id, version, now, reason):
        stamp = _iso(now)
        with self._transaction():
            current = self._require(record_id, version)
            if current["status"] == "blocked":
                return current
            if current["status"] not in ACTIVE:
                raise MatrixError("已送达或已失效的预做记录不能重新排队。")
            if current["status"] == "queued" and current["next_attempt_at"] and current["next_attempt_at"] > stamp:
                return current
            attempts = current["attempts"] + 1
            blocked = attempts >= 3
            # Failure 1: 30 minutes; failure 2: 60; failure 3 requires human review.
            following = None if blocked else _iso(_clock(now) + timedelta(minutes=30 * 2 ** (attempts - 1)))
            self.db.execute("""UPDATE prework_queue SET status=?,attempts=?,next_attempt_at=?,lease_until=NULL,
                updated_at=?,reason=? WHERE record_id=? AND input_version=?""",
                            ("blocked" if blocked else "queued", attempts, following, stamp,
                             _safe_reason(reason), record_id, version))
            return self.get(record_id, version)
