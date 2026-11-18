"""One-shot, explicitly linked Feishu task -> owned Bitable status sync.

No chat discovery, no task modification, no notification and no scheduling.
"""
import argparse
from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from urllib.parse import parse_qs, urlparse
from uuid import UUID

from .automation import Item, Evidence, decide
from .bitable import items, js, require_owned
from .config import load
from .feishu import Feishu
from .models import MatrixError
from .schema import field, select
from .state import State, exclusive_lock


FIELDS = [field("自动检测", "checkbox"),
          select("完成判定", ["人工确认", "关联飞书任务完成"]),
          field("关联任务链接", style={"type": "url"}),
          field("完成证据", description="保留来源链接、源任务完成时间、检测时间；不保存聊天全文。"),
          field("同步凭据", description="自动同步去重标识，不手动修改。")]
PROJECTION = ["标题", "状态", "更新时间", *[f["name"] for f in FIELDS]]


def now_ms():
    return int(time.time() * 1000)


def task_guid(url):
    p = urlparse(url or "")
    if p.scheme != "https" or p.netloc != "applink.feishu.cn" or p.path != "/client/todo/task":
        raise MatrixError("关联任务链接不是可识别的飞书任务链接，需确认来源。")
    values = parse_qs(p.query).get("guid", [])
    try:
        if len(values) != 1:
            raise ValueError()
        return str(UUID(values[0]))
    except ValueError as exc:
        raise MatrixError("关联任务链接缺少有效且唯一的 GUID。") from exc


def parse_row(row):
    status = row.get("状态") or []
    rules = row.get("完成判定") or []
    if not isinstance(status, list) or len(status) != 1 or not isinstance(rules, list):
        raise MatrixError("事项状态或完成判定结构异常，不自动写入。")
    url = row.get("关联任务链接") or ""
    rule = "task_completed" if rules == ["关联飞书任务完成"] else "manual"
    timestamp = row.get("更新时间")
    try:
        instant = datetime.fromisoformat(timestamp) if timestamp else None
        updated = int(instant.timestamp()*1000) if instant and instant.tzinfo else 0
    except (TypeError, ValueError):
        updated = 0
    revision = sha256(js(row).encode()).hexdigest()
    return Item(row["record_id"], row.get("标题") or "未命名事项", status[0], revision, updated,
                task_guid(url) if url else "", rule, row.get("自动检测") is True, row.get("同步凭据") or "")


