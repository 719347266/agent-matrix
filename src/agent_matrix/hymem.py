"""记忆增强 (optional): Tencent Hunyuan's Hy Memory (https://github.com/Tencent-Hunyuan/Hy-Memory, Apache-2.0) as a second,
deeper memory next to 心响's own.

心响's own memory stays as it is (rules, versions, 关于你 …) and works without this. When the owner switches this on and
fills in an OpenAI-compatible endpoint and key of their own (DeepSeek, 通义, 硅基流动, OpenAI …), every meeting outcome,
deliverable, verdict and 关于你 is also written into Hy Memory, which extracts, merges and reconciles them with that model;
before an AI works on an item, what Hy Memory recalls about it is put into the item's background.

Only the listener process holds the Hy Memory client (its stores are local files under state/hy_memory); other processes
queue what to write (hymem_queue) and ask the listener to search through the local app. The key never leaves config.yaml
except to the endpoint the owner chose.
"""
from datetime import datetime
import json
import os
from pathlib import Path
import threading

from . import db as database
from .timeutil import TZ

PRESETS = {   # shown as one-click choices; the owner still gets the key from that provider and pastes it
    "siliconflow": {"name": "硅基流动", "base_url": "https://api.siliconflow.cn/v1", "llm": "deepseek-ai/DeepSeek-V3",
                    "embed": "BAAI/bge-m3", "dims": 1024, "key_url": "https://cloud.siliconflow.cn/account/ak"},
    "dashscope": {"name": "阿里云百炼（通义）", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "llm": "qwen-plus",
                  "embed": "text-embedding-v3", "dims": 1024, "key_url": "https://bailian.console.aliyun.com/?apiKey=1"},
    "deepseek": {"name": "DeepSeek（向量另配）", "base_url": "https://api.deepseek.com", "llm": "deepseek-chat",
                 "embed": "", "dims": 1024, "key_url": "https://platform.deepseek.com/api_keys"},
    "openai": {"name": "OpenAI", "base_url": "https://api.openai.com/v1", "llm": "gpt-4.1-mini",
               "embed": "text-embedding-3-small", "dims": 1536, "key_url": "https://platform.openai.com/api-keys"},
}
USER = "owner"
MAX_TRIES = 5
_LOCK = threading.Lock()
_CLIENT = {}   # (root, settings fingerprint) → client


def _db(root):
    return database.open(root, "xinxiang")


def _now():
    return datetime.now(TZ).isoformat(timespec="seconds")


def installed():
    try:
        import importlib.util
        return importlib.util.find_spec("hy_memory") is not None
    except Exception:
        return False


def ready(config):
    """Switched on and filled in enough to be used."""
    return bool(config and getattr(config, "memory_boost_enabled", False) and config.memory_api_key
                and config.memory_base_url and config.memory_llm_model
                and (config.memory_embed_model) and installed())


def _env(root, config):
    return {
        "MEMORY_MODE": "pro", "MEMORY_LANGUAGE": "zh-Hans", "MEMORY_ENABLE_GRAPH": "false",
        "MEMORY_DATA_DIR": str(Path(root) / "state" / "hy_memory"),
        "MEMORY_LLM_PROVIDER": "openai", "MEMORY_LLM_BASE_URL": config.memory_base_url.strip(),
        "MEMORY_LLM_API_KEY": config.memory_api_key.strip(), "MEMORY_LLM_MODEL": config.memory_llm_model.strip(),
        "MEMORY_EMBEDDER_PROVIDER": "openai",
        "MEMORY_EMBEDDER_BASE_URL": (config.memory_embed_base_url or config.memory_base_url).strip(),
        "MEMORY_EMBEDDER_API_KEY": (config.memory_embed_api_key or config.memory_api_key).strip(),
        "MEMORY_EMBEDDER_MODEL": config.memory_embed_model.strip(), "MEMORY_EMBEDDING_DIMS": str(int(config.memory_embed_dims or 1024)),
        "MEMORY_LOG_LLM_CONTENT": "false", "ANONYMIZED_TELEMETRY": "False",
    }


def client(root, config):
    """The one Hy Memory client of this process, made with the owner's settings (made again when they change)."""
    env = _env(root, config)
    key = (str(Path(root).resolve()), json.dumps(env, sort_keys=True))
    with _LOCK:
        if key in _CLIENT:
            return _CLIENT[key]
        for old in list(_CLIENT.values()):
            try:
                old.close()
            except Exception:
                pass
        _CLIENT.clear()
        Path(env["MEMORY_DATA_DIR"]).mkdir(parents=True, exist_ok=True)
        os.environ.update(env)
        import logging
        for name in ("hy_memory", "chromadb", "httpx"):   # it logs every step at INFO: keep 心响's own log readable
            logging.getLogger(name).setLevel(logging.WARNING)
        from hy_memory import HyMemoryClient
        _CLIENT[key] = HyMemoryClient(mode="pro")
        return _CLIENT[key]


# ---- writing: anyone queues, the listener writes ---------------------------------------------------------------

def on(root):
    """Cheap check for writers in any process: the switch as last saved (the listener keeps it in sync)."""
    try:
        return (Path(root) / "state" / "hymem_on").is_file()
    except OSError:
        return False


def mark(root, enabled):
    flag = Path(root) / "state" / "hymem_on"
    if enabled:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(_now(), encoding="utf-8")
    else:
        flag.unlink(missing_ok=True)


