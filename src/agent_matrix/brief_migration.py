"""BRIEF-SPEC step 1: additive schema and a reviewable, read-only migration plan.

This command deliberately cannot migrate statuses or assign executors. Existing
rows first need an owner-approved mapping; old runtime readers are still active.
"""
import argparse
from collections import Counter
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re

from .values import single
from .bitable import items, js, require_owned
from .config import load
from .feishu import Feishu
from .models import MatrixError
from .records import read_records
from .schema import field, select, SOURCE_FIELDS
from .state import State, exclusive_lock


STATUS_MAP = {"待确认": "待确认", "待办": "未开始", "未开始": "未开始",
              "进行中": "进行中", "阻塞": "卡住", "卡住": "卡住",
              "草稿已备": "待验收", "待验收": "待验收", "完成": "完成", "取消": "取消"}
EXECUTORS = {"我", "AI", "别人"}


def definitions(table_id):
    return [field("编号", "auto_number", style={"rules": [{"type": "incremental_number", "length": 1}]},
                  description="稳定数字编号；仅自动生成，不重置规则，不手工重排或复用。"),
            select("执行方", ["我", "AI", "别人"]),
            field("进度", description="不超过15字；步骤数＋当前步骤，不写百分比。"),
            field("卡点", description="卡住时必填：缺什么、需要谁给。"),
            field("完成标准", description="一句话验收标准；AI结果须本人回复过。"),
            field("交付物链接", style={"type": "url"}),
            select("验收结果", ["通过", "退回"]), field("退回意见"),
            field("父事项", "link", link_table=table_id, bidirectional=False),
            field("顺延次数", "number", style={"type": "plain", "precision": 0})]




def canonical_status(value):
    state = single(value)
    if state not in STATUS_MAP:
        raise MatrixError("遇到未知状态；需要先确认迁移方式。")
    return STATUS_MAP[state]


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


def check_schema(actual, expected):
    for definition in expected:
        name = definition["name"]
        if name not in actual:
            continue
        found = actual[name]
        if found.get("type") != definition["type"]:
            raise MatrixError("已有字段类型不同，不覆盖：" + name)
        if definition["type"] == "select":
            options = {o.get("name") for o in found.get("options", [])}
            if found.get("multiple") is not False or options != {o["name"] for o in definition["options"]}:
                raise MatrixError("已有单选字段配置不同，不覆盖：" + name)
        if name == "编号" and found.get("style", {}).get("rules") != definition["style"]["rules"]:
            raise MatrixError("已有编号规则不同；不能重置历史编号。")
        if name == "父事项" and (found.get("link_table") != definition["link_table"] or found.get("bidirectional", False)):
            raise MatrixError("已有父事项关联目标不同，不覆盖。")


def setup_fields(config, client, state):
    require_owned(config, state)
    coords = ["--base-token", config.base_token, "--table-id", config.tables["事项表"]]
    expected = definitions(config.tables["事项表"])
    def schema():
        return {x.get("name", x.get("field_name")): x for x in items(client.call("+field-list", *coords))}
    actual = schema(); check_schema(actual, expected)
    key = "brief-v1:fields:" + sha256(js([config.base_token, config.tables["事项表"], expected]).encode()).hexdigest()
    missing = [x for x in expected if x["name"] not in actual]
    previous = state.operation(key)
    if previous and previous["status"] not in {"done", "not_sent"}:
        if missing:
            raise MatrixError("字段创建上次结果未确认；先核对部分成功结果，不重复创建。")
        state.confirm(key, {"verified_by_readback": True, "fields": [x["name"] for x in expected]})
    elif previous and previous["status"] == "done" and missing:
        raise MatrixError("已创建字段后来缺失；不重建编号列，需核对人工修改。")
    elif missing:
        state.once(key, lambda: client.call("+field-create", *coords, "--json", js(missing),
                                           "--i-have-read-guide", write=True))
    actual = schema(); check_schema(actual, expected)
    if any(x["name"] not in actual for x in expected):
        raise MatrixError("新增字段尚未完整读回；暂不报告接通。")
    fields = [*SOURCE_FIELDS, *[x["name"] for x in expected]]
    rows = read_records(client, state, "事项表", fields)
    registry_key = "brief-v1:number-registry:" + config.tables["事项表"]
    registry = validate_registry(rows, state.get(registry_key))
    state.set(registry_key, registry)
    state.set("brief-v1:migration-stage", "fields_ready_awaiting_owner_mapping")
    return {"ok": True, "created_fields": [x["name"] for x in missing], "rows": rows,
            "schema": actual, "stable_numbers_verified": len(rows), "executor_values_written": False,
            "statuses_migrated": False, "messages_sent": 0}