class LinkedGateway:
    def __init__(self, client, state):
        self.client, self.state = client, state
        self.config, self.root = client.config, client.root
        require_owned(self.config, state)
        from .itemstore import enabled
        if enabled(self.config):  # this old one-shot tool reads the Feishu table itself; the items now live locally
            raise MatrixError("事项已改存本机（item_store: local），这个旧工具不再适用；飞书任务由心响自动同步。")
        self.table_id = self.config.tables["事项表"]
        self.coords = ["--base-token", self.config.base_token, "--table-id", self.table_id]
        self.links = {}

    def schema(self):
        return {f.get("name", f.get("field_name")): f
                for f in items(self.client.call("+field-list", *self.coords))}

    def check_schema(self):
        fields = self.schema()
        missing = [name for name in PROJECTION if name not in fields]
        if missing:
            raise MatrixError("自动同步字段尚未就绪：" + "、".join(missing))
        for f in FIELDS:
            if fields[f["name"]].get("type") != f["type"]:
                raise MatrixError("自动同步字段类型不匹配：" + f["name"])

    def setup(self):
        actual = self.schema()
        missing = [f for f in FIELDS if f["name"] not in actual]
        if missing:
            key = "sync-fields:" + sha256(js(missing).encode()).hexdigest()
            self.state.once(key, lambda: self.client.call("+field-create", *self.coords,
                            "--json", js(missing), "--i-have-read-guide", write=True))
        # Server schema can be eventually consistent. A later setup rechecks; no replay.
        return {"requested_fields": [f["name"] for f in missing], "automatic_detection_enabled": False}

    def read_rows(self, record_id=None):
        projection = [arg for name in PROJECTION for arg in ("--field-id", name)]
        offset, revision, rows = 0, None, []
        (self.root/"state").mkdir(exist_ok=True)
        with TemporaryDirectory(prefix="sync-read-", dir=self.root/"state") as directory:
            for page in range(100):
                output = Path(directory)/f"records-{page}.ndjson"
                argv = ["base", "+record-get" if record_id else "+record-list", *self.coords,
                        *projection, "--as", "user", "--format", "ndjson", "--output", str(output.relative_to(self.root))]
                argv += ["--record-id", record_id] if record_id else ["--offset", str(offset)]
                self.client._exec(argv, manifest=True)
                meta = json.loads(output.with_suffix(".manifest.json").read_text())
                if meta.get("base_token") != self.config.base_token or meta.get("table_id") != self.table_id:
                    raise MatrixError("导出结果与本系统表格不一致，已停止。")
                if "has_more" not in meta or "rev" not in meta:
                    raise MatrixError("导出结果缺少完整性信息，已停止。")
                if revision is not None and meta["rev"] != revision:
                    raise MatrixError("读取期间表格发生变化，请重新读取；本次不写入。")
                revision = meta["rev"]
                chunk = [json.loads(line) for line in output.read_text().splitlines() if line.strip()]
                if len(chunk) != meta.get("records_count"):
                    raise MatrixError("记录数量校验失败，已停止。")
                rows.extend(chunk)
                if meta["has_more"] is False:
                    ids = [r.get("record_id") for r in rows]
                    if None in ids or len(set(ids)) != len(ids):
                        raise MatrixError("事项 ID 缺失或重复，已停止。")
                    return rows
                next_offset = meta.get("next_offset")
                if record_id or not isinstance(next_offset, int) or next_offset <= offset:
                    raise MatrixError("分页游标异常，已停止。")
                offset = next_offset
        raise MatrixError("事项超过单轮读取上限；本次不做部分同步。")

    def get_item(self, item_id):
        rows = self.read_rows(item_id)
        if len(rows) != 1 or rows[0]["record_id"] != item_id:
            raise MatrixError("原事项已不存在或 ID 不一致，不写入。")
        self.links[item_id] = rows[0].get("关联任务链接") or ""
        return parse_row(rows[0])

    def get_evidence(self, item):
        if not item.source_id:
            raise MatrixError("事项尚未关联任务，不读取其他来源。")
        data = self.client._exec(["task", "tasks", "get", "--params", js({"task_guid": item.source_id,
                                "user_id_type": "open_id"}), "--as", "user"])
        task = data.get("task", {})
        if task.get("guid") != item.source_id:
            raise MatrixError("返回任务与关联 GUID 不一致，已停止。")
        try:
            completed = int(task.get("completed_at") or 0)
        except (TypeError, ValueError) as exc:
            raise MatrixError("源任务完成时间异常，不自动完成。") from exc
        return Evidence(f"task:{item.source_id}:{completed}", item.id, item.source_id, "task_status",
                        "completed" if completed > 0 else "incomplete", completed, now_ms(), self.links[item.id])

    def write_complete(self, item, evidence, proof_id):
        require_owned(self.config, self.state)
        raise MatrixError("旧的关联任务自动完成规则已停用；需迁移到待验收并由本人通过，不能直接写完成。")


def run(client, state, *, apply=False):
    gateway = LinkedGateway(client, state)
    gateway.check_schema()
    rows = gateway.read_rows()
    results = []
    for row in rows:
        if row.get("自动检测") is not True:
            continue
        item = parse_row(row)
        if item.rule != "task_completed" or not item.source_id:
            results.append({"item_id": item.id, "status": "review", "reason": "尚未明确关联来源和完成条件"})
            continue
        gateway.links[item.id] = row["关联任务链接"]
        evidence = gateway.get_evidence(item)
        decision = asdict(decide(item, evidence, now_ms=now_ms()))
        if decision["action"] == "complete":
            decision = {"action": "review", "reason": "关联来源已交付；等待新状态迁移后进入待验收，不能直接完成。"}
        results.append({"item_id": item.id, **decision, "checked": False, "written": False})
    return {"ok": True, "mode": "apply" if apply else "read_only_scan", "records_read": len(rows),
            "tracked_records": len(results), "results": results, "messages_sent": 0, "scheduled": False,
            "completion_writes_disabled": True, "review_writeback_pending_migration": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description="仅同步事项表明确关联的飞书任务")
    parser.add_argument("command", choices=["setup", "scan", "apply"])
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    args = parser.parse_args(argv)
    path = args.config.resolve()
    try:
        config = load(path)
        with exclusive_lock(path.parent/"state"):
            state = State(path.parent/"state/state.db")
            run_id = state.begin("sync-linked-" + args.command)
            try:
                client = Feishu(config, path.parent)
                client.verify_owner(client.auth())
                if args.command == "setup":
                    result = LinkedGateway(client, state).setup()
                else:
                    result = run(client, state, apply=args.command == "apply")
                state.finish(run_id, True)
                print(json.dumps(result, ensure_ascii=False, indent=2))
            except Exception as exc:
                state.finish(run_id, False, type(exc).__name__)
                raise
            finally:
                state.close()
        return 0
    except (MatrixError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc) if isinstance(exc, MatrixError)
                          else "本地配置或记录解析异常，本次停止同步。"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
