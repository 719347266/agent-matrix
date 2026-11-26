"""Where items are recorded: the Feishu table, Notion, or this computer only."""
from pathlib import Path

from . import itemstore
from .config import load, save
from .feishu import Feishu
from .models import MatrixError
from .records import ensure_local
from .schema import FIELDS


def choose(root, mode):
    """The owner picks where items are recorded: "feishu" (Feishu table backup), "notion" (connect first,
    see notion.connect) or "device" (this computer only)."""
    root = Path(root)
    path = root / "config.yaml"
    config = load(path)
    if mode == "notion":
        if not (config.notion_token and config.notion_database_id):
            raise MatrixError("先连接 Notion。")
    elif mode == "feishu":
        if not config.base_token or config.tables.get("事项表") in (None, "local"):
            raise MatrixError("飞书还没初始化：先装好 lark-cli 并运行 setup。")
        db = itemstore._db(root)
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
    if not itemstore.imported(root) and config.item_store == "feishu" and config.base_token:
        # items still live in the Feishu table: bring them home first
        client = Feishu(config, root)
        client.verify_owner(client.auth())
        config.item_store = "local"
        ensure_local(client)
    if not itemstore.imported(root) and mode != "local":
        itemstore.import_rows(root, [], list(FIELDS))  # never used Feishu: start empty
    if mode == "notion":
        itemstore.queue_all(root)
    config.item_store = mode
    save(path, config)
    return mode
