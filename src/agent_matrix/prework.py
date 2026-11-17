"""Owner-scoped prework: assess, prepare artifacts, publish, then request review.

This module never runs task text as commands, submits approvals, publishes to
external audiences, or marks the underlying business task complete.
"""
import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Literal
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from pydantic import BaseModel, ConfigDict, Field
from .agents.pre_worker import FIELDS, assess, fingerprint
from .bitable import items, js, require_owned
from .config import load
from .delivery import send_once, require_message_receipt
from .feishu import Feishu, FeishuError
from .models import MatrixError, MatrixBusy
from .prework_store import WorkQueue
from .records import read_records, ensure_fields
from .schema import field, select
from .state import State, exclusive_lock
from .timeutil import TZ


EXTRA_FIELDS = [select("预做策略", ["自动判断", "仅备草稿", "不代做"]), field("预做指令"),
                field("预做结果摘要"), field("预做凭据", description="预做输入版本；用于防止覆盖人工修改。")]
OUTPUT_FIELDS = ["AI草稿链接", "预做结果摘要", "预做凭据"]


# Each deliverable type has required sections and a length cap (FIX 2.2 / 3.1).
TEMPLATES = {
    "周报": (["本周完成", "进行中", "下周计划", "风险"], 800),
    "数据汇总": (["结论", "口径"], 2000),
    "方案": (["推荐", "对比", "拍板"], 1200),
    "会议材料": (["议程", "决策", "背景"], 800),
    "回复草稿": (["草稿", "确认"], 800),
    "其他": ([], 1500),
}
PLACEHOLDERS = ["TODO", "待补充", "XX", "（略）", "(略)", "此处填写", "待完善"]