def source_digest(rows):
    columns = ["record_id", *SOURCE_FIELDS, "编号", "执行方", "进度", "卡点", "完成标准",
               "交付物链接", "验收结果", "退回意见", "父事项", "顺延次数"]
    data = [{key: row.get(key) for key in columns} for row in sorted(rows, key=lambda r: r["record_id"])]
    return sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build_plan(rows, proposals, spec_digest):
    """Proposals are explicitly reviewed task-specific choices, not keyword guesses."""
    validate_registry(rows)
    if set(proposals) != {r["record_id"] for r in rows}:
        raise MatrixError("执行方提案没有完整覆盖当前事项；不能用缺省值补齐。")
    planned = []
    for row in sorted(rows, key=lambda r: number(r["编号"])):
        proposal = proposals[row["record_id"]]
        executor = proposal.get("executor")
        if executor not in EXECUTORS or not proposal.get("reason"):
            raise MatrixError("每项提案必须有唯一执行方及来源依据。")
        old_status = single(row["状态"])
        status = canonical_status(old_status)
        split = proposal.get("split")
        if split:
            if executor != "我" or not isinstance(split, dict) or not split.get("ai_title") or not split.get("user_title"):
                raise MatrixError("混合任务须明确AI准备子项和用户执行子项。")
            # No child records are created during planning and no new IDs invented.
            status = "卡住"
        progress = "0/1 等备稿验收" if split else "1/1 已备稿" if status == "待验收" else "0/1 待确认" if status == "待确认" else "0/1 未开始" if status == "未开始" else "进度待核对"
        old_count = row.get("延期次数")
        if old_count is not None and (isinstance(old_count, bool) or not isinstance(old_count, (int, float)) or old_count < 0 or int(old_count) != old_count):
            raise MatrixError("原延期次数异常，不能猜测顺延历史。")
        planned.append({"record_id": row["record_id"], "number": number(row["编号"]),
                        "title": row["标题"], "old_status": old_status, "proposed_status": status,
                        "proposed_executor": executor, "reason": proposal["reason"],
                        "progress": progress, "blocker": "需本人先验收AI准备子项" if split else proposal.get("blocker", ""),
                        "owners": row.get("负责人") or [], "due": row.get("截止日"),
                        "original_due": row.get("首次截止日"), "defer_count": old_count,
                        "defer_history": "保留原延期次数，历史明细缺失不补造", "deliverable": row.get("AI草稿链接"),
                        "split": split, "excluded_from_owner_work": proposal.get("excluded_from_owner_work", False),
                        "missing": [label for label, absent in [("负责人", not row.get("负责人")), ("日期", not row.get("截止日"))] if absent]})
    digest = source_digest(rows)
    plan = {"source_digest": digest, "spec_digest": spec_digest, "approved": False,
            "executor_values_written": False, "statuses_migrated": False, "child_records_created": 0,
            "items": planned, "counts": dict(Counter(x["proposed_executor"] for x in planned)),
            "split_count": sum(bool(x["split"]) for x in planned),
            "deadline_changes": [], "runtime_version": "legacy", "ready_for_activation": False}
    plan["plan_id"] = sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return plan


def verify_approval(plan, rows, approval):
    """Only validates a future explicit owner receipt; this module has no apply path."""
    if not isinstance(approval, dict) or approval.get("decision") != "approve" or not approval.get("owner_message_ref"):
        raise MatrixError("尚未收到本人对执行方映射的明确确认。")
    if approval.get("plan_id") != plan.get("plan_id") or source_digest(rows) != plan.get("source_digest"):
        raise MatrixError("待确认方案或原事项已变化，需要重新列出变更。")
    return True


V1_STATUSES = {"待确认", "未开始", "进行中", "卡住", "待验收", "完成", "取消"}


def _owner_ids(row):
    return [x.get("id") for x in row.get("负责人") or [] if isinstance(x, dict)]


def _date_cell(value):
    from .workflow import day
    found = day(value)
    return found.strftime("%Y-%m-%d 00:00") if found else None


