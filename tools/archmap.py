"""Read the package and report its structure: who imports whom, which layer each module is in,
where the layering is broken, import cycles, and direct database connections.

    python tools/archmap.py            # print the report
Used by tests/test_architecture.py, which fails when the structure gets worse.
"""
import ast
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "src" / "agent_matrix"

# A module may import from its own layer or any layer below it, never from above.
LAYERS = [
    ("基础", ["models", "config", "timeutil", "schema", "alerts", "cards", "digest", "reply_timing", "llm",
              "precheck", "db", "lifecycle", "values", "textmatch", "delivery", "setupshim", "promptfile", "localdoc", "procs"]),
    ("存储", ["state", "activity", "inbox", "thread", "prework_store", "itemstore", "memory", "jobstore", "snapshot", "itemlinks", "projects", "bugs", "approvals", "setupasks", "versions", "stats", "tidy"]),
    ("外部连接", ["feishu", "bitable", "records", "notion", "storemode", "workenv", "sources.chats", "sources.slack", "sources.wechat", "sources"]),
    ("业务规则", ["workflow", "merge", "automation", "linked_sync", "sources.feishu_tasks", "manual", "deliverables", "automation_demo", "brief_migration", "brief_v1", "prework",
                  "reply", "meeting_watch", "meeting_review", "chat_review", "background", "pastwork", "proactive",
                  "agents", "agents.daily_brief", "agents.meeting_followup", "agents.meeting_prep",
                  "agents.pre_worker", "agents.recurring", "agents.weekly_review"]),
    ("AI执行", ["engines", "itemwork", "drive", "worker", "sessions", "local_sessions", "cindy_pickup", "aidrivers"]),
    ("入口", ["listen", "app", "cli", "winclient", "setupcheck", "update", "scheduled", "monitor", "tray", "commands", "__main__", "__init__"]),
]
LEVEL = {m: i for i, (_, mods) in enumerate(LAYERS) for m in mods}


def modules():
    out = {}
    for path in sorted(PKG.rglob("*.py")):
        name = ".".join(path.relative_to(PKG).with_suffix("").parts)
        out[name.removesuffix(".__init__") if name != "__init__" else name] = path
    return out


def imports(mods):
    """{module: [(target, top_level, line)]} for imports inside the package."""
    out = {}
    for name, path in mods.items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        top = {id(n) for n in tree.body}
        package = name.split(".")[:-1] if path.name != "__init__.py" else ([] if name == "__init__" else name.split("."))
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level:
                base = package[:len(package) - (node.level - 1)] + (node.module.split(".") if node.module else [])
            elif (node.module or "").split(".")[0] == "agent_matrix":
                base = node.module.split(".")[1:]
            else:
                continue
            for alias in node.names:
                target = ".".join(base + [alias.name])
                if target not in mods:
                    target = ".".join(base)
                if target and target in mods and target != name:
                    found.append((target, id(node) in top, node.lineno))
        out[name] = sorted(set(found))
    return out


def upward(graph):
    """Imports that reach into a higher layer."""
    return sorted((src, dst, line) for src, edges in graph.items() for dst, _, line in edges
                  if LEVEL.get(dst, 99) > LEVEL.get(src, 99))


def cycles(graph):
    """Groups of modules that import each other (Tarjan)."""
    index, low, stack, on, out, counter = {}, {}, [], set(), [], [0]
    sys.setrecursionlimit(10000)

    def visit(v):
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        for w, _, _ in graph.get(v, ()):
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            group = []
            while True:
                w = stack.pop()
                on.discard(w)
                group.append(w)
                if w == v:
                    break
            if len(group) > 1:
                out.append(sorted(group))

    for v in graph:
        if v not in index:
            visit(v)
    return sorted(out)


def connects(mods):
    """Modules that open SQLite themselves instead of going through db.py."""
    return sorted(name for name, path in mods.items()
                  if name != "db" and "sqlite3.connect(" in path.read_text(encoding="utf-8"))


def report():
    mods = modules()
    graph = imports(mods)
    return {"modules": mods, "graph": graph, "unplaced": sorted(set(mods) - set(LEVEL)), "upward": upward(graph),
            "cycles": cycles(graph), "lazy": sum(1 for e in graph.values() for _, top, _ in e if not top),
            "connects": connects(mods)}


if __name__ == "__main__":
    r = report()
    print("未归层:", r["unplaced"])
    print("向上引用:", len(r["upward"]))
    for src, dst, line in r["upward"]:
        print(f"  {src}:{line} -> {dst}")
    print("循环依赖:", len(r["cycles"]))
    for group in r["cycles"]:
        print("  ", group)
    print("函数内引用:", r["lazy"])
    print("自行连库:", r["connects"])