class Result(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    input_version: str = Field(pattern=r"^[a-f0-9]{64}$")
    kind: Literal["internal_result", "draft"]
    template: Literal["周报", "数据汇总", "方案", "会议材料", "回复草稿", "其他"]
    # One line in the 12:00/17:00 batch: "#15 周报初稿 · <summary>".
    summary: str = Field(min_length=1, max_length=40)
    artifact_path: str
    artifact_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    # Every completion criterion ticked, each with where it is met.
    checks: list[str] = Field(min_length=1, max_length=20)
    source_refs: list[str] = Field(min_length=1, max_length=30)
    # Half-finished work is not delivered: missing material means `prework block`.
    limitations: list[str] = Field(default_factory=list, max_length=0)
    confirmation: str = Field(default="", max_length=30)
    external_actions_performed: Literal[False] = False


def key_for(record_id, version):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", record_id or "") or not re.fullmatch(r"[a-f0-9]{64}", version or ""):
        raise MatrixError("预做事项 ID 或输入版本无效。")
    return f"prework:{record_id}:{version}"


def local_path(root, path):
    candidate = Path(path)
    candidate = candidate if candidate.is_absolute() else root / candidate
    candidate = candidate.resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise MatrixError("预做文件不在当前项目内。")
    return candidate


def _status(row):
    value = row.get("状态") or []
    return value[0] if isinstance(value, list) and len(value) == 1 else value if isinstance(value, str) else ""


def _saved_url(config, state, key):
    operation = state.operation(key+":doc")
    doc = (operation or {}).get("receipt") or {}
    doc = doc.get("document", {})
    url, token = doc.get("url", ""), doc.get("document_id", "")
    parsed = urlparse(url)
    if token and parsed.scheme == "https" and parsed.hostname == urlparse(config.base_url).hostname and parsed.path == "/docx/"+token:
        return url
    return None


def _draft_url(value):
    """Read one actual hyperlink, never the Markdown label or an ambiguous list."""
    if isinstance(value, dict):
        value = value.get("url")
    if isinstance(value, list):
        return _draft_url(value[0]) if len(value) == 1 else None
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


def _our_draft(row, version, url):
    expected = _draft_url(url)
    return bool(expected and _status(row) in {"草稿已备", "待验收"} and row.get("预做凭据") == version
                and _draft_url(row.get("AI草稿链接")) == expected)


def setup(config, client, state):
    require_owned(config, state)
    coords = ["--base-token", config.base_token, "--table-id", config.tables["事项表"]]
    actual = {f.get("name", f.get("field_name")): f for f in items(client.call("+field-list", *coords))}
    for definition in EXTRA_FIELDS:
        old = actual.get(definition["name"])
        if old and old.get("type") != definition["type"]:
            raise MatrixError("预做字段类型与预期不一致：" + definition["name"])
    missing = [definition for definition in EXTRA_FIELDS if definition["name"] not in actual]
    if missing:
        key = "prework-fields:" + sha256(js(missing).encode()).hexdigest()
        state.once(key, lambda: client.call("+field-create", *coords, "--json", js(missing), "--i-have-read-guide", write=True))
    return {"ok": True, "requested_fields": [x["name"] for x in missing]}


def _v1(state):
    return state.get("brief-v1:migration-stage") == "v1_active"


def _rows(config, client, state):
    require_owned(config, state)
    from .agents.pre_worker import V1_FIELDS
    fields = [*FIELDS, *OUTPUT_FIELDS, *(V1_FIELDS + ["交付物链接"] if _v1(state) else [])]
    ensure_fields(client, "事项表", fields)
    return read_records(client, state, "事项表", fields)


def _claims_today(state, now):
    # Recorded for the record only; the owner asked for no cap on how much AI takes on.
    return state.get("prework-new-claims:" + now.date().isoformat(), [])


def check(config, client, state, now=None):
    if not config.prework_enabled:
        return {"ok": True, "run_model": False, "reason": "prework_disabled", "ready": []}
    if state.get("brief-v1:migration-stage") == "fields_ready_awaiting_owner_mapping":
        return {"ok": True, "run_model": False, "reason": "executor_mapping_pending",
                "ready": [], "blocked_to_notify": 0,
                "note": "执行方映射尚待本人确认；迁移期间不按旧规则接新事项或催确认。"}
    now = now or datetime.now(TZ)
    source_rows = _rows(config, client, state)
    result = assess(config, source_rows, today=now.date())
    if not result["ok"]:
        raise MatrixError("无法核验预做负责人，不执行。")
    queue = WorkQueue(state)
    queue.discover(result["candidates"], now)
    eligible = {(r["record_id"], r["input_version"]) for r in result["candidates"]}
    sources = {r["record_id"]: r for r in source_rows}
    for old in queue.list():
        pair = (old["record_id"], old["input_version"])
        if old["status"] not in {"queued", "running", "prepared", "blocked"} or pair in eligible:
            continue
        fresh = sources.get(pair[0])
        url = _saved_url(config, state, key_for(*pair))
        if fresh and fingerprint(fresh) == pair[1] and _our_draft(fresh, pair[1], url):
            continue
        queue.invalidate(*pair, now)
    def unknown_delivery(row):
        batch = state.get(key_for(row["record_id"], row["input_version"])+":batch")
        operation = state.operation(batch) if batch else None
        return bool(operation and operation["status"] not in {"done", "not_sent", "cancelled"})
    unresolved = [r for r in queue.list() if unknown_delivery(r)]
    ready = [r for r in queue.ready(now) if not unknown_delivery(r)]
    ready.sort(key=lambda r: (r["candidate"].get("due_date") is None, r["candidate"].get("due_date") or "", r["record_id"]))
    unnotified = [r for r in queue.list() if r["status"] == "blocked" and not unknown_delivery(r)
                 and not state.get(key_for(r["record_id"], r["input_version"])+":notice")]
    if _v1(state):
        # v1: results/blockers not yet sent wake the run so `notify` sends them right away.
        unnotified = [r for r in queue.list() if not state.get(key_for(r["record_id"], r["input_version"])+":sent-now")
                      and (r["status"] == "blocked" or state.get(key_for(r["record_id"], r["input_version"])+":published"))
                      and r["status"] not in {"stale", "done"}]
    return {"ok": True, "run_model": bool(ready or unnotified), "reason": "actionable" if ready or unnotified else "idle",
            "ready": [{"record_id": r["record_id"], "input_version": r["input_version"], "status": r["status"],
                       "title": r["candidate"]["title"], "due_date": r["candidate"].get("due_date")} for r in ready],
            "blocked_to_notify": len(unnotified), "candidate_count": result["candidate_count"],
            "awaiting_receipt_count": len(unresolved), "skipped": result["skipped"]}


def current(config, client, state, record_id, version, *, published=None):
    if not config.prework_enabled:
        raise MatrixError("预做开关已关闭。")
    row = next((r for r in _rows(config, client, state) if r["record_id"] == record_id), None)
    if not row or fingerprint(row) != version:
        raise MatrixError("原事项已删除或内容有变化，旧结果不会覆盖新内容。")
    if _our_draft(row, version, published):
        return row
    candidate = assess(config, [row])["candidates"]
    if not candidate:
        raise MatrixError("事项已完成、取消、改归属或不再允许预做。")
    return row


def prepare(config, client, state, record_id, version):
    key = key_for(record_id, version)
    current(config, client, state, record_id, version, published=_saved_url(config, state, key))
    queue = WorkQueue(state)
    now = datetime.now(TZ)
    claimed = queue.claim(record_id, version, now)
    if not claimed:
        raise MatrixError("预做事项已有处理者或尚未到重试时间。")
    if _v1(state) and not state.get("prework-ever-claimed:" + record_id):
        if not claimed["candidate"].get("rework"):
            today = _claims_today(state, now)
            if record_id not in today:
                state.set("prework-new-claims:" + now.date().isoformat(), [*today, record_id])
        state.set("prework-ever-claimed:" + record_id, now.isoformat())
    if claimed["result_path"]:
        result_file = local_path(client.root, claimed["result_path"])
        if sha256(result_file.read_bytes()).hexdigest() != claimed["result_sha256"]:
            raise MatrixError("中断前的结果文件已变化，需核对后再续接。")
        claimed = queue.prepared(record_id, version, claimed["result_path"], claimed["result_sha256"], datetime.now(TZ))
    directory = client.root / "state/prework" / (record_id + "-" + version[:16])
    directory.mkdir(parents=True, exist_ok=True); directory.chmod(0o700)
    context = directory / "context.json"
    context.write_text(js(claimed["candidate"])); context.chmod(0o600)
    return {"ok": True, "context_path": str(context), "row": claimed,
            "draft": state.get(key+":draft"), "published": state.get(key+":published")}


def draft_init(config, client, state, record_id, version, decision):
    key = key_for(record_id, version)
    current(config, client, state, record_id, version)
    row = WorkQueue(state).get(record_id, version)
    if not row or row["status"] != "running":
        raise MatrixError("先领取预做事项，再开始制作。")
    saved = state.get(key+":draft")
    if saved:
        return saved
    if not isinstance(decision, dict) or not decision.get("audience") or not decision.get("reader_task"):
        raise MatrixError("文档缺少读者、阅读任务和版式决策。")
    receipt = state.once(key+":draft-init", lambda: client._exec([
        "docs", "+script", "--command", "init-draft", "--presentation-decision", js(decision), "--format", "json"]))
    cwd = Path(receipt.get("cwd", "")).resolve()
    if cwd != client.root.resolve():
        raise MatrixError("文档草稿创建到了不同工作目录，停止发布。")
    path = local_path(client.root, receipt.get("draft_path", ""))
    if path.suffix != ".xml" or not (path.parent/".presentation-decision.json").is_file():
        raise MatrixError("文档初始化缺少有效草稿路径或版式基线。")
    result = {"draft_path": str(path.relative_to(client.root)), "workspace": str(path.parent.relative_to(client.root))}
    state.set(key+":draft", result)
    return result


def _xml_text(value):
    if "<!DOCTYPE" in value.upper() or "<!ENTITY" in value.upper():
        raise MatrixError("预做文档不接受外部实体或自定义声明。")
    try:
        node = ET.fromstring("<document>" + value + "</document>")
    except ET.ParseError as exc:
        raise MatrixError("预做文档 XML 不完整，不能发布。") from exc
    return "".join("".join(node.itertext()).split())


def stage(config, client, state, result_path):
    path = local_path(client.root, result_path)
    result = Result.model_validate_json(path.read_text())
    key = key_for(result.record_id, result.input_version)
    fresh = current(config, client, state, result.record_id, result.input_version)
    if fresh.get("预做策略") in (["仅备草稿"], "仅备草稿") and result.kind != "draft":
        raise MatrixError("该事项只允许准备草稿。")
    draft = state.get(key+":draft")
    artifact = local_path(client.root, result.artifact_path)
    if not draft or artifact != local_path(client.root, draft["draft_path"]):
        raise MatrixError("结果不属于本事项初始化的草稿。")
    data = artifact.read_bytes()
    if not data or len(data) > 2_000_000 or sha256(data).hexdigest() != result.artifact_sha256:
        raise MatrixError("预做文件缺失、过大或指纹不一致。")
    text = _xml_text(data.decode())
    if not text.strip():
        raise MatrixError("结果文档为空。")
    sections, cap = TEMPLATES[result.template]
    missing = [name for name in sections if name not in text]
    if missing:
        raise MatrixError("没做完：模板必备栏目缺少「" + "、".join(missing) + "」，补齐后再交付。")
    found = [word for word in PLACEHOLDERS if word in text]
    if found:
        raise MatrixError("没做完：文档里还有占位文字「" + "、".join(found) + "」。")
    if len(text) > cap:
        raise MatrixError(f"写太长了：{len(text)} 字，{result.template}上限 {cap} 字；先给结论，删掉铺垫。")
    if _v1(state) and result.template == "其他" and not (fresh.get("完成标准") or "").strip():
        raise MatrixError("事项没有完成标准，不能按「其他」交付；先在卡点里问本人做到什么程度算完。")
    check_result = client._exec(["docs", "+script", "--command", "parse", "--content", "@./"+str(artifact.relative_to(client.root)), "--format", "json"])
    if check_result.get("assessment", {}).get("status") != "passed":
        raise MatrixError("文档版式或资源检查未通过，不能发布。")
    canonical = js(result.model_dump())
    old = state.get(key+":validated")
    if old is not None and old != canonical:
        raise MatrixError("该事项已有确定的预做结果，不自动改版或重复发布。")
    state.set(key+":validated", canonical)
    WorkQueue(state).prepared(result.record_id, result.input_version, str(path.relative_to(client.root)), sha256(path.read_bytes()).hexdigest(), datetime.now(TZ))
    return {"ok": True, "status": "prepared", "record_id": result.record_id, "input_version": result.input_version}


def publish(config, client, state, record_id, version):
    key = key_for(record_id, version)
    queue = WorkQueue(state); row = queue.get(record_id, version)
    if not row or row["status"] not in {"prepared", "running", "done"}:
        raise MatrixError("该事项尚无可发布的预做结果。")
    old = state.get(key+":published")
    if old:
        current(config, client, state, record_id, version, published=old["url"])
        return old
    current(config, client, state, record_id, version, published=_saved_url(config, state, key))
    pending = [x for x in state.pending() if x.startswith("prework:"+record_id+":") and state.operation(x)["status"] != "not_sent"]
    if pending:
        raise MatrixError("该事项存在未确认的历史写入，先核对后再发布。")
    validated = state.get(key+":validated")
    if not validated:
        raise MatrixError("没有通过文档校验的结果。")
    result = Result.model_validate_json(validated)
    artifact = local_path(client.root, result.artifact_path)
    if sha256(artifact.read_bytes()).hexdigest() != result.artifact_sha256:
        raise MatrixError("结果文件在校验后变化，停止发布。")
    argv = ["docs", "+create", "--content", "@./"+str(artifact.relative_to(client.root)),
            "--doc-format", "xml", "--parent-position", "my_library", "--as", "user"]
    def create():
        try:
            client._exec([*argv, "--dry-run"])
        except FeishuError as exc:
            exc.not_sent = True
            raise
        return client._exec(argv)
    receipt = state.once(key+":doc", create)
    doc = receipt.get("document", {})
    url, token = doc.get("url", ""), doc.get("document_id", "")
    parsed = urlparse(url)
    if not token or parsed.scheme != "https" or parsed.hostname != urlparse(config.base_url).hostname or parsed.path != "/docx/"+token:
        raise MatrixError("新文档回执的 ID 或地址未核验，不发送结果。")
    if receipt.get("warnings"):
        raise MatrixError("文档已创建但存在局部警告；保留原文档，修复后再交付，不重新创建。")
    fetched = client._exec(["docs", "+fetch", "--doc", token, "--detail", "with-ids", "--doc-format", "xml", "--as", "user"])
    remote = fetched.get("document", {})
    expected = _xml_text(artifact.read_text())
    if remote.get("document_id") not in (None, token) or expected not in _xml_text(remote.get("content", "")):
        raise MatrixError("文档正文读回不一致，暂不标记结果已备。")
    current(config, client, state, record_id, version, published=url)
    patch = {"状态": ["待验收" if _v1(state) else "草稿已备"], "AI草稿链接": url, "预做结果摘要": result.summary, "预做凭据": version}
    if _v1(state):
        patch.update({"交付物链接": url, "进度": "1/1 已交付", "卡点": None})
    def update():
        response = client.call("+record-batch-update", "--base-token", config.base_token, "--table-id", config.tables["事项表"],
                               "--json", js({"update_records": {record_id: patch}}), write=True)
        if response.get("ignored_fields"):
            raise MatrixError("预做结果回写有字段被忽略，需核对，不自动重试。")
        fresh = current(config, client, state, record_id, version, published=url)
        if any((_draft_url(fresh.get(name)) != _draft_url(value)) if name in {"AI草稿链接", "交付物链接"}
               else (fresh.get(name) or None) != value for name, value in patch.items()):
            raise MatrixError("结果回写尚未读回确认，保留待核对。")
        return {"verified_by_readback": True}
    state.once(key+":writeback", update)
    published = {"record_id": record_id, "input_version": version, "title": row["candidate"]["title"], "url": url,
                 "summary": result.summary, "confirmation": result.confirmation, "limitations": result.limitations,
                 "kind": result.kind, "status": "awaiting_owner_confirmation"}
    state.set(key+":published", published)
    return published


def notify(config, client, state, max_items=3):
    if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 8:
        raise MatrixError("合并发送条数必须是 1 到 8 的整数。")
    if _v1(state):
        return notify_now(config, client, state)
    queue = WorkQueue(state)
    notices = []
    for row in queue.list():
        key = key_for(row["record_id"], row["input_version"])
        if state.get(key+":notice") or row["status"] in {"stale", "done"}:
            continue
        published = state.get(key+":published")
        if published:
            current(config, client, state, row["record_id"], row["input_version"], published=published["url"])
            notices.append({"key": key, "row": row, "result": published})
        elif row["status"] == "blocked":
            notices.append({"key": key, "row": row, "result": None})
    if not notices:
        return {"ok": True, "sent": False, "reason": "no_new_results"}
    # Persist batch membership before sending; interrupted marking never regroups
    # an already-sent item into a different message key.
    old_batch = next((state.get(n["key"]+":batch") for n in notices if state.get(n["key"]+":batch")), None)
    if old_batch:
        batch = old_batch; members = state.get(batch+":members")
        if not members:
            raise MatrixError("合并发送缺少原批次，需核对回执。")
        notices = [n for n in notices if n["key"] in members]
    else:
        notices = notices[:max_items]
        members = [n["key"] for n in notices]
        batch = "prework-notify-"+sha256(js(members).encode()).hexdigest()[:32]
        state.set(batch+":members", members)
        for n in notices:
            state.set(n["key"]+":batch", batch)
    lines = ["【预做结果｜请你确认】"]
    for n in notices:
        row, result = n["row"], n["result"]
        if result:
            lines += ["", row["candidate"]["title"], result["summary"], result["url"], "请确认："+result["confirmation"]]
            if result["limitations"]:
                lines += ["待补充："+"；".join(result["limitations"])]
        else:
            lines += ["", row["candidate"]["title"], "暂未执行："+str(row.get("reason") or row.get("error") or "需要补充材料或确认")]
    lines += ["", "可在 Cindy 对话里回复事项名称和修改意见。结果已备不等于业务事项已验收，尚未自动打完成勾。"]
    text = "\n".join(lines)
    frozen = state.get(batch+":text")
    if frozen is not None:
        text = frozen
    if len(text.encode("utf-8")) > 18000:
        raise MatrixError("合并消息超过 18000 字节，未发送、未标记送达；请核对完整内容后再处理。")
    if frozen is None:
        state.set(batch+":text", text)
    receipt = send_once(state, batch, lambda: client.send_self(text, batch))
    for n in notices:
        if n["result"]:
            queue.done(n["row"]["record_id"], n["row"]["input_version"], receipt)
        state.set(n["key"]+":notice", receipt)
    return {"ok": True, "sent": True, "count": len(notices), "receipt": require_message_receipt(receipt)}


def notify_now(config, client, state):
    """Owner wants control: every finished result and every new blocker goes out right away.

    One short line per item; all items finished in this run share one message.
    """
    from .workflow import Gateway
    queue = WorkQueue(state)
    pending = []
    for row in queue.list():
        key = key_for(row["record_id"], row["input_version"])
        if state.get(key+":sent-now") or row["status"] in {"stale", "done"}:
            continue
        published = state.get(key+":published")
        if published:
            pending.append(("result", key, row, published))
        elif row["status"] == "blocked":
            pending.append(("blocked", key, row, None))
    if not pending:
        return {"ok": True, "sent": False, "reason": "no_new_results"}
    from . import brief_v1
    rows = {r["record_id"]: r for r in Gateway(client, state).rows()}
    today = datetime.now(TZ).date()
    done_items, stuck_items, first = [], [], None
    for kind, key, row, published in pending:
        fresh = rows.get(row["record_id"], {})
        if not fresh.get("编号"):
            continue
        line = brief_v1.describe(fresh, today, {})
        if kind == "result":
            first = first or brief_v1.number(fresh["编号"])
            head, hint, color, _ = line
            if published.get("confirmation"):
                hint = (hint + " · " if hint else "") + "需你定：" + published["confirmation"][:30]
            done_items.append(brief_v1._row_item((head, hint, color, None), published["url"]))
        else:
            stuck_items.append(brief_v1._row_item(line))
    blocks = []
    if done_items:
        blocks.append(brief_v1._block(f"做完了，请你看 · {len(done_items)} 件", "green", done_items))
    if stuck_items:
        blocks.append(brief_v1._block(f"卡住了，需要你补材料 · {len(stuck_items)} 件", "orange", stuck_items))
    if not blocks:
        return {"ok": True, "sent": False, "reason": "no_rows"}
    n = first or "编号"
    hint = f"回复：{n} 过（通过）· {n} 退 要改的地方 · 编号 + 材料（补材料）"
    title = "AI 做完了，请你验收" if done_items else "AI 卡住了，需要你补材料"
    text = brief_v1.card(title, "", "green" if done_items else "orange", blocks, hint, state.get("brief-v1:board-url") or config.base_url,
                         summary=f"{title}：{len(done_items)} 件做完，{len(stuck_items)} 件卡住")
    batch = "prework-now-" + sha256(js([k for _, k, _, _ in pending]).encode()).hexdigest()[:32]
    frozen = state.get(batch+":card")
    if frozen is None:
        state.set(batch+":card", text)
    receipt = send_once(state, batch, lambda: client.send_card(frozen or text, batch))
    for kind, key, row, _ in pending:
        if kind == "result" and row["status"] == "prepared":
            queue.done(row["record_id"], row["input_version"], receipt)
        state.set(key+":sent-now", receipt)
        state.set(key+":notice", receipt)
    return {"ok": True, "sent": True, "count": len(pending), "receipt": require_message_receipt(receipt)}


def mark_blocked(config, client, state, record_id, reason):
    """Show the blocker on the task itself and queue one line for the next brief."""
    from .workflow import Gateway, queue_notice
    gateway = Gateway(client, state)
    row = next((r for r in gateway.rows() if r["record_id"] == record_id), None)
    if not row:
        raise MatrixError("事项不存在。")
    blocker = re.sub(r"\s+", " ", reason).strip()[:28]
    gateway.update(row, {"状态": ["卡住"], "卡点": blocker, "进度": "卡住"}, "prework-block:" + record_id + ":" + sha256(blocker.encode()).hexdigest()[:12], "prework:block")


def main(argv=None):
    parser = argparse.ArgumentParser(description="预做评估、准备、结果交付；不对外提交")
    parser.add_argument("command", choices=["setup", "check", "status", "prepare", "draft-init", "stage", "publish", "notify", "block", "defer"])
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--record-id"); parser.add_argument("--version")
    parser.add_argument("--decision", help="内联版式决策 JSON；初始化前不预建文档或决策文件。"); parser.add_argument("--result", type=Path)
    parser.add_argument("--reason")
    parser.add_argument("--max-items", type=int, default=3, help="notify 单批合并条数，1–8，默认 3；已冻结批次保持原成员。")
    args = parser.parse_args(argv)
    path = args.config.resolve(); config = load(path)
    try:
        with exclusive_lock(path.parent/"state", wait=0 if args.command == "check" else 90):
            state = State(path.parent/"state/state.db")
            try:
                queue = WorkQueue(state)
                if args.command == "status":
                    result = {"ok": True, "enabled": config.prework_enabled, "lead_days": config.prework_lead_days, "rows": queue.list()}
                else:
                    client = Feishu(config, path.parent); client.verify_owner(client.auth()); require_owned(config, state)
                    if args.command == "setup": result = setup(config, client, state)
                    elif args.command == "check": result = check(config, client, state)
                    elif args.command == "prepare": result = prepare(config, client, state, args.record_id, args.version)
                    elif args.command == "draft-init":
                        if not args.decision: raise MatrixError("缺少版式决策。")
                        result = draft_init(config, client, state, args.record_id, args.version, json.loads(args.decision))
                    elif args.command == "stage":
                        if not args.result: raise MatrixError("缺少结果清单文件。")
                        result = stage(config, client, state, args.result)
                    elif args.command == "publish": result = publish(config, client, state, args.record_id, args.version)
                    elif args.command == "notify": result = notify(config, client, state, max_items=args.max_items)
                    else:
                        key_for(args.record_id, args.version)
                        if not args.reason: raise MatrixError("需要明确的卡点说明。")
                        if args.command == "block":
                            queue.block(args.record_id,args.version,args.reason)
                            if _v1(state):
                                mark_blocked(config, client, state, args.record_id, args.reason)
                        else: queue.defer(args.record_id,args.version,datetime.now(TZ),args.reason)
                        result = {"ok": True}
                print(js(result))
                from . import activity
                if args.command == "check":
                    activity.beat(path.parent, "代做检查", f"可做 {len(result.get('ready', []))} 件")
                elif args.command in {"publish", "block"}:
                    activity.log(path.parent, "prework", ("交付：" if args.command == "publish" else "卡住：") + str(result.get("title") or args.record_id))
                return 2 if args.command == "check" and not result.get("run_model") else 0
            finally: state.close()
    except MatrixBusy:
        print(js({"ok": True, "run_model": False, "reason": "busy"})); return 2
    except (MatrixError, OSError, ValueError) as exc:
        from . import activity
        activity.log(path.parent, "error", f"代做 {args.command} 出错", detail=str(exc), status="failed")
        print(js({"ok": False, "error": str(exc) if isinstance(exc, MatrixError) else "预做文件或结果格式异常，已停止。"})); return 1


if __name__ == "__main__":
    raise SystemExit(main())
