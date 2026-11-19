"""Build an isolated copy of the live 心响 project for end-to-end tests.

    python setup.py <live root> <sandbox dir> <fake lark-cli>

Reads the live databases only (SQLite online backup, read-only connections). The copy's config points
lark_binary at the fake and turns the built-in AI off, so nothing in the copy can reach Feishu or start
a model. Also writes live-before.json: what the live project looks like now, to prove later it was not
touched by the tests.
"""
import hashlib
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse

live, sandbox, fake = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
DBS = ["state.db", "items.db", "xinxiang.db", "inbox.db", "activity.db", "app-commands.db", "cindy-pickup.sqlite"]


def live_fingerprint():
    out = {"dbs": {}}
    for name in DBS:
        with closing(sqlite3.connect(f"file:{live / 'state' / name}?mode=ro", uri=True, timeout=30)) as db:
            tables = [t for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            out["dbs"][name] = {t: db.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}
    shim = Path.home() / ".agent-matrix/bin/xinxiang"
    out["shim_sha256"] = hashlib.sha256(shim.read_bytes()).hexdigest() if shim.exists() else None
    out["config_sha256"] = hashlib.sha256((live / "config.yaml").read_bytes()).hexdigest()
    with closing(sqlite3.connect(f"file:{live / 'state/activity.db'}?mode=ro", uri=True)) as db:
        out["activity_max_id"] = db.execute("SELECT max(id) FROM events").fetchone()[0]
    return out


if sandbox.exists():
    shutil.rmtree(sandbox)
(sandbox / "state").mkdir(parents=True)
(sandbox / "home").mkdir()
(sandbox / "logs").mkdir()

for name in DBS:
    with closing(sqlite3.connect(f"file:{live / 'state' / name}?mode=ro", uri=True, timeout=30)) as src, \
            closing(sqlite3.connect(sandbox / "state" / name)) as dst:
        src.backup(dst)
for extra in ("tasks.json",):
    if (live / "state" / extra).exists():
        shutil.copyfile(live / "state" / extra, sandbox / "state" / extra)
shutil.copytree(live / "prompts", sandbox / "prompts")

sys.path.insert(0, str(live / "src"))
from agent_matrix.config import load, save  # noqa: E402

config = load(live / "config.yaml")
config.lark_binary = str(fake)
config.worker_enabled = False
save(sandbox / "config.yaml", config)

fields = ["标题", "方向", "负责人", "截止日", "首次截止日", "延期次数", "下一步动作", "优先级", "预计耗时(分钟)",
          "AI草稿链接", "预做结果摘要", "预做策略", "预做指令", "原话引用", "编号", "执行方", "进度", "卡点", "完成标准",
          "交付物链接", "验收结果", "退回意见", "父事项", "顺延次数", "去重键", "来源链接", "预做凭据", "更新时间",
          "创建时间", "能否AI预做", "类型", "关联任务链接", "顺延历史", "完成时间", "排除本人提醒", "补充材料", "拍板结果",
          "交付情况", "会议ID", "开始时间", "结束时间", "参会人", "逐字稿状态", "提取状态", "提取待办数", "Agent名",
          "新增条数", "更新条数"]
world = {"identity": {"owner_open_id": config.owner_open_id, "owner_name": config.owner_name, "app_id": config.app_id,
                      "base_host": urlparse(config.base_url).hostname},
         "field_names": fields, "meetings": {}, "transcripts": {}, "tasks": [], "docs": {}}
(sandbox / "world.json").write_text(json.dumps(world, ensure_ascii=False, indent=1))
(sandbox / "live-before.json").write_text(json.dumps(live_fingerprint(), ensure_ascii=False, indent=1))
print(json.dumps({"ok": True, "sandbox": str(sandbox)}, ensure_ascii=False))