def remember(root, kind, text, workspace="", ref=""):
    """Queue one thing to be written into Hy Memory (nothing happens while 记忆增强 is off). Same kind+ref only once."""
    text = str(text or "").strip()
    if not text or not on(root):
        return False
    db = _db(root)
    try:
        with db:
            cur = db.execute("INSERT OR IGNORE INTO hymem_queue (at, kind, text, workspace, ref) VALUES (?,?,?,?,?)",
                             (_now(), kind, text[:6000], str(workspace or "")[:80], str(ref or f"{kind}:{_now()}")[:120]))
        return cur.rowcount > 0
    finally:
        db.close()


def flush(root, config, limit=8):
    """Write what waits (called by the listener). Returns (written, failed)."""
    if not ready(config):
        return 0, 0
    db = _db(root)
    try:
        rows = db.execute("SELECT id, kind, text, workspace FROM hymem_queue WHERE done IS NULL AND tries<? ORDER BY id LIMIT ?",
                          (MAX_TRIES, limit)).fetchall()
    finally:
        db.close()
    written = failed = 0
    for qid, kind, text, workspace in rows:
        try:
            kw = {"user_id": USER}
            if workspace:
                kw["metadata"] = {"project": workspace, "kind": kind}
            client(root, config).add(text, **kw)
            _done(root, qid, None)
            written += 1
        except Exception as exc:
            _done(root, qid, f"{type(exc).__name__}: {exc}"[:300])
            failed += 1
    return written, failed


def _done(root, qid, error):
    db = _db(root)
    try:
        with db:
            if error:
                db.execute("UPDATE hymem_queue SET tries=tries+1, error=? WHERE id=?", (error, qid))
            else:
                db.execute("UPDATE hymem_queue SET done=?, error=NULL WHERE id=?", (_now(), qid))
    finally:
        db.close()


def status(root):
    db = _db(root)
    try:
        done, waiting, stuck = db.execute(
            "SELECT sum(done IS NOT NULL), sum(done IS NULL AND tries<?), sum(done IS NULL AND tries>=?) FROM hymem_queue",
            (MAX_TRIES, MAX_TRIES)).fetchone()
        last = db.execute("SELECT error FROM hymem_queue WHERE error IS NOT NULL AND done IS NULL ORDER BY id DESC LIMIT 1").fetchone()
        return {"written": done or 0, "waiting": waiting or 0, "stuck": stuck or 0, "error": last[0] if last else ""}
    finally:
        db.close()


# ---- reading -----------------------------------------------------------------------------------------------

def search(root, config, query, limit=8):
    """What Hy Memory recalls for a question, as plain lines (profile first, then the rest)."""
    if not ready(config) or not str(query or "").strip():
        return []
    found = client(root, config).search(str(query)[:500], user_ids=[USER], limit=limit)
    mem = (found or {}).get("memories") or {}
    lines = []
    for channel in ("profile", "normal", "proactive"):
        for m in mem.get(channel) or []:
            text = " ".join(str(m.get("content") or "").split())
            if text and text not in lines:
                lines.append(text)
    return lines[:limit]


def search_via_app(root, query, limit=8, timeout=40):
    """For processes other than the listener (preparing an AI job): ask the listener's local app to search."""
    import urllib.request
    try:
        info = json.loads((Path(root) / "state" / "app.json").read_text(encoding="utf-8"))
        token = (Path(root) / "state" / "app-token").read_text(encoding="utf-8").strip()
        req = urllib.request.Request(f"http://127.0.0.1:{info['port']}/api/hymem", method="POST",
                                     data=json.dumps({"search": query, "limit": limit}).encode("utf-8"),
                                     headers={"Content-Type": "application/json", "X-Xinxiang-Token": token,
                                              "Host": f"127.0.0.1:{info['port']}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")).get("lines") or []
    except Exception:
        return []


def test(root, config):
    """Try the owner's settings once: the model answers, a memory is written and found again."""
    if not installed():
        return {"ok": False, "error": "这台电脑上的心响还没带上 Hy Memory，更新到最新版再试"}
    if not ready(config):
        return {"ok": False, "error": "还没填全：接口地址、API Key、大模型和向量模型都要有"}
    try:
        c = client(root, config)
        c.add("测试：心响记忆增强已接通。", user_id="xinxiang-test")
        found = c.search("心响记忆增强", user_ids=["xinxiang-test"], limit=3)
        return {"ok": True, "found": bool((found or {}).get("memories"))}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}


def backfill(root):
    """When 记忆增强 is switched on: queue what 心响 already knows, so Hy Memory does not start empty."""
    from . import memory
    n = 0
    prof = memory.profile(root)["text"].strip()
    if prof:
        n += remember(root, "profile", "关于本人：\n" + prof, ref="profile:" + prof[:40])
    for m in memory.moments(root, limit=300):
        n += remember(root, "moment", f"{m.get('at', '')[:10]} {m.get('source_title') or ''}：{m.get('text')}", ref=f"moment:{m.get('id')}")
    for o in memory.outcomes(root):
        if o.get("reason") or o["verdict"] == "通过":
            n += remember(root, "verdict", f"{o['at']} 本人对「{o.get('template') or '交付'}」的评审：{o['verdict']}" + (f"，原因：{o['reason']}" if o.get("reason") else ""),
                          workspace=o.get("project") or "", ref=f"verdict:{o['record_id']}:{o['at']}")
    return n
