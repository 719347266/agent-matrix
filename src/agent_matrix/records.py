"""Minimal, complete reads of tables owned by this project."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from .bitable import items, require_owned
from .models import MatrixError


def ensure_fields(client, table_name, fields):
    if table_name not in client.config.tables:
        raise MatrixError("尚未绑定表格：" + table_name)
    actual=items(client.call("+field-list","--base-token",client.config.base_token,
                            "--table-id",client.config.tables[table_name]))
    names={f.get("name",f.get("field_name")) for f in actual}
    missing=set(fields)-names
    if missing:
        raise MatrixError(table_name+"缺少必要字段："+"、".join(sorted(missing)))


def read_records(client, state, table_name, fields):
    require_owned(client.config,state)
    if table_name not in client.config.tables:
        raise MatrixError("表格不属于当前项目绑定范围。")
    table_id=client.config.tables[table_name]
    projected=list(dict.fromkeys(fields))
    projection=[part for name in projected for part in ("--field-id",name)]
    root=Path(client.root).resolve(); (root/"state").mkdir(exist_ok=True)
    rows=[]; revision=None; offset=0
    with TemporaryDirectory(prefix="records-read-",dir=root/"state") as directory:
        for page in range(100):
            path=Path(directory)/f"page-{page}.ndjson"
            client._exec(["base","+record-list","--base-token",client.config.base_token,"--table-id",table_id,
                          *projection,"--offset",str(offset),"--as","user","--format","ndjson",
                          "--output",str(path.relative_to(root))],manifest=True)
            meta=json.loads(path.with_suffix(".manifest.json").read_text())
            if (meta.get("base_token"),meta.get("table_id"))!=(client.config.base_token,table_id):
                raise MatrixError("返回数据与请求表格不一致。")
            if not isinstance(meta.get("has_more"),bool) or "rev" not in meta:
                raise MatrixError("表格返回缺少完整性信息。")
            if revision is not None and meta["rev"]!=revision:
                raise MatrixError("分页读取时表格版本变化，需要重读；本轮不做部分处理。")
            revision=meta["rev"]
            if not set(projected)<=set(meta.get("columns",{})):
                raise MatrixError("导出字段缺失或已经改名，停止处理。")
            chunk=[json.loads(line) for line in path.read_text().splitlines() if line.strip()]
            if len(chunk)!=meta.get("records_count"):
                raise MatrixError("导出记录条数校验失败。")
            rows.extend(chunk)
            if not meta["has_more"]:
                ids=[r.get("record_id") for r in rows]
                if None in ids or len(ids)!=len(set(ids)):
                    raise MatrixError("表格记录 ID 缺失或重复。")
                return rows
            following=meta.get("next_offset")
            if not isinstance(following,int) or following<=offset:
                raise MatrixError("分页游标异常。")
            offset=following
    raise MatrixError("超过单轮读取上限，不返回不完整结果。")