def apply(config, client, state, proposals, now, excluded_numbers=(15,)):
    """Owner-approved migration ("全部做完"): executor + v1 status for every row.

    Every write goes through Gateway (re-read, patch, read back). A per-record
    log with the old values is saved first so the change can be reverted.
    """
    from .values import draft_url as _draft_url
    from .workflow import Gateway, ensure_v1, DEFAULT_PROGRESS
    schema = ensure_v1(client, state)
    gateway = Gateway(client, state)
    rows = gateway.rows()
    out = Path(client.root) / "state/implementation-v1"; out.mkdir(parents=True, exist_ok=True)
    log_path = out / "migration-log.json"
    log = json.loads(log_path.read_text()) if log_path.exists() else {"started": now.isoformat(), "records": {}}
    log["schema"] = schema
    owner = config.owner_open_id
    by_id = {r["record_id"]: r for r in rows}
    results = []

    def save():
        log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2))

    for row in sorted(rows, key=lambda r: number(r["编号"])):
        rid, n = row["record_id"], number(row["编号"])
        if single(row.get("执行方")) or str(row.get("去重键") or "").startswith("split:"):
            continue  # already migrated, or a child created by this migration
        proposal = proposals.get(rid) or {}
        old_status = single(row.get("状态"))
        status = canonical_status(old_status)
        direction = single(row.get("方向"))
        mine = owner in _owner_ids(row)
        executor = proposal.get("executor") or ("别人" if direction in {"别人的", "别人答应我的"} or not mine else "我")
        draft = _draft_url(row.get("AI草稿链接"))
        patch = {"执行方": [executor]}
        excluded = bool(proposal.get("excluded_from_owner_work")) or n in excluded_numbers
        if executor == "别人":
            if status == "待确认":
                status = "未开始"
            excluded = excluded or direction != "别人答应我的"
        patch["状态"] = [status]
        patch["进度"] = {"待验收": "1/1 已交付", "未开始": "0/1 等对方" if executor == "别人" else "0/1 未开始"}.get(status, DEFAULT_PROGRESS[status])
        if draft and not row.get("交付物链接"):
            patch["交付物链接"] = draft
        if excluded:
            patch["排除本人提醒"] = True
        if n in excluded_numbers:
            patch["卡点"] = "你说先不做，暂不提醒"
        split = proposal.get("split")
        log["records"].setdefault(rid, {"number": n, "before": {k: row.get(k) for k in ["执行方", "状态", "进度", "交付物链接", "排除本人提醒", "卡点"]}})
        save()
        if split:
            child_fields = {"标题": split["ai_title"][:60], "执行方": ["AI"], "状态": ["待验收" if draft else "未开始"],
                            "进度": "1/1 已交付" if draft else "0/1 未开始", "负责人": [{"id": owner}],
                            "父事项": [{"id": rid}], "完成标准": "可直接用于：" + str(row.get("标题"))[:30]}
            if draft:
                child_fields["交付物链接"] = draft
            if row.get("截止日"):
                child_fields["截止日"] = _date_cell(row["截止日"])
            if single(row.get("来源")):
                child_fields["来源"] = [single(row.get("来源"))]
            receipt = gateway.create(child_fields, "split:" + rid, "migration:split")
            child_id = receipt["record_id_list"][0]
            child = next(r for r in gateway.rows() if r["record_id"] == child_id)
            child_number = number(child["编号"])
            log["records"][rid]["child"] = {"record_id": child_id, "number": child_number}
            save()
            patch.update({"状态": ["卡住"], "卡点": f"等#{child_number}验收备稿", "进度": "0/1 等备稿验收"})
            patch.pop("交付物链接", None)
        fresh = next(r for r in gateway.rows() if r["record_id"] == rid)
        gateway.update(fresh, patch, f"v1-migrate:{rid}", "migration:owner-approved")
        log["records"][rid]["after"] = patch
        save()
        results.append({"number": n, "executor": executor, "status": patch["状态"][0], "excluded": excluded,
                        "split_child": log["records"][rid].get("child", {}).get("number")})
    state.set("brief-v1:migration-stage", "v1_active")
    log["finished"] = datetime.now().isoformat()
    save()
    return {"ok": True, "schema": schema, "migrated": results, "log": str(log_path.relative_to(client.root))}


def main(argv=None):
    parser = argparse.ArgumentParser(description="简报规范：补字段、稳定编号、迁移执行方和新状态")
    parser.add_argument("command", choices=["setup-fields", "plan", "apply"])
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--proposals", type=Path)
    args = parser.parse_args(argv)
    root = args.config.resolve().parent
    try:
        config = load(args.config)
        with exclusive_lock(root / "state"):
            state = State(root / "state/state.db")
            try:
                client = Feishu(config, root); client.verify_owner(client.auth()); require_owned(config, state)
                out = root / "state/brief-spec-v1"; out.mkdir(exist_ok=True)
                if args.command == "apply":
                    source = args.proposals or out / "executor-proposals.json"
                    # A fresh table has nothing to map; existing rows fall back to owner/direction rules.
                    proposals = json.loads(source.read_text()) if source.exists() else {}
                    result = apply(config, client, state, proposals, datetime.now())
                    print(js(result))
                elif args.command == "setup-fields":
                    result = setup_fields(config, client, state)
                    (out / "fields-and-numbering.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
                    print(js({k: v for k, v in result.items() if k not in {"rows", "schema"}}))
                else:
                    if args.proposals is None:
                        raise MatrixError("生成提案需要已审阅的逐事项映射文件。")
                    rows = read_records(client, state, "事项表", [*SOURCE_FIELDS, *[x["name"] for x in definitions(config.tables["事项表"])]])
                    state.set("brief-v1:number-registry:" + config.tables["事项表"], validate_registry(rows, state.get("brief-v1:number-registry:" + config.tables["事项表"])))
                    result = build_plan(rows, json.loads(args.proposals.read_text()), sha256((root / "BRIEF-SPEC.md").read_bytes()).hexdigest())
                    (out / "migration-plan.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
                    state.set("brief-v1:pending-plan", result)
                    print(js({k: v for k, v in result.items() if k != "items"}))
            finally:
                state.close()
        return 0
    except (MatrixError, OSError, ValueError) as exc:
        print(js({"ok": False, "error": str(exc) if isinstance(exc, MatrixError) else "本地文件读取失败，尚未迁移执行方或状态。"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
