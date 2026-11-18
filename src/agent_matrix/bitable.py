import json
from .config import save
from .models import MatrixError
from .schema import TABLES, VIEWS, WEEK_FIELD, SEEDS
from .timeutil import now_iso


def js(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def items(data):
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        raise MatrixError("飞书列表响应结构异常。")
    if data.get("has_more"):
        raise MatrixError("飞书列表未完整返回，停止以免漏查后重复创建。")
    for key in ("items", "tables", "views", "fields"):
        if isinstance(data.get(key), list):
            return data[key]
    raise MatrixError("飞书列表响应缺少已知集合字段。")


def find_value(data, key):
    if isinstance(data, dict):
        if isinstance(data.get(key), str) and data[key]:
            return data[key]
        for value in data.values():
            found = find_value(value, key)
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = find_value(value, key)
            if found:
                return found
    return None


def require_owned(config, state):
    from .itemstore import feishu_free
    if feishu_free(config):
        return  # items in Notion or only on this computer: no Feishu table is ever written
    if not config.base_token or state.get("owned_base") != config.base_token:
        raise MatrixError("目标 Base 没有本项目的创建凭据；拒绝写入其他表格。")


def setup_preview(config):
    return {"dry_run": True, "stage": 0, "base_name": config.base_name,
            "target": config.base_token or "新建本系统专用多维表格", "tables": TABLES,
            "view_helper_field": WEEK_FIELD, "views": VIEWS,
            "disabled_rule_examples": SEEDS, "send_messages": False,
            "scheduling_enabled": False, "llm_enabled": False}


def setup(config, config_path, client, state):
    status = client.auth()
    client.verify_owner(status)
    if not config.owner_open_id:
        config.owner_open_id = status["user"]["openId"]
        config.owner_name = status["user"].get("userName") or ""
        config.app_id = status["app_id"]
        save(config_path, config)
    if not config.base_token:
        receipt = state.once("create-base", lambda: client.call("+base-create", "--name", config.base_name,
            "--time-zone", config.timezone, "--table-name", "事项表", "--fields", js(TABLES["事项表"]), write=True))
        token = find_value(receipt, "base_token")
        if not token:
            raise MatrixError("新建 Base 响应未返回 base_token；创建回执已保存，不会重复创建。")
        state.set("owned_base", token)
        config.base_token = token
        config.base_url = find_value(receipt, "url") or find_value(receipt, "base_url") or ""
        save(config_path, config)
    require_owned(config, state)
    coords = ["--base-token", config.base_token]
    existing = items(client.call("+table-list", *coords))
    for name, fields in TABLES.items():
        matches = [x for x in existing if x.get("name", x.get("table_name")) == name]
        if len(matches) > 1:
            raise MatrixError(f"发现多个同名表：{name}，停止避免选错。")
        if name in config.tables:
            if config.tables[name] not in {x.get("table_id", x.get("id")) for x in existing}:
                raise MatrixError(f"已绑定的{name}不再存在；不会自动重建。")
        elif matches:
            config.tables[name] = matches[0].get("table_id", matches[0].get("id"))
        else:
            receipt = state.once("create-table:" + name, lambda n=name, f=fields: client.call(
                "+table-create", *coords, "--name", n, "--fields", js(f), write=True))
            table_id = find_value(receipt, "table_id") or find_value(receipt, "id")
            if not table_id:
                raise MatrixError(f"{name}的创建回执缺少 ID，停止自动操作。")
            config.tables[name] = table_id
        save(config_path, config)

    # Inspect actual fields before formula/filter/record writes.
    field_maps = {}
    for name, table_id in config.tables.items():
        actual = items(client.call("+field-list", *coords, "--table-id", table_id))
        field_maps[name] = {x.get("name", x.get("field_name")): x for x in actual}
        missing = [f["name"] for f in TABLES[name] if f["name"] not in field_maps[name]]
        if missing:
            raise MatrixError(f"{name}字段不完整：{','.join(missing)}；不会覆盖现有结构。")

    task_coords = [*coords, "--table-id", config.tables["事项表"]]
    if WEEK_FIELD["name"] not in field_maps["事项表"]:
        client.call("+table-get", *task_coords)
        state.once("week-helper", lambda: client.call("+field-create", *task_coords, "--json", js(WEEK_FIELD), "--i-have-read-guide", write=True))
    available_views = items(client.call("+view-list", *task_coords))
    view_ids = {x.get("name", x.get("view_name")): x.get("id", x.get("view_id")) for x in available_views}
    for name, conditions in VIEWS.items():
        if name not in view_ids:
            receipt = state.once("create-view:" + name, lambda n=name: client.call("+view-create", *task_coords,
                "--json", js({"name": n, "type": "grid"}), write=True))
            view_ids[name] = find_value(receipt, "view_id") or find_value(receipt, "id")
            if not view_ids[name]:
                raise MatrixError(f"{name}创建回执缺少视图 ID，不继续配置。")
        # Existing user-edited filters stay untouched after the first successful setup.
        state.once("filter-view:" + name, lambda v=view_ids[name], c=conditions: client.call("+view-set-filter",
            *task_coords, "--view-id", v, "--json", js(c), write=True))

    state.once("seed-disabled-rules", lambda: client.call("+record-batch-create", *coords,
        "--table-id", config.tables["周期规则表"], "--json", js({"create_records": SEEDS}), write=True))
    state.once("setup-run-log", lambda: client.call("+record-batch-create", *coords,
        "--table-id", config.tables["运行日志表"], "--json", js({"create_records": [{
            "Agent名": "setup（第0阶段）", "开始时间": now_iso(), "结束时间": now_iso(),
            "状态": ["成功"], "新增条数": len(SEEDS), "更新条数": 0,
        }]}), write=True))
    state.set("setup_complete", True)
    return {"ok": True, "stage": 0, "base_url": config.base_url, "tables": config.tables,
            "views": list(VIEWS), "disabled_rules": len(SEEDS), "automatic_agents_enabled": False}
